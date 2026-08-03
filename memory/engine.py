"""BeanAgent 长期记忆模块统一入口与生命周期管理。"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent.config_models import MemoryConfig
from memory.consolidator import (
    ConsolidationDraft,
    ConsolidationExtractor,
    ConsolidationResult,
    Consolidator,
    render_consolidation_conversation,
)
from memory.contracts import (
    EvidenceRef, MemoryIngestRequest, MemoryIngestResult, MemoryMutation,
    MemoryMutationResult, MemoryQuery, MemoryQueryResult, MemoryRecord,
    MemoryToolProfile, MemoryToolSpec,
)
from memory.events import ConsolidationCommitted, TurnIngested
from memory.hyde_enhancer import HyDEEnhancer
from memory.injection_planner import InjectionPlanner
from memory.implicit_extractor import ImplicitLongTermExtractor, ImplicitMemoryDraft
from memory.md_store import MarkdownMemoryStore
from memory.memorizer import Memorizer
from memory.optimizer import MemoryOptimizer
from memory.post_response_worker import PostResponseMemoryWorker
from memory.query_builder import build_memory_queries, build_procedure_queries
from memory.query_rewriter import QueryRewriter
from memory.retriever import Retriever
from memory.rule_schema import build_procedure_rule_schema
from memory.store import MemoryStore2
from memory.structured_output import (
    CONSOLIDATION_EVENTS_TOOL,
    RECENT_CONTEXT_TOOL,
    complete_forced_function,
)
from memory.sufficiency_checker import should_enhance_retrieval
from session.store import SessionStore

if TYPE_CHECKING:
    from agent.event_bus import EventBus

logger = logging.getLogger(__name__)


class MemoryEngine:
    """组装记忆读写、归档和工具能力，不持有共享 LLMProvider 的所有权。"""

    def __init__(self, workspace: Path, embedder: Any, provider: Any, sessions: SessionStore, *, config: MemoryConfig | None = None, consolidation_extractor: ConsolidationExtractor | None = None, implicit_extractor: Any | None = None, keep_count: int | None = None, consolidation_threshold: int | None = None) -> None:
        self._config = config or MemoryConfig()
        self._embedder = embedder
        self._provider = provider
        self._sessions = sessions
        self._store = MemoryStore2(Path(workspace) / "memory" / "memory2.db", self._config.embedding.dimensions)
        self._markdown = MarkdownMemoryStore(Path(workspace))
        self._retriever = Retriever(
            self._store, embedder,
            rrf_k=self._config.retrieval.rrf_k,
            keyword_rrf_weight=self._config.retrieval.keyword_rrf_weight,
            hotness_alpha=self._config.retrieval.hotness_alpha,
            hotness_half_life_days=self._config.retrieval.half_life_days,
        )
        self._memorizer = Memorizer(self._store, embedder)
        self._rewriter = QueryRewriter(provider)
        self._hyde = HyDEEnhancer(provider)
        retrieval_config = self._config.retrieval
        self._injection_planner = InjectionPlanner(
            thresholds={
                "procedure": retrieval_config.procedure_threshold,
                "preference": retrieval_config.preference_threshold,
                "event": retrieval_config.event_threshold,
                "profile": retrieval_config.profile_threshold,
            },
            max_forced_procedures=retrieval_config.max_forced_procedures,
            max_procedure_preference=retrieval_config.max_procedure_preference,
            max_event_profile=retrieval_config.max_event_profile,
        )
        self._optimizer = MemoryOptimizer(
            self._markdown,
            provider,
            step_delay_seconds=self._config.optimizer.step_delay_seconds,
        )
        # 生产运行只配置 context_window；显式覆盖仅用于小窗口确定性测试。
        derived_keep_count = self._config.keep_count if keep_count is None else keep_count
        derived_threshold = (
            self._config.consolidation_min_new_messages
            if consolidation_threshold is None
            else consolidation_threshold
        )
        # 生产环境将后台压缩软门槛与 Turn 前积压保护分开：默认在 30 条启动后台
        # 压缩，只有继续积压到 36 条才同步等待。显式覆盖继续服务小窗口测试。
        self._context_guard_threshold = (
            self._config.context_guard_threshold
            if keep_count is None and consolidation_threshold is None
            else derived_keep_count + derived_threshold
        )
        self._consolidator = Consolidator(
            sessions, self._markdown,
            consolidation_extractor or _LLMConsolidationExtractor(provider),
            keep_count=derived_keep_count, threshold=derived_threshold,
        )
        self._implicit_extractor = implicit_extractor or ImplicitLongTermExtractor(provider)
        self._post_response = PostResponseMemoryWorker(
            self._memorizer,
            self._retriever,
            provider,
        )
        # 构造函数可能运行在事件循环之外，因此这里只创建惰性绑定的队列；常驻任务在
        # 第一次摄入 Turn 时启动，避免初始化阶段错误捕获不存在的 event loop。
        self._post_response_queue: asyncio.Queue[TurnIngested] = asyncio.Queue()
        self._post_response_task: asyncio.Task[None] | None = None
        # 参考实现按 session_key 隔离维护队列：同一会话严格串行，不同会话各自拥有
        # asyncio Task，可以并行等待 LLM/IO，避免慢会话阻塞全部用户。
        self._maintenance_queues: dict[str, deque[TurnIngested]] = {}
        self._maintenance_locks: dict[str, asyncio.Lock] = {}
        self._maintenance_tasks: dict[str, asyncio.Task[None]] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._accepting = True
        self._closed = False
        self._event_bus: EventBus | None = None

    async def query(self, request: MemoryQuery) -> MemoryQueryResult:
        if request.intent == "timeline":
            return self._query_timeline(request)
        scope = request.scope
        require_scope_match = _should_require_scope_match(request)
        recent_history = ""
        aux_queries = _string_list(request.context.get("aux_queries"))
        memory_types = list(request.filters.kinds) or None
        if request.intent == "interest":
            # 主动 Agent 只需要了解用户稳定兴趣与个人画像。这里固定收窄类型并丢弃
            # 外部辅助查询，避免调用方借 filters/上下文扩大到事件或执行规则。
            memory_types = ["preference", "profile"]
            aux_queries = []
        if request.intent == "procedure":
            # 专用流程查询只查看规则和服务偏好，不触发任何 LLM 改写；调用方
            # 可提供一个已生成的辅助 query，原始行动描述始终保留为主 query。
            memory_types = ["procedure", "preference"]
            rewritten = aux_queries[0] if aux_queries else ""
            procedure_queries = build_procedure_queries(request.text, rewritten)
            aux_queries = list(dict.fromkeys([
                *procedure_queries[1:],
                *aux_queries[1:],
            ]))
        if request.intent == "answer":
            # 深度回忆允许使用近期对话消解指代；普通 Turn 不经过这条 LLM 路径，避免增加首字等待。
            session_key = scope.session_key or (
                f"{scope.channel}:{scope.chat_id}"
                if scope.channel and scope.chat_id
                else ""
            )
            history = (
                await asyncio.to_thread(self._sessions.load_history, session_key, 6)
                if session_key
                else []
            )
            recent_history = _format_recent_history(history)
            try:
                decision = await self._rewriter.decide(request.text, recent_history)
                queries = build_memory_queries(
                    request.text,
                    decision.episodic_query if decision.needs_episodic else "",
                    decision.procedure_query,
                )
                aux_queries = list(dict.fromkeys([*aux_queries, *queries[1:]]))
            except Exception as error:
                # 查询改写只是增强能力；失败时必须保留原始问题继续检索，不能让记忆工具整体失败。
                logger.warning("深度记忆查询改写失败，使用原始问题检索: %s", error)
        items = await self._retriever.retrieve(
            request.text,
            memory_types=memory_types,
            top_k=request.limit,
            scope_channel=scope.channel or None,
            scope_chat_id=scope.chat_id or None,
            require_scope_match=require_scope_match,
            time_start=request.filters.time_start,
            time_end=request.filters.time_end,
            aux_queries=aux_queries,
        )
        hyde_used = False
        hypothesis: str | None = None
        if request.intent == "answer" and should_enhance_retrieval(items):
            # HyDE 只服务显式深度回忆的空召回；普通上下文即使未命中也要快速进入主回答。
            async def retrieve_hypothesis(query: str) -> list[dict[str, object]]:
                return await self._retriever.retrieve(
                    query,
                    memory_types=list(request.filters.kinds) or None,
                    top_k=request.limit,
                    scope_channel=scope.channel or None,
                    scope_chat_id=scope.chat_id or None,
                    require_scope_match=require_scope_match,
                    time_start=request.filters.time_start,
                    time_end=request.filters.time_end,
                )

            augmented = await self._hyde.augment(
                query=request.text,
                context=recent_history,
                raw_items=items,
                retrieve_fn=retrieve_hypothesis,
            )
            items = augmented.items
            hyde_used = augmented.used_hyde
            hypothesis = augmented.hypothesis

        injection_plan = self._injection_planner.plan(items)
        items = injection_plan.items
        records: list[MemoryRecord] = []
        for item in items:
            source_ref = str(item.get("source_ref") or "")
            evidence = [
                EvidenceRef(
                    kind="message",
                    refs=[source_ref] if source_ref else [],
                    source_ref=source_ref,
                )
            ] if source_ref else []
            extra = item.get("extra_json")
            records.append(MemoryRecord(
                id=str(item["id"]),
                kind=str(item.get("memory_type") or "event"),
                summary=str(item.get("summary") or ""),
                score=float(item.get("score") or 0),
                evidence=evidence,
                signals=extra if isinstance(extra, dict) else {},
            ))
        return MemoryQueryResult(records=records, trace={
            "engine": "default",
            "intent": request.intent,
            "memory_types": memory_types,
            "read_only": request.intent == "interest",
            "vector_keyword_fusion": True,
            "hyde_used": hyde_used,
            "hyde_hypothesis": hypothesis,
            "injected_ids": [str(item.get("id") or "") for item in items],
            "rejected": injection_plan.rejected,
        })

    def _query_timeline(self, request: MemoryQuery) -> MemoryQueryResult:
        """直接读取结构化事件时间线，避免为确定的时间范围调用远端 Embedding。"""

        time_start = request.filters.time_start
        time_end = request.filters.time_end
        if time_start is None or time_end is None:
            return MemoryQueryResult(trace={
                "engine": "default",
                "intent": "timeline_missing_time",
                "hit_count": 0,
            })
        items = self._store.list_events_by_time_range(
            time_start,
            time_end,
            limit=request.limit,
        )
        records = [_record_from_item(item) for item in items]
        return MemoryQueryResult(records=records, trace={
            "engine": "default",
            "intent": "timeline",
            "hit_count": len(records),
        })

    async def mutate(self, mutation: MemoryMutation) -> MemoryMutationResult:
        if mutation.kind == "forget":
            requested = list(dict.fromkeys(value for value in mutation.ids if value))
            affected, missing = self._memorizer.supersede_batch(requested)
            return MemoryMutationResult(status="superseded", affected_ids=affected, missing_ids=missing, items=self._store.get_items_by_ids(affected))

        summary = mutation.summary.strip()
        if not summary:
            return MemoryMutationResult(status="ignored", actual_kind=mutation.memory_kind)
        metadata = dict(mutation.metadata)
        actual_kind = mutation.memory_kind.strip() or "preference"
        if actual_kind == "procedure" and not str(metadata.get("tool_requirement") or "").strip():
            # procedure 没有可执行工具约束时无法安全拦截，参考实现降级为 preference。
            actual_kind = "preference"
        if actual_kind == "procedure":
            metadata["rule_schema"] = build_procedure_rule_schema(
                summary,
                tool_requirement=str(metadata.get("tool_requirement") or "") or None,
                steps=[str(step) for step in metadata.get("steps", [])] if isinstance(metadata.get("steps"), list) else [],
                rule_schema=metadata.get("rule_schema") if isinstance(metadata.get("rule_schema"), dict) else None,
            )
        # 对齐 Akashic：显式 memorize 是 workspace 级长期记忆。Turn scope 只参与调用
        # 上下文，不持久化为条目过滤条件；原始来源仍由 source_ref 审计。
        metadata.pop("scope_channel", None)
        metadata.pop("scope_chat_id", None)
        result = await self._memorizer.save_item_with_supersede(
            summary, actual_kind, metadata, mutation.source_ref,
            emotional_weight=int(metadata.get("emotional_weight", 0) or 0),
            supersede_threshold=self._config.dedup.supersede_threshold,
        )
        status, item_id = result.split(":", 1)
        return MemoryMutationResult(item_id=item_id, status=status, actual_kind=actual_kind)

    def tool_profile(self) -> MemoryToolProfile:
        return _tool_profile()

    def read_self(self) -> str:
        """供 PromptBlock 读取自我认知，不暴露 Markdown Store 所有权。"""

        return self._markdown.read_self()

    def get_memory_context(self) -> str:
        """返回可直接注入稳定 Prompt 前缀的 Markdown 长期记忆。"""

        return self._markdown.get_memory_context()

    def read_recent_context(self, session_key: str = "") -> str:
        """读取当前会话的近期压缩上下文；普通 Prompt 不再读取全局 RECENT_CONTEXT.md。"""

        key = str(session_key or "").strip()
        return self._sessions.get_recent_context(key) if key else ""

    async def retrieve_for_turn(self, message: Any) -> str:
        text = str(getattr(message, "content", getattr(message, "text", "")) or "")
        channel = str(getattr(message, "channel", "") or "")
        chat_id = str(getattr(message, "chat_id", "") or "")
        if not text.strip():
            return ""
        # 每轮自动召回只使用当前问题做低延迟检索；LLM 改写和 HyDE 由显式 answer 查询按需承担。
        result = await self.query(MemoryQuery(
            text,
            intent="context",
            scope=_scope(channel, chat_id),
        ))
        return _injection_block(result.records)

    async def ingest(self, request: MemoryIngestRequest) -> MemoryIngestResult:
        """接受标准化摄入请求；当前最小闭环只处理完整 Turn。"""

        if request.source_kind != "turn":
            return MemoryIngestResult(
                accepted=False,
                summary=f"不支持的记忆摄入类型: {request.source_kind}",
            )
        event = dict(request.content)
        event.setdefault("session_key", request.scope.session_key)
        event.setdefault("channel", request.scope.channel)
        event.setdefault("chat_id", request.scope.chat_id)
        event.update({key: value for key, value in request.metadata.items() if key not in event})
        await self.on_turn_committed(event)
        return MemoryIngestResult(accepted=True, summary="Turn 已进入记忆后台处理队列")

    async def on_turn_committed(self, event: Any) -> None:
        """将已持久化 Turn 快照投递给两条独立后台处理链。"""

        status = event.get("status") if isinstance(event, dict) else getattr(event, "status", "ok")
        if str(status or "ok") != "ok":
            return
        if not self._accepting or self._closed:
            raise RuntimeError("MemoryEngine 已停止接收新的 Turn")
        snapshot = self._build_turn_snapshot(event)
        await self._ensure_workers()
        # put_nowait 是 Turn 提交边界的关键：回复发送与 Session 提交不等待 LLM 提取、
        # 向量化或 Markdown IO，后台失败也不会回滚已经落库的对话。
        self._post_response_queue.put_nowait(snapshot)
        self._enqueue_maintenance(snapshot)

    def bind_events(self, event_bus: EventBus) -> None:
        """订阅正式 TurnCommitted；重复绑定同一总线保持幂等。"""

        from agent.event_bus import TurnCommitted

        if self._event_bus is event_bus:
            return
        self.unbind_events()
        event_bus.on(TurnCommitted, self.on_turn_committed)
        event_bus.on(ConsolidationCommitted, self.on_consolidation_committed)
        self._event_bus = event_bus

    def unbind_events(self) -> None:
        """关闭前解除订阅，避免已关闭的 Store 再收到提交事件。"""

        if self._event_bus is None:
            return
        from agent.event_bus import TurnCommitted

        self._event_bus.off(TurnCommitted, self.on_turn_committed)
        self._event_bus.off(ConsolidationCommitted, self.on_consolidation_committed)
        self._event_bus = None

    async def _run_consolidation(self, event: TurnIngested) -> ConsolidationResult | None:
        await self.replay_pending_consolidations()
        result = await self._consolidator.consolidate(event.session_key)
        if result is None:
            return None
        committed = ConsolidationCommitted(
            history_entry_payloads=[
                (str(entry.get("summary") or ""), int(entry.get("emotional_weight", 0) or 0))
                for entry in result.history_entries if str(entry.get("summary") or "").strip()
            ],
            source_ref=result.source_ref,
            scope_channel=event.channel,
            scope_chat_id=event.chat_id,
            conversation=result.conversation,
        )
        # outbox 必须先于 cursor 和事件派发持久化；若进程在派发前崩溃，
        # 下次 Turn 仍能从 SQLite 重放向量同步，而不会丢失已归档窗口的派生任务。
        self._store.enqueue_consolidation(result.source_ref, _consolidation_payload(committed))
        # cursor 是模型历史的排除边界，只有 Markdown 和 outbox 都可恢复后才能推进。
        self._sessions.set_cursor(event.session_key, result.cursor)
        if self._event_bus is not None:
            await self._event_bus.emit(committed)
        else:
            await self.on_consolidation_committed(committed)
        return result

    async def ensure_context_ready(self, session_key: str) -> bool:
        """在普通 Turn 前确保未归档历史没有越过安全阈值。

        正常压缩仍由 TurnCommitted 后台触发；这里只处理后台尚未完成或曾经
        失败的积压。cursor 必须真实推进才算恢复，避免压缩器静默跳过后继续
        把过量原文送入模型。
        """

        key = str(session_key or "").strip()
        if not key:
            raise ValueError("session_key 不能为空")
        messages = self._sessions.fetch_session_messages(key)
        before = max(0, min(self._sessions.get_cursor(key), len(messages)))
        if len(messages) - before < self._context_guard_threshold:
            return True

        channel, _, chat_id = key.partition(":")
        event = TurnIngested(
            session_key=key,
            channel=channel,
            chat_id=chat_id,
            user_message="",
            assistant_response="",
            tool_chain=[],
            source_ref=key,
        )
        try:
            await self._run_consolidation_serialized(event)
        except Exception:
            logger.exception("Turn 前记忆归档失败: session_key=%s", key)
            return False
        return self._sessions.get_cursor(key) > before

    def needs_context_preparation(self, session_key: str) -> bool:
        """只判断下一轮是否需要同步归档，不触发任何维护任务。"""

        key = str(session_key or "").strip()
        if not key:
            raise ValueError("session_key 不能为空")
        messages = self._sessions.fetch_session_messages(key)
        cursor = max(0, min(self._sessions.get_cursor(key), len(messages)))
        return len(messages) - cursor >= self._context_guard_threshold

    async def _run_consolidation_serialized(
        self,
        event: TurnIngested,
    ) -> ConsolidationResult | None:
        """让后台维护与 Turn 前保护共享同一 Session 的压缩临界区。"""

        lock = self._maintenance_locks.setdefault(event.session_key, asyncio.Lock())
        async with lock:
            return await self._run_consolidation(event)

    async def on_consolidation_committed(self, event: ConsolidationCommitted) -> None:
        # 事件向量写入与隐式记忆提取只共享归档原文，彼此没有数据依赖。并发等待
        # 两类外部 IO，随后才写 implicit 并清理 outbox，保持原有提交边界。
        async with asyncio.TaskGroup() as group:
            group.create_task(self._save_consolidation_events(event))
            implicit_task = group.create_task(
                self._implicit_extractor.extract(
                    event.conversation,
                    existing_profile="",
                )
            )
        implicit = implicit_task.result()
        await self._save_implicit_long_term(implicit, event)
        self._store.complete_consolidation(event.source_ref)

    async def _save_consolidation_events(
        self,
        event: ConsolidationCommitted,
    ) -> None:
        """顺序写入单个归档窗口的事件，控制 Embedding API 瞬时并发。"""

        for index, (summary, emotional_weight) in enumerate(event.history_entry_payloads):
            summary = str(summary or "").strip()
            if summary:
                await self._memorizer.save_from_consolidation(
                    summary, [], f"{event.source_ref}#{index}", event.scope_channel, event.scope_chat_id,
                    emotional_weight=emotional_weight,
                )

    async def replay_pending_consolidations(self) -> None:
        """重放未完成的向量事务；单条失败保留 outbox 并继续其它窗口。"""

        for payload in self._store.list_pending_consolidations():
            try:
                await self.on_consolidation_committed(_consolidation_from_payload(payload))
            except Exception:
                logger.exception("Consolidation 向量同步重放失败: source_ref=%s", payload.get("source_ref"))

    async def _save_implicit_long_term(
        self,
        draft: ImplicitMemoryDraft,
        event: ConsolidationCommitted,
    ) -> None:
        for memory_type, items in (
            ("profile", draft.profile),
            ("preference", draft.preference),
            ("procedure", draft.procedure),
        ):
            for index, item in enumerate(items):
                summary = str(item.get("summary") or "").strip()
                if not summary:
                    continue
                extra: dict[str, object] = {
                    "scope_channel": event.scope_channel,
                    "scope_chat_id": event.scope_chat_id,
                }
                if memory_type == "profile":
                    extra["category"] = str(item.get("category") or "personal_fact")
                else:
                    extra["tool_requirement"] = item.get("tool_requirement")
                    extra["steps"] = item.get("steps") if isinstance(item.get("steps"), list) else []
                    if memory_type == "procedure" and isinstance(item.get("rule_schema"), dict):
                        extra["rule_schema"] = item["rule_schema"]
                happened_at = item.get("happened_at") if isinstance(item.get("happened_at"), str) else None
                await self._memorizer.save_item_with_supersede(
                    summary,
                    memory_type,
                    extra,
                    f"{event.source_ref}#{memory_type}:{index}",
                    happened_at=happened_at,
                    emotional_weight=_emotional_weight(item.get("emotional_weight")),
                    supersede_threshold=self._config.dedup.supersede_threshold,
                )

    async def drain(self) -> None:
        """等待当前已接收的两类后台任务全部完成，主要用于关闭和确定性测试。"""

        await self._post_response_queue.join()
        # 新任务可能在一次 gather 快照完成前被追加，因此循环到任务表真正为空。
        while self._maintenance_tasks:
            await asyncio.gather(*list(self._maintenance_tasks.values()), return_exceptions=True)

    async def _ensure_workers(self) -> None:
        async with self._lifecycle_lock:
            if self._post_response_task is None:
                self._post_response_task = asyncio.create_task(
                    self._post_response_loop(), name="memory-post-response"
                )

    async def _post_response_loop(self) -> None:
        while True:
            event = await self._post_response_queue.get()
            try:
                # worker 内部也会隔离 LLM/检索异常；此处再设任务级边界，防止未来实现
                # 变更时一次异常终止常驻消费者，导致 queue.join() 永久等待。
                await self._post_response.handle(event)
            except Exception:
                logger.exception("每轮记忆失效处理失败")
            finally:
                self._post_response_queue.task_done()

    def _enqueue_maintenance(self, event: TurnIngested) -> None:
        queue = self._maintenance_queues.setdefault(event.session_key, deque())
        queue.append(event)
        if event.session_key in self._maintenance_tasks:
            return
        task = asyncio.create_task(
            self._run_maintenance_queue(event.session_key),
            name=f"memory-maintenance:{event.session_key}",
        )
        self._maintenance_tasks[event.session_key] = task

    async def _run_maintenance_queue(self, session_key: str) -> None:
        try:
            while True:
                queue = self._maintenance_queues.get(session_key)
                if not queue:
                    return
                event = queue.popleft()
                try:
                    await self._run_consolidation_serialized(event)
                except Exception:
                    # Session 已提交，维护失败只保留 cursor 供后续重试，不能终止其它
                    # Session 的独立任务，也不能回滚正常回复。
                    logger.exception("会话记忆归档失败: session_key=%s", session_key)
        finally:
            current = asyncio.current_task()
            if self._maintenance_tasks.get(session_key) is current:
                self._maintenance_tasks.pop(session_key, None)
            if not self._maintenance_queues.get(session_key):
                self._maintenance_queues.pop(session_key, None)

    def _build_turn_snapshot(self, event: Any) -> TurnIngested:
        def value(name: str, default: Any = "") -> Any:
            if isinstance(event, dict):
                return event.get(name, default)
            return getattr(event, name, default)

        session_key = str(value("session_key") or "").strip()
        if not session_key:
            raise ValueError("TurnCommitted.session_key 不能为空")
        user_message = str(value("input_message") or value("user_message") or "")
        assistant_response = str(value("assistant_response") or "")
        tool_chain = value("tool_chain_raw", value("tool_chain", []))
        user_id = str(value("user_message_id") or "")
        assistant_id = str(value("assistant_message_id") or "")

        # Batch 5 的事件契约可以只携带持久化消息 ID。这里从 SessionStore 重建原文，
        # 保证后台 worker 读取的永远是提交成功的数据，而不是上游仍可能修改的对象。
        if (not user_message or not assistant_response) and (user_id or assistant_id):
            persisted = {item["id"]: item for item in self._sessions.fetch_by_ids([user_id, assistant_id])}
            user = persisted.get(user_id, {})
            assistant = persisted.get(assistant_id, {})
            user_message = user_message or str(user.get("content") or "")
            assistant_response = assistant_response or str(assistant.get("content") or "")
            if not tool_chain:
                tool_chain = assistant.get("tool_chain") or []

        safe_tool_chain = copy.deepcopy(tool_chain) if isinstance(tool_chain, list) else []
        source_ref = str(value("source_ref") or "")
        if not source_ref:
            source_ref = ",".join(item for item in (user_id, assistant_id) if item)
        channel = str(value("channel") or "")
        chat_id = str(value("chat_id") or "")
        if not channel and ":" in session_key:
            channel, derived_chat_id = session_key.split(":", 1)
            chat_id = chat_id or derived_chat_id
        return TurnIngested(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            user_message=user_message,
            assistant_response=assistant_response,
            tool_chain=safe_tool_chain,
            source_ref=source_ref or session_key,
        )

    async def optimize(self) -> dict[str, int]:
        return await self._optimizer.optimize()

    async def close(self) -> None:
        if self._closed:
            return
        self._accepting = False
        self.unbind_events()
        # 先完成已接收任务，再取消等待新消息的常驻消费者；否则 SQLite/Markdown
        # 可能先关闭，造成尾部 Turn 丢失。取消仅发生在队列清空之后。
        await self.drain()
        tasks = [task for task in (self._post_response_task,) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._maintenance_locks.clear()
        # 最后关闭本地数据库和 Embedder HTTP 客户端；Provider 由应用组装层共享。
        self._store.close()
        self._markdown.close()
        close = getattr(self._embedder, "close", None)
        if close is not None:
            await close()
        self._closed = True


class _LLMConsolidationExtractor:
    def __init__(self, provider: Any) -> None:
        self._provider = provider

    async def extract(self, messages: list[dict[str, object]], previous_recent_context: str, *, recent_turns: str = "", current_memory: str = "") -> ConsolidationDraft:
        conversation = render_consolidation_conversation(messages)
        # 两个提取器只读取相同证据，不互相消费结果；并发执行可把两次 LLM
        # 网络等待从相加改为取较慢者，任一失败时 TaskGroup 会取消另一分支。
        async with asyncio.TaskGroup() as group:
            event_task = group.create_task(
                complete_forced_function(
                    self._provider,
                    _event_extraction_prompt(
                        conversation,
                        current_memory=current_memory,
                    ),
                    CONSOLIDATION_EVENTS_TOOL,
                    required_arrays=("history_entries", "pending_items"),
                )
            )
            recent_task = group.create_task(
                complete_forced_function(
                    self._provider,
                    _recent_context_prompt(
                        conversation,
                        previous_recent_context,
                        recent_turns=recent_turns,
                    ),
                    RECENT_CONTEXT_TOOL,
                    required_arrays=(
                        "active_topics",
                        "user_preferences",
                        "follow_ups",
                        "avoidances",
                        "dormant_threads",
                        "ongoing_threads",
                    ),
                )
            )
        event_data = event_task.result()
        recent_data = recent_task.result()
        history_entries = []
        for item in event_data.get("history_entries") or []:
            if not isinstance(item, dict) or not str(item.get("summary") or "").strip():
                continue
            history_entries.append({
                "summary": str(item["summary"]).strip(),
                "emotional_weight": _emotional_weight(item.get("emotional_weight")),
            })
        allowed_tags = {"identity", "preference", "key_info", "health_long_term", "requested_memory", "correction", "agent_context"}
        pending_items = [
            {"tag": str(item.get("tag")), "content": str(item.get("content") or "").strip()}
            for item in event_data.get("pending_items") or []
            if isinstance(item, dict)
            and str(item.get("tag")) in allowed_tags
            and str(item.get("content") or "").strip()
        ]
        return ConsolidationDraft(
            history_entries=history_entries,
            pending_items=pending_items,
            recent_context=_render_recent_context(recent_data),
        )

def _event_extraction_prompt(conversation: str, *, current_memory: str = "") -> str:
    memory_section = ""
    if current_memory.strip():
        memory_section = f"""当前用户档案（用于查重）：
{current_memory}

"""
    return f"""你是记忆提取代理（Memory Extraction Agent）。从对话中精确提取结构化信息，返回 JSON。

## 字段说明

### 1. "history_entries" → 记忆事件条目（数组，每条对应一个独立主题）
按主题拆分，每个独立话题写一条对象，格式为 {{"summary":"...", "emotional_weight":0}}。
summary 要求 1-2 句，以 [YYYY-MM-DD HH:MM] 开头，保留足够细节便于后续向量写入和回源判断。
不同主题必须拆成独立条目，不得合并。若整段对话只有一个主题，返回只含一条的数组。

history_entries.emotional_weight 规则：
- 范围 0-10
- 普通技术讨论、普通事务记录、无明显情绪色彩 → 0
- 用户明确表达强烈喜欢/厌恶、明显受挫、关系冲突、情绪波动时按强度给 3-9
- 不确定时保守输出 0

**history_entries 提取规则（严格遵守）**：
1. 只提取 USER 明确表达的行动、经历、计划和状态；ASSISTANT 的建议、推荐、解释一律不写入，即使其中提到了地名、店名或活动。
2. 每条必须是简洁的第三人称摘要句，绝对不能包含 "USER:" 或 "ASSISTANT:" 等原始对话标记，不得复制粘贴原始对话文本。
3. 商家名称、地点、人名、数量、价格、型号等具体细节必须保留，不得用"某商店""某地方"概括。
4. 先判断当前 USER 内容的材料类型：是"用户此刻直接自述"，还是"用户正在展示一段外部聊天记录、截图 OCR、转贴 transcript 给助手看"。
5. 若 USER 内容属于外部聊天记录 / transcript，必须先做层级理解：
   - 外层：当前 USER 正在把一段材料发给助手看。
   - 内层：材料中可能有多个 speaker；这些 speaker 不自动等于当前 USER。
   - 只有当材料中某个 speaker 与当前 USER 的映射在当前会话里被明确确认时，才允许把该 speaker 的事实写入摘要。
6. 对 transcript 场景，默认认为 speaker 映射不明确；除非当前会话中有非常明确的显式说明，否则不要尝试判断材料里的某个昵称/说话人就是用户或对方。
7. 若 speaker 映射不明确，history_entries 只允许写 1 条高层 event，例如"用户向助手展示了一段与某人的聊天记录，内容涉及求职、学校、兴趣等话题"。
8. 对 transcript 场景，禁止输出任何未确认关系的句子，例如：
   - "用户向对方透露……"
   - "对方是……"
   - "双方确认……"
   - 把聊天记录里的具体事实直接写成用户个人经历
9. transcript 场景下，默认最多输出 1 条高层 history_entry；不要下钻成人物小传，不要替材料里的 speaker 自动补全身份关系。

**transcript 场景示例（严格遵守）**：
- 错误：用户贴出一段聊天记录，speaker 归属未确认，却写成"用户向对方透露自己正在找暑期实习"。
- 错误：用户贴出一段聊天记录，直接写成"对方位于北京大兴区，就读于二外 MPAcc 专业"。
- 错误：用户贴出一段聊天记录，直接写成"对方昵称为'一只快乐的小奶龙'"。
- 错误：用户贴出一段聊天记录，直接写成"用户曾为打 FGO 日服选修日语"。
- 正确：用户向助手展示了一段与匹配对象的聊天记录，聊天内容涉及学校背景、兴趣爱好和求职话题。

### 2. "pending_items" → PENDING.md 候选缓冲
只写用户的长期记忆候选，返回对象数组。每个对象格式：
{{"tag": "<tag>", "content": "<string>"}}

允许的 tag 只有 7 个：
- "identity"：稳定背景事实，如身份、学校/专业、长期技术方向、实习/工作经历、长期设备、长期维护项目
- "preference"：稳定偏好、禁忌、审美、游戏口味、价值取向
- "key_info"：用户明确允许保存的 key / token / id / 账号信息
- "health_long_term"：长期健康状态的一阶事实，只写长期状态，不写动态指标、基线、最近波动
- "requested_memory"：用户明确要求"长期记住"的关键内容，可比普通事实更连贯
- "correction"：对当前 MEMORY.md 现有事实的明确纠正
- "agent_context"：助手操作用户环境所需的工具性配置，如已部署服务的端口、环境变量名、工具分工约定、常用登录站点列表；不是用户画像，但对助手执行操作有长期价值；具体参数（端口号、变量名）必须完整保留。**硬规则：只有当对话明确表明该配置当前有效且助手已被授权使用时才提取；方案讨论、架构设计、网络诊断中出现的端口和地址一律不提取**

必须遵守：
- 只写跨对话仍有长期价值的内容
- 不写 agent 执行规则、SOP、工具调用顺序、流程规范
- 不写短期状态、近期计划、日程、课表、一次性操作
- 不写动态健康数据、实时指标、最近状态
- 不写对话过程总结
- 不写 self_insights、行为规律总结、关系演进感悟
- "requested_memory" 只能在用户明确表达"记住这个 / 写进长期记忆 / 以后要能聊到 / 希望你记住"时使用

进阶过滤（四条硬规则，任一触发即不提取）：

1. **网络运维细节不提取**
内网 IP、路由模式（如"CGNAT""桥接模式""NAT"）、运营商名称、MAC 地址等网络层配置属于瞬时运维信息，不提取。项目路径、配置文件名、环境变量名等与用户开发环境直接相关的信息可以提取。
✗ "家庭网络是联通宽带，光猫路由模式，内网 IP 192.168.1.x" → 不提取（网络层瞬时配置）
✓ "项目位于 /home/user/project，配置文件 config.toml" → 可提取（开发环境画像）

2. **临时状态不提取，规律习惯可提取**
带"最近""这周""目前""正在"等时间限定词的瞬时状态不提取。每周/每天持续的规律性行为模式可以提取为偏好或习惯标识。
✗ "用户最近加班频繁，靠咖啡撑着" → 不提取（瞬时状态，随时会变）
✓ "用户每周去健身房，主要做力量训练" → 可提取（规律性习惯，是长期生活方式）

3. **时效性数字和瞬时情绪不提取**
带有具体数值的动态指标（如 Star 数、增长率、评分）、瞬时情绪描述（如"失落""焦虑"）、正在进行中的短期状态。保留背后的价值判断，不提取数字和情绪本身。
✗ "项目刚突破 500 Star，但增速降到每天 2 个，用户为此很焦虑" → 不提取（数字过期、情绪瞬时）
✓ "用户长期维护某开源项目并重视社区增长" → 可提取（稳定身份信息）

4. **Agent 执行规则不放入 pending_items**
以"偏好"开头但语义上描述 agent 应如何执行的内容（如检索策略、元数据标注规范、输出格式要求等），属于 procedure，应由隐式提取路径写入向量库。
✗ "偏好搜索结果按来源可信度分层展示" → 不提取为 pending_item（agent 输出规范）
✗ "希望以后推荐前先查最新评测和社区反馈" → 不提取为 pending_item（agent 执行规则）

5. **agent_context 只提取已部署的配置，不提取方案讨论**
判断标准：对话中是否明确表明该服务/工具**当前已在运行**，且助手**已被告知可以使用**。
对话中提出的架构方案、网络诊断信息、假设性配置，即使出现了具体端口、地址或变量名，也不提取。

{memory_section}待处理对话：
{conversation}

完成判断后必须且只能调用 submit_consolidation_events；不得通过普通正文返回结果。
没有符合条件的内容时也必须调用，并将 history_entries 和 pending_items 设为空数组。"""


def _recent_context_prompt(conversation: str, old_context: str, *, recent_turns: str = "") -> str:
    return f"""你是近期语境压缩代理。你的任务不是自由总结，而是为后续上下文中保守地抽取近期语境。

目标：
1. 提取用户最近持续关注的话题
2. 提取最近新暴露、但尚未沉淀为长期记忆的显式偏好
3. 提取最近适合自然续接的话题
4. 提取最近应避免打扰、应避免推荐、或明显不想聊的方向
5. 提取跨窗口持续存在的重要现实线索（ongoing_threads）
6. 提取已经离开最新主线但仍可能被用户回头追问的会话内旧话题（dormant_threads）

规则：
- 只允许依据 USER 明确表达过的内容输出；ASSISTANT 的建议、解释、命名、延伸，一律不得当作证据
- recent_topics 可以总结“用户最近在讨论什么”，但必须贴近 USER 原话，不得升级成长期偏好
- active_topics 和 follow_ups 要优先写“话题层级”的概括，不要写 JSON Schema、函数名、字段名、具体术语翻译这类实现细节，除非用户明确把该细节当作核心关注点反复强调
- user_preferences 只允许在 USER 出现明确偏好/要求/禁忌表达时输出，例如：喜欢、偏好、希望、别、不要、避免、不想
- 不要把技术方案讨论、架构设想、问题求证、头脑风暴自动写成“用户偏好”
- 对技术讨论场景，只有当 USER 明确表达“以后都这样做 / 我就是偏好这种方式 / 我不要另一种方式 / 以后统一按这个来”时，才允许写 user_preferences；否则一律视为 active_topics 或 follow_ups
- 用户用“为什么不……”“能不能……”“是不是可以……”“只要不是最后一轮就……”这类方式提出方案设想或追问时，默认视为设计提议，不视为稳定偏好
- avoidances 只允许在 USER 明确表达“不要/别/避免/不想”时输出；没有明确否定表达就留空
- 如果最新 recent turns 显示话题已经明显切换，不要把较早窗口的技术讨论升级成当前偏好或避免事项
- 只保留未来几轮仍会影响辅助决策的信息
- 不要记录工具细节、推理过程、普通寒暄
- active_topics、user_preferences、follow_ups、avoidances 和 ongoing_threads 最多 3 条，每条尽量 1 句
- dormant_threads 最多 5 条；最新话题切换时，旧的普通会话话题优先降级到 dormant_threads，而不是直接删除
- 没有把握就留空；宁可漏掉，也不要脑补

ongoing_threads 严格限制：
- 只记录用户正在经历、推进或承受的重要事情
- 必须是对用户当前生活、情绪、工作、学习、关系或健康有持续影响的线索
- 普通提问、技术讨论、方案脑暴、一次性 ask、知识求证，一律不得写入 ongoing_threads
- 若旧的 ongoing_threads 中已有某条重要线索，而当前窗口没有明确终结它，默认保留
- 只有当用户明确表示这件事已解决、结束、过去了、不再关心，才允许删除
- ongoing_threads 的写入门槛高于 active_topics；宁可少写，也不要把普通话题升级进去

专项禁令：
- 用户讨论“某个设计有没有依据/有没有实践/是否可行/为什么不这样做”，这是方案讨论，不是偏好；默认只能进入 active_topics 或 follow_ups，不能进入 user_preferences
- 用户说“为什么不让前台……只要不是最后一轮就……”是在提出一种实现设想，不等于“用户偏好以后统一这样做”
- 用户说“这样也不会引入额外延迟”“有没有这样的设计”，这是在分析方案目标，不等于稳定偏好
- 用户讨论“零延迟”“预加载”“流式预取”“前瞻性检索”这类设计目标时，默认视为当前方案讨论，不得直接提炼成 user_preferences
- 对方案讨论里的具体实现细节，优先上收一层概括，例如写“下一轮检索规划”“流式预取方案”，不要写“JSON Schema”“结构化预取指令”这类细碎实现点
- 用户说“睡觉了”“头有点疼”“身体不适”，这只是当前状态；除非用户明确说“别再聊这个”“不要继续”“我不想讨论”，否则不得生成 avoidances
- assistant 说“今晚先别想架构和代码了”“先休息”，这是 assistant 建议，不是用户 avoidances
- 如果较早窗口是技术方案讨论，而最新 recent turns 已切到睡眠/头痛/身体状态，则 user_preferences 和 avoidances 默认应为空；技术方案最多保留在 active_topics / follow_ups
- “最近在讨论前瞻性检索/流式预取方案”只能进入 active_topics / follow_ups，不能进入 ongoing_threads
- “用户最近几天反复因面试失败而情绪低落”“用户近期持续受睡眠紊乱影响”这类重要现实线索，才允许进入 ongoing_threads

反例：
- 错误：把“在 React 过程中同时输出下一轮检索内容”写成“用户偏好在对话中实时生成下一轮检索指令”
- 错误：把“这样也不会引入额外延迟”写成“用户偏好零延迟预加载”
- 错误：把“为什么不让前台在进行时同时输出自己想要什么”写成“用户偏好实时生成下一轮检索指令”
- 错误：把“睡觉了，吃了褪黑素头有点疼”写成“避免在身体不适时继续讨论技术架构”
- 错误：把“最近在讨论 React / 流式预取方案”写成 ongoing_threads
- 正确：active_topics 可写“用户最近在讨论前瞻性检索/流式预取方案”
- 正确：ongoing_threads 可写“用户最近几天反复提到面试受挫，持续影响情绪”
- 正确：如果用户没有明确说“希望/不要/避免/不想”，user_preferences 和 avoidances 可以为空

输出前自检：
1. 检查 user_preferences 中每一条，是否都能在 USER 原话里找到明确偏好/要求词（如“希望/不要/避免/不想/偏好/喜欢”）
2. 若找不到明确偏好/要求词，删除该条
3. 检查 avoidances 中每一条，是否都能在 USER 原话里找到明确否定/回避表达
4. 若找不到明确否定/回避表达，删除该条
5. 如果删除后为空，返回空数组，不要为了“信息完整”硬填

【上一版 recent context（仅供延续，不要机械复述）】
{old_context or '（空）'}

【较早窗口（本次待压缩）】
{conversation or '（空）'}

【最新 Recent Turns（仅用于判断话题是否切换）】
- `[user]` 表示近期用户消息，只用于判断近期正在讨论什么，以及话题是否已经切换
- `[a-preview]` 表示 ASSISTANT 回复的截断预览，内容可能不完整，也不代表用户立场
- Recent Turns 中的任何内容都不能替代待压缩窗口中的 USER 原文
- 严禁将 `[a-preview]` 作为用户身份、事实、偏好、关系、回避事项或 ongoing thread 的证据
{recent_turns or '（空）'}

完成判断后必须且只能调用 submit_recent_context；不得通过普通正文返回结果。
没有符合条件的内容时也必须调用，参数为：
{{"active_topics": [], "user_preferences": [], "follow_ups": [], "avoidances": [], "dormant_threads": [], "ongoing_threads": []}}"""


def _render_recent_context(data: dict[str, object]) -> str:
    labels = (
        ("active_topics", "最近持续关注"),
        ("user_preferences", "最近明确偏好"),
        ("follow_ups", "最近待延续话题"),
        ("avoidances", "最近避免事项"),
    )
    lines = ["# Recent Context", "", "## Compression"]
    for key, label in labels:
        values = data.get(key)
        items = [str(item).strip() for item in values if str(item).strip()][:3] if isinstance(values, list) else []
        lines.append(f"- {label}：{'；'.join(items) if items else 'none'}")
    lines.extend(["", "## Dormant Threads"])
    dormant = data.get("dormant_threads")
    items = [str(item).strip() for item in dormant if str(item).strip()][:5] if isinstance(dormant, list) else []
    lines.extend(f"- {item}" for item in items)
    if not items:
        lines.append("- none")
    lines.extend(["", "## Ongoing Threads"])
    ongoing = data.get("ongoing_threads")
    items = [str(item).strip() for item in ongoing if str(item).strip()][:3] if isinstance(ongoing, list) else []
    lines.extend(f"- {item}" for item in items)
    if not items:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def _scope(channel: str, chat_id: str):
    from memory.contracts import MemoryScope
    return MemoryScope(session_key=f"{channel}:{chat_id}" if channel and chat_id else "", channel=channel, chat_id=chat_id)


def _format_recent_history(messages: list[dict[str, Any]], *, max_chars: int = 4000) -> str:
    """将近期消息压缩为 query rewrite 可读文本，并从尾部限制字符预算。"""

    lines = [
        f"{str(item.get('role') or '').upper()}: {str(item.get('content') or '').strip()}"
        for item in messages
        if str(item.get("content") or "").strip()
    ]
    return "\n".join(lines)[-max(1, int(max_chars)):]


def _string_list(value: object) -> list[str]:
    """只接受显式字符串列表，防止工具上下文把任意对象传入检索器。"""

    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _emotional_weight(value: object) -> int:
    """容忍 LLM 的字符串或越界输出，避免一处非关键字段阻塞整个归档窗口。"""

    try:
        return max(0, min(int(value or 0), 10))
    except (TypeError, ValueError):
        return 0


def _consolidation_payload(event: ConsolidationCommitted) -> dict[str, object]:
    return {
        "history_entry_payloads": [list(item) for item in event.history_entry_payloads],
        "source_ref": event.source_ref,
        "scope_channel": event.scope_channel,
        "scope_chat_id": event.scope_chat_id,
        "conversation": event.conversation,
    }


def _consolidation_from_payload(payload: dict[str, object]) -> ConsolidationCommitted:
    raw_entries = payload.get("history_entry_payloads")
    entries: list[tuple[str, int]] = []
    if isinstance(raw_entries, list):
        for item in raw_entries:
            if isinstance(item, list) and len(item) == 2:
                entries.append((str(item[0]), _emotional_weight(item[1])))
    return ConsolidationCommitted(
        history_entry_payloads=entries,
        source_ref=str(payload.get("source_ref") or ""),
        scope_channel=str(payload.get("scope_channel") or ""),
        scope_chat_id=str(payload.get("scope_chat_id") or ""),
        conversation=str(payload.get("conversation") or ""),
    )


def _injection_block(records: list[MemoryRecord]) -> str:
    if not records:
        return ""
    groups = {"procedure": [], "preference": [], "event": [], "profile": []}
    for record in records:
        line = f"- [{record.id}] {record.summary}"
        if record.kind == "procedure" and record.signals.get("tool_requirement"):
            line += f"（必须调用工具：{record.signals['tool_requirement']}）"
        groups.setdefault(record.kind, []).append(line)
    sections = []
    if groups["procedure"]: sections.append("## 【强制约束】记忆规则（必须执行）\n" + "\n".join(groups["procedure"]))
    if groups["preference"]: sections.append("## 【流程规范】用户偏好与规则\n" + "\n".join(groups["preference"]))
    history = [*groups["event"], *groups["profile"]]
    if history: sections.append("## 【相关历史】与当前用户的过往信息\n" + "\n".join(history))
    return "\n\n".join(sections)


def _record_from_item(item: dict[str, object]) -> MemoryRecord:
    """把存储层条目转换为稳定的记忆记录，并保留原始消息证据。"""

    source_ref = str(item.get("source_ref") or "")
    evidence = [
        EvidenceRef(
            kind="message",
            refs=[source_ref],
            source_ref=source_ref,
        )
    ] if source_ref else []
    extra = item.get("extra_json")
    return MemoryRecord(
        id=str(item["id"]),
        kind=str(item.get("memory_type") or "event"),
        summary=str(item.get("summary") or ""),
        score=float(item.get("score") or 0),
        evidence=evidence,
        signals=extra if isinstance(extra, dict) else {},
    )


def _should_require_scope_match(request: MemoryQuery) -> bool:
    """默认共享 Workspace 长期记忆，仅为显式诊断请求启用来源会话过滤。"""

    scope = request.scope
    has_scope = bool(scope.channel and scope.chat_id)
    # channel/chat_id 描述记忆来源而非所有者；Web 新建聊天会更换 chat_id，
    # 因此默认过滤会让同一 Workspace 的长期记忆无法跨会话使用。
    return has_scope and bool(request.context.get("require_scope_match", False))


def _tool_profile() -> MemoryToolProfile:
    recall = MemoryToolSpec(
        "检索用户长期记忆中的历史事实、经历、偏好和既往处理记录。用户明确询问以前说过、做过或发生过的内容，要求列举、汇总、确认历史，或自动注入不足以可靠回答时调用；通用知识、闲聊和仅凭当前对话即可回答的问题不要调用。answer 用于深度主题检索，timeline 用于按时间回顾。结果不足时不得编造；使用结果时必须输出引用标记。",
        {"type": "object", "properties": {"query": {"type": "string", "description": "写成脱离当前对话也能理解的检索主题，保留关键人物、时间、型号和事件。"}, "intent": {"type": "string", "enum": ["answer", "timeline"], "default": "answer", "description": "answer=深度主题检索；timeline=按 time_filter 回顾历史事件。"}, "memory_kind": {"type": "string"}, "time_filter": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, "required": ["query"]},
    )
    memorize = MemoryToolSpec(
        "记住用户明确要求长期保留的信息、稳定偏好或可复用流程；不要记录普通闲聊。",
        {"type": "object", "properties": {"summary": {"type": "string"}, "memory_kind": {"type": "string", "enum": ["event", "profile", "preference", "procedure"]}, "tool_requirement": {"type": "string"}, "steps": {"type": "array", "items": {"type": "string"}}}, "required": ["summary"]},
    )
    forget = MemoryToolSpec(
        "按 recall_memory 返回的记忆 ID 软删除错误、过时或用户要求遗忘的条目。",
        {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}}}, "required": ["ids"]},
    )
    return MemoryToolProfile(recall=recall, memorize=memorize, forget=forget)


__all__ = ["MemoryEngine"]
