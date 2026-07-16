"""BeanAgent 长期记忆模块统一入口与生命周期管理。"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent.config_models import MemoryConfig
from memory.consolidator import ConsolidationDraft, ConsolidationExtractor, ConsolidationResult, Consolidator
from memory.contracts import (
    EvidenceRef, MemoryIngestRequest, MemoryIngestResult, MemoryMutation,
    MemoryMutationResult, MemoryQuery, MemoryQueryResult, MemoryRecord,
    MemoryToolProfile, MemoryToolSpec,
)
from memory.events import ConsolidationCommitted, TurnIngested
from memory.implicit_extractor import ImplicitLongTermExtractor, ImplicitMemoryDraft
from memory.md_store import MarkdownMemoryStore
from memory.memorizer import Memorizer
from memory.optimizer import MemoryOptimizer
from memory.post_response_worker import PostResponseMemoryWorker
from memory.query_rewriter import QueryRewriter
from memory.retriever import Retriever
from memory.store import MemoryStore2
from session.store import SessionStore

if TYPE_CHECKING:
    from agent.event_bus import EventBus

logger = logging.getLogger(__name__)


class MemoryEngine:
    """组装记忆读写、归档和工具能力，不持有共享 LLMProvider 的所有权。"""

    def __init__(self, workspace: Path, embedder: Any, provider: Any, sessions: SessionStore, *, config: MemoryConfig | None = None, consolidation_extractor: ConsolidationExtractor | None = None, implicit_extractor: Any | None = None, keep_count: int = 20, consolidation_threshold: int | None = None) -> None:
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
        self._optimizer = MemoryOptimizer(self._markdown, provider)
        self._consolidator = Consolidator(
            sessions, self._markdown,
            consolidation_extractor or _LLMConsolidationExtractor(provider),
            keep_count=keep_count, threshold=consolidation_threshold,
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
        scope = request.scope
        # 每轮预检索对齐 Akashic 的 context 语义：scope 作为来源信息传入检索器，
        # 但默认不作为过滤条件；显式查询 intent 的矩阵在独立边界中处理。
        require_scope_match = bool(scope.channel and scope.chat_id) and request.intent != "context"
        items = await self._retriever.retrieve(
            request.text,
            memory_types=list(request.filters.kinds) or None,
            top_k=request.limit,
            scope_channel=scope.channel or None,
            scope_chat_id=scope.chat_id or None,
            require_scope_match=require_scope_match,
            time_start=request.filters.time_start,
            time_end=request.filters.time_end,
        )
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
        return MemoryQueryResult(records=records, trace={"engine": "default", "intent": request.intent, "vector_keyword_fusion": True})

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

    def read_recent_context(self) -> str:
        """供动态 system-reminder 读取近期压缩上下文。"""

        return self._markdown.read_recent_context()

    async def retrieve_for_turn(self, message: Any) -> str:
        text = str(getattr(message, "content", getattr(message, "text", "")) or "")
        channel = str(getattr(message, "channel", "") or "")
        chat_id = str(getattr(message, "chat_id", "") or "")
        decision = await self._rewriter.decide(text, "")
        queries = [decision.episodic_query] if decision.needs_episodic else []
        if decision.procedure_query:
            queries.append(decision.procedure_query)
        if not queries:
            return ""
        result = await self.query(MemoryQuery(
            " ".join(queries),
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
        # outbox 必须先于事件派发持久化。cursor 已代表 Markdown 提交；若进程在派发前
        # 崩溃，下次 Turn 仍能从 SQLite 重放向量同步，而不会重新归档旧窗口。
        self._store.enqueue_consolidation(result.source_ref, _consolidation_payload(committed))
        if self._event_bus is not None:
            await self._event_bus.emit(committed)
        else:
            await self.on_consolidation_committed(committed)
        return result

    async def on_consolidation_committed(self, event: ConsolidationCommitted) -> None:
        for index, (summary, emotional_weight) in enumerate(event.history_entry_payloads):
            summary = str(summary or "").strip()
            if summary:
                await self._memorizer.save_from_consolidation(
                    summary, [], f"{event.source_ref}#{index}", event.scope_channel, event.scope_chat_id,
                    emotional_weight=emotional_weight,
                )
        implicit = await self._implicit_extractor.extract(event.conversation, existing_profile="")
        await self._save_implicit_long_term(implicit, event)
        self._store.complete_consolidation(event.source_ref)

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
        lock = self._maintenance_locks.setdefault(session_key, asyncio.Lock())
        try:
            async with lock:
                while True:
                    queue = self._maintenance_queues.get(session_key)
                    if not queue:
                        return
                    event = queue.popleft()
                    try:
                        await self._run_consolidation(event)
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
                self._maintenance_locks.pop(session_key, None)

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

    async def extract(self, messages: list[dict[str, object]], previous_recent_context: str) -> ConsolidationDraft:
        conversation = "\n".join(f"[{item.get('role')}] {item.get('content')}" for item in messages)
        event_data = await self._complete_json(_event_extraction_prompt(conversation, previous_recent_context))
        recent_data = await self._complete_json(_recent_context_prompt(conversation, previous_recent_context))
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

    async def _complete_json(self, prompt: str) -> dict[str, object]:
        response = await self._provider.complete([{"role": "user", "content": prompt}], tools=[])
        text = str(getattr(response, "content", response) or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Consolidation LLM 必须返回 JSON object")
        return data


def _event_extraction_prompt(conversation: str, recent_context: str) -> str:
    return f"""你是记忆提取代理。从对话中精确提取 history_entries 和 pending_items，只返回合法 JSON。

history_entries：每个独立主题一条，summary 用第三人称并以 [YYYY-MM-DD HH:MM] 开头，emotional_weight 为 0-10。
- 只写 USER 明确表达的行动、经历、计划和状态；ASSISTANT 的建议、解释和推荐不作证据。
- 保留地点、人名、数量、价格、型号等细节，不复制 USER:/ASSISTANT: 标记。
- 若 USER 展示外部聊天记录或 transcript，默认 speaker 身份不明确，只允许一条“用户展示了一段聊天记录”的高层 event；禁止把材料中的人物事实归给当前用户。

pending_items：只保存跨对话有价值的长期候选，格式为 {{"tag":"...","content":"..."}}。合法 tag：identity、preference、key_info、health_long_term、requested_memory、correction、agent_context。
- 不写 agent SOP、工具顺序、输出规范；这些属于 procedure。
- 不写最近/这周/目前等临时状态、日程、动态健康指标、Star 数、增长率和瞬时情绪。
- requested_memory 仅在 USER 明确要求长期记住时使用。
- agent_context 只保存已部署、当前有效且明确授权助手使用的配置；架构方案、网络诊断、内网 IP、路由模式和假设端口不提取。
- 工程路径、配置文件名、环境变量名可在明确长期有效时提取。

当前 RECENT_CONTEXT 只用于理解话题延续，不能作为身份、关系或事实归属证据；发生冲突时以当前窗口 USER 原文为准。
{recent_context or '（空）'}

待处理对话：
{conversation}

返回：{{"history_entries": [], "pending_items": []}}"""


def _recent_context_prompt(conversation: str, old_context: str) -> str:
    return f"""你是近期语境压缩代理，只返回合法 JSON，不自由总结。
只依据 USER 明确表达，提取 active_topics、user_preferences、follow_ups、avoidances、ongoing_threads，每项最多 3 条。
- user_preferences 必须能找到“喜欢/偏好/希望”等明确锚点；技术方案讨论、为什么不、能不能不算稳定偏好。
- avoidances 必须能找到“不要/别/避免/不想”等明确否定；ASSISTANT 建议不算。
- ongoing_threads 只保存持续影响生活、情绪、工作、学习、关系或健康的重要现实线索；普通技术讨论和一次性提问不进入。
- 话题已切换时，不把较早技术方案升级为偏好或避免事项。
- 宁可数组为空，也不要脑补。

上一版：
{old_context or '（空）'}

待压缩窗口：
{conversation}

返回：{{"active_topics": [], "user_preferences": [], "follow_ups": [], "avoidances": [], "ongoing_threads": []}}"""


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
        groups.setdefault(record.kind, []).append(f"- [{record.id}] {record.summary}")
    sections = []
    if groups["procedure"]: sections.append("## 【强制约束】记忆规则（必须执行）\n" + "\n".join(groups["procedure"]))
    if groups["preference"]: sections.append("## 【流程规范】用户偏好与规则\n" + "\n".join(groups["preference"]))
    history = [*groups["event"], *groups["profile"]]
    if history: sections.append("## 【相关历史】与当前用户的过往信息\n" + "\n".join(history))
    return "\n\n".join(sections)


def _tool_profile() -> MemoryToolProfile:
    recall = MemoryToolSpec(
        "检索用户长期记忆，返回带原始消息 evidence 的事件、偏好、画像和流程；使用结果时必须输出引用标记。",
        {"type": "object", "properties": {"query": {"type": "string"}, "intent": {"type": "string", "enum": ["context", "answer", "timeline", "interest", "procedure"]}, "memory_kind": {"type": "string"}, "time_filter": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, "required": ["query"]},
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
