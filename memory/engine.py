"""BeanAgent 长期记忆模块统一入口与生命周期管理。"""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent.config_models import MemoryConfig
from agent.context_budget import estimate_tokens
from memory.consolidator import (
    ConsolidationDraft,
    ConsolidationExtractor,
    render_consolidation_conversation,
)
from memory.compaction_worker import CompactionOutboxWorker
from memory.checkpoint import (
    flatten_units,
    group_logical_units,
    render_source_messages,
    select_compaction_units,
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
    complete_forced_function,
)
from memory.sufficiency_checker import should_enhance_retrieval
from session.store import SessionStore
from session.compaction import (
    canonical_digest,
    compaction_scope_id,
    compaction_source_ref,
)
from session.store import NewSurfaceEvent, SessionCompaction, SessionCompactionPrepare

if TYPE_CHECKING:
    from agent.event_bus import EventBus

logger = logging.getLogger(__name__)


class MemoryEngine:
    """组装记忆读写、归档和工具能力，不持有共享 LLMProvider 的所有权。"""

    def __init__(self, workspace: Path, embedder: Any, provider: Any, sessions: SessionStore, *, config: MemoryConfig | None = None, consolidation_extractor: ConsolidationExtractor | None = None, implicit_extractor: Any | None = None, keep_count: int | None = None, consolidation_threshold: int | None = None) -> None:
        # 旧参数只保留调用兼容性，明确不参与 checkpoint 选择或 token gate；压缩
        # 的唯一触发入口是 Pipeline 根据 Provider token 预算调用 compact_for_context。
        del keep_count, consolidation_threshold
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
        dedup_config = self._config.dedup
        self._memorizer = Memorizer(
            self._store,
            embedder,
            provider=provider,
            candidate_thresholds={
                "event": dedup_config.event_candidate_threshold,
                "profile": dedup_config.profile_candidate_threshold,
                "preference": dedup_config.preference_candidate_threshold,
                "procedure": dedup_config.procedure_candidate_threshold,
            },
            candidate_top_k=dedup_config.candidate_top_k,
        )
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
        self._consolidation_extractor = consolidation_extractor or _LLMConsolidationExtractor(provider)
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
        # checkpoint 副作用使用独立 worker；当前 Turn 只等待 summary 与 ledger commit，
        # 记忆提取和向量/Markdown 写入不会占住当前请求的等待链。
        self._compaction_worker = CompactionOutboxWorker(
            self._process_compaction_payload,
            max_concurrency=2,
        )
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
        if actual_kind == "procedure":
            metadata["rule_schema"] = build_procedure_rule_schema(
                summary,
                tool_requirement=str(metadata.get("tool_requirement") or "") or None,
                steps=[str(step) for step in metadata.get("steps", [])] if isinstance(metadata.get("steps"), list) else [],
                rule_schema=metadata.get("rule_schema") if isinstance(metadata.get("rule_schema"), dict) else None,
            )
        # scope 仅作为来源审计字段；默认检索仍是 workspace 级，不强制按会话过滤。
        metadata["scope_channel"] = mutation.scope.channel
        metadata["scope_chat_id"] = mutation.scope.chat_id
        result = await self._memorizer.save_item_with_supersede(
            summary, actual_kind, metadata, mutation.source_ref,
            emotional_weight=int(metadata.get("emotional_weight", 0) or 0),
        )
        status, item_id = result.split(":", 1)
        return MemoryMutationResult(item_id=item_id, status=status, actual_kind=actual_kind)

    def tool_profile(self) -> MemoryToolProfile:
        return _tool_profile()

    def read_self(self) -> str:
        """供 PromptBlock 读取自我认知，不暴露 Markdown Store 所有权。"""

        return self._markdown.read_self()

    def read_bean(self) -> str:
        """供 PromptBlock 读取 workspace 人格真源。"""

        return self._markdown.read_bean()

    def get_memory_context(self) -> str:
        """返回可直接注入稳定 Prompt 前缀的 Markdown 长期记忆。"""

        return self._markdown.get_memory_context()

    def read_checkpoint_summary(self, session_key: str = "") -> str:
        """读取 active generation 摘要，作为下一轮的动态 system context。"""

        key = str(session_key or "").strip()
        if not key:
            return ""
        checkpoint = self._sessions.get_active_compaction(key)
        return checkpoint.summary if checkpoint is not None else ""

    async def compact_for_context(
        self,
        session_key: str,
        *,
        estimated_tokens: int = 0,
        force: bool = False,
    ) -> bool:
        """按 token gate 归档完整 logical units，并提交可恢复 checkpoint。

        该入口只处理已经落库的 closed Turns；当前正在推理的用户输入、ReAct
        tool batch 和未完成内容不会进入 durable source plan。
        """

        key = str(session_key or "").strip()
        if not key:
            raise ValueError("session_key 不能为空")
        meta = self._sessions.get_session_meta(key)
        if meta is None:
            return False
        boundary = self._sessions.get_active_message_boundary(key)
        raw_messages = [
            item
            for item in self._sessions.fetch_session_messages(key)
            if int(item.get("seq", -1)) >= boundary
        ]
        units = group_logical_units(raw_messages)
        if not units:
            return False
        context_window = max(0, int(getattr(self._provider, "context_window", 0) or 0))
        if context_window > 0:
            keep_recent_tokens = max(1, min(20_000, context_window // 5))
        else:
            # provider 容量未知时只有 force（通常来自实际超限）才尝试收敛，
            # 并按当前估算保留较小尾部，避免重新引入固定消息窗口。
            if not force:
                return False
            keep_recent_tokens = max(1, int(estimated_tokens or 1) // 5)
        selected_units, retained_units = select_compaction_units(
            units,
            keep_recent_tokens=keep_recent_tokens,
        )
        if not selected_units:
            return False

        generation = self._sessions.next_compaction_generation(key)
        scope_id = compaction_scope_id(key, str(meta["created_at"]))
        source_ref = compaction_source_ref(scope_id, generation)
        selected_messages = flatten_units(selected_units)
        retained_tail = flatten_units(retained_units)
        source_ids = [str(item["id"]) for item in selected_messages]
        source_from_seq = int(selected_messages[0]["seq"])
        consolidated_through_seq = int(selected_messages[-1]["seq"]) + 1
        source_plan = {
            "source_from_seq": source_from_seq,
            "consolidated_through_seq": consolidated_through_seq,
            "source_message_ids": source_ids,
            "retained_message_ids": [str(item["id"]) for item in retained_tail],
            "generation": generation,
        }
        source_plan_digest = canonical_digest(source_plan)
        source_mutation_digest = canonical_digest(selected_messages)
        previous_summary = self.read_checkpoint_summary(key)
        conversation = render_source_messages(selected_units)
        summary = await self._summarize_checkpoint(previous_summary, conversation)
        # 记录压缩后仍会送入模型的摘要与保留尾部，避免 tokens_after 永远为 0，
        # 也让诊断能区分“历史已归档”和“系统/工具静态开销”。
        tokens_after = estimate_tokens([
            {"role": "system", "content": summary},
            *retained_tail,
        ])
        now = _now_iso()
        prepare = SessionCompactionPrepare(
            session_key=key,
            session_created_at=str(meta["created_at"]),
            generation=generation,
            parent_generation=max(0, generation - 1),
            source_ref=source_ref,
            source_plan_digest=source_plan_digest,
            source_mutation_digest=source_mutation_digest,
            source_from_seq=source_from_seq,
            consolidated_through_seq=consolidated_through_seq,
            source_message_ids=source_ids,
            selected_source_messages=selected_messages,
            retained_tail=retained_tail,
            prepared_at=now,
        )
        self._sessions.prepare_compaction(prepare)
        checkpoint = SessionCompaction(
            session_key=key,
            session_created_at=str(meta["created_at"]),
            generation=generation,
            parent_generation=max(0, generation - 1),
            created_at=now,
            trigger="context_overflow" if force else "soft_limit",
            summary_format_version=1,
            summary=summary,
            source_ref=source_ref,
            source_plan_digest=source_plan_digest,
            source_mutation_digest=source_mutation_digest,
            source_from_seq=source_from_seq,
            consolidated_through_seq=consolidated_through_seq,
            source_message_ids=source_ids,
            selected_source_messages=selected_messages,
            retained_tail=retained_tail,
            model_runtime_id=str(getattr(self._provider, "model", "beanagent")),
            model=str(getattr(self._provider, "model", "")),
            context_window=context_window,
            threshold_tokens=(int(context_window * 0.74) if context_window else 0),
            hard_input_tokens=max(0, context_window - int(getattr(self._provider, "max_tokens", 0) or 0)),
            keep_recent_tokens=keep_recent_tokens,
            tokens_before=max(0, int(estimated_tokens)),
            tokens_after=max(0, int(tokens_after)),
            summary_usage={},
        )
        # 先把不可变 source plan 写入 durable outbox，再推进 checkpoint；worker 只在
        # ledger 成为 active generation 后消费，崩溃时不会丢失统一记忆提取任务。
        job_payload: dict[str, object] = {
            "kind": "checkpoint_memory",
            "source_ref": source_ref,
            "session_key": key,
            "generation": generation,
            "scope_channel": key.partition(":")[0],
            "scope_chat_id": key.partition(":")[2],
            "previous_summary": previous_summary,
            "selected_source_messages": [dict(item) for item in selected_messages],
        }
        self._store.enqueue_consolidation(source_ref, job_payload)
        # 这里才推进 active generation；之后的记忆副作用失败不能让原文重新回到
        # 模型窗口，也不能生成另一代重复覆盖同一 source plan。
        self._sessions.commit_compaction(checkpoint)
        self._replace_provider_surface(checkpoint)
        try:
            # submit 只把 durable job 放入内存队列，不等待 LLM/Embedding/Markdown。
            await self._compaction_worker.submit(job_payload)
        except Exception:
            # checkpoint 已 durable commit；outbox 仍可在启动恢复阶段重放。
            logger.exception("checkpoint outbox 调度失败: source_ref=%s", source_ref)
        return True

    def _replace_provider_surface(self, checkpoint: SessionCompaction) -> None:
        """把语义压缩映射成模型侧 surface 的单个 DSH 风格 replace 节点。"""

        nodes = self._sessions.load_surface_events(checkpoint.session_key)
        if not nodes:
            # 没有模型侧 surface 时只可能是非 Provider 的历史维护调用；语义
            # checkpoint 仍按原有逻辑提交，不凭空制造模型消息。
            return
        selected_turns = {
            str(item.get("turn_id") or "")
            for item in checkpoint.selected_source_messages
            if str(item.get("turn_id") or "")
        }
        selected = [
            node for node in nodes
            if str(node.get("turn_id") or "") in selected_turns
        ]
        if not selected:
            return
        first_index = next(index for index, node in enumerate(nodes) if node is selected[0])
        last_index = max(index for index, node in enumerate(nodes) if node is selected[-1])
        shadowed = nodes[first_index:last_index + 1]
        if not all(
            str(node.get("turn_id") or "") in selected_turns
            for node in shadowed
        ):
            raise ValueError("compaction surface replace 不能跨越未选中的 Turn")
        start = int(shadowed[0]["surface_seq"])
        end = int(shadowed[-1]["surface_seq"])
        epoch_id = str(shadowed[0].get("epoch_id") or "default")
        content = (
            "<session-context-compaction>\n"
            "以下是已归档历史的上下文摘要；与当前用户原文冲突时以原文为准。\n"
            f"{checkpoint.summary.strip()}\n"
            "</session-context-compaction>"
        )
        self._sessions.append_surface(NewSurfaceEvent(
            session_key=checkpoint.session_key,
            epoch_id=epoch_id,
            turn_id=f"compaction:{checkpoint.generation}",
            iteration=0,
            role="user",
            content={"role": "user", "content": content},
            source_kind="compaction_summary",
            operation_key=f"compaction:{checkpoint.source_ref}",
            status="replaced",
            surface_op="replace",
            replace_start=start,
            replace_end=end,
            source_event_seqs=[int(node["surface_seq"]) for node in shadowed],
        ))

    async def _summarize_checkpoint(self, previous_summary: str, conversation: str) -> str:
        """用上一代 summary + 本轮归档 source 生成稳定 checkpoint 摘要。"""

        prompt = _checkpoint_summary_prompt(previous_summary, conversation)
        complete = getattr(self._provider, "complete", None)
        if not callable(complete):
            return conversation[-12_000:]
        response = await complete(
            [
                {"role": "system", "content": "你是上下文压缩摘要器，只输出可持续的中文摘要。"},
                {"role": "user", "content": prompt},
            ],
            tools=[],
            max_tokens=min(8192, max(256, int(getattr(self._provider, "max_tokens", 8192) or 8192))),
            tool_choice="none",
            disable_thinking=True,
        )
        summary = str(getattr(response, "content", "") or "").strip()
        return summary or conversation[-12_000:]

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
        """将已持久化 Turn 快照投递给每轮记忆链；checkpoint 由 token gate 触发。"""

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

    def bind_events(self, event_bus: EventBus) -> None:
        """只订阅正式 TurnCommitted；checkpoint 副作用统一由 outbox worker 调度。"""

        from agent.event_bus import TurnCommitted

        if self._event_bus is event_bus:
            return
        self.unbind_events()
        event_bus.on(TurnCommitted, self.on_turn_committed)
        self._event_bus = event_bus

    def unbind_events(self) -> None:
        """关闭前解除订阅，避免已关闭的 Store 再收到提交事件。"""

        if self._event_bus is None:
            return
        from agent.event_bus import TurnCommitted

        self._event_bus.off(TurnCommitted, self.on_turn_committed)
        self._event_bus = None

    async def ensure_context_ready(self, session_key: str) -> bool:
        """兼容旧 ContextGuard API；token gate 已在 Pipeline 组装完整 payload 时执行。"""

        if not str(session_key or "").strip():
            raise ValueError("session_key 不能为空")
        return True

    def needs_context_preparation(self, session_key: str) -> bool:
        """旧 guard 保留为兼容探针，但不再根据消息数触发压缩。"""

        if not str(session_key or "").strip():
            raise ValueError("session_key 不能为空")
        return False

    async def on_consolidation_committed(self, event: ConsolidationCommitted) -> None:
        pending_lines = [
            f"- [{str(item.get('tag') or 'key_info')}] {str(item.get('content') or '').strip()}"
            for item in event.pending_items
            if isinstance(item, dict) and str(item.get("content") or "").strip()
        ]
        if pending_lines:
            self._markdown.append_pending_once(
                "\n".join(pending_lines),
                source_ref=event.source_ref,
            )
        # 新 checkpoint 把隐式候选随 outbox 一并保存，重放时不再重新调用 LLM；
        # 旧格式 outbox 没有该字段时才走兼容提取路径。
        implicit = _implicit_from_payload(event.implicit_memory)
        implicit_task = (
            asyncio.sleep(0, result=implicit)
            if event.implicit_memory
            else self._implicit_extractor.extract(event.conversation, existing_profile="")
        )
        # 事件向量写入与隐式记忆保存没有数据依赖，允许并发等待，完成后再清理 outbox。
        async with asyncio.TaskGroup() as group:
            group.create_task(self._save_consolidation_events(event))
            implicit_task_group = group.create_task(implicit_task)
        implicit = implicit_task_group.result()
        await self._save_implicit_long_term(implicit, event)
        self._store.complete_consolidation(event.source_ref)

    async def _process_compaction_payload(self, payload: dict[str, object]) -> bool:
        """消费一个 checkpoint source；提取失败时保留 outbox 供后续重放。"""

        if payload.get("kind") != "checkpoint_memory":
            # 兼容重构前已落盘的完整提取 payload；新任务不会走这条路径。
            event = _consolidation_from_payload(payload)
            if event.session_key and event.generation:
                checkpoint = self._sessions.get_compaction(
                    event.session_key,
                    event.generation,
                )
                if checkpoint is None or checkpoint.source_ref != event.source_ref:
                    # 没有对应 active checkpoint 的旧 payload 无法安全重建来源；它通常
                    # 来自 outbox 落盘后进程在 ledger commit 前退出，清理后等待新的
                    # token gate 重新生成合法 generation，不能把孤儿来源写入记忆。
                    self._store.complete_consolidation(event.source_ref)
                    return False
            await self.on_consolidation_committed(event)
            return True

        session_key = str(payload.get("session_key") or "").strip()
        source_ref = str(payload.get("source_ref") or "").strip()
        generation = max(0, int(payload.get("generation") or 0))
        if not session_key or not source_ref or generation <= 0:
            raise ValueError("checkpoint outbox payload identity 无效")
        checkpoint = self._sessions.get_compaction(session_key, generation)
        if checkpoint is None or checkpoint.source_ref != source_ref:
            # outbox 可能在 ledger commit 前落盘；不能把未激活 source 写入长期记忆。
            self._store.complete_consolidation(source_ref)
            return False

        selected_messages = [dict(item) for item in checkpoint.selected_source_messages]
        if not selected_messages:
            raise ValueError("checkpoint source plan 不能为空")
        conversation = render_consolidation_conversation(selected_messages)
        previous_summary = str(payload.get("previous_summary") or "")
        current_memory = await asyncio.to_thread(self._markdown.read_long_term)
        # 三种记忆提取共享同一份已提交 source；并发只发生在后台 worker 内，
        # 不再延长触发压缩的当前 Turn。
        draft, implicit = await asyncio.gather(
            self._consolidation_extractor.extract(
                selected_messages,
                previous_summary,
                current_memory=current_memory.strip(),
            ),
            self._implicit_extractor.extract(
                conversation,
                existing_profile="",
            ),
        )
        event = ConsolidationCommitted(
            history_entry_payloads=[
                (
                    str(entry.get("summary") or ""),
                    _emotional_weight(entry.get("emotional_weight")),
                )
                for entry in draft.history_entries
                if str(entry.get("summary") or "").strip()
            ],
            source_ref=source_ref,
            scope_channel=str(payload.get("scope_channel") or ""),
            scope_chat_id=str(payload.get("scope_chat_id") or ""),
            conversation=conversation,
            session_key=session_key,
            generation=generation,
            pending_items=[
                dict(item)
                for item in draft.pending_items
                if isinstance(item, dict)
            ],
            implicit_memory=_implicit_payload(implicit),
        )
        await self.on_consolidation_committed(event)
        return True

    async def _save_consolidation_events(
        self,
        event: ConsolidationCommitted,
    ) -> None:
        """顺序写入单个归档窗口的事件，控制 Embedding API 瞬时并发。"""

        batch = [
            (str(summary).strip(), f"{event.source_ref}#{index}", event.scope_channel, event.scope_chat_id, emotional_weight)
            for index, (summary, emotional_weight) in enumerate(event.history_entry_payloads)
            if str(summary or "").strip()
        ]
        save_batch = getattr(self._memorizer, "save_events_batch", None)
        if callable(save_batch):
            await save_batch(batch)
            return
        for index, (summary, emotional_weight) in enumerate(event.history_entry_payloads):
            summary = str(summary or "").strip()
            if summary:
                await self._memorizer.save_from_consolidation(
                    summary, [], f"{event.source_ref}#{index}", event.scope_channel, event.scope_chat_id,
                    emotional_weight=emotional_weight,
                )

    async def replay_pending_consolidations(self) -> None:
        """启动后台 worker 重放 durable outbox；启动阶段等待已有任务收敛。"""

        payloads = self._store.list_pending_consolidations()
        await self._compaction_worker.submit_many(payloads)
        await self._compaction_worker.drain()

    async def _save_implicit_long_term(
        self,
        draft: ImplicitMemoryDraft,
        event: ConsolidationCommitted,
    ) -> None:
        batches: list[list[tuple[str, str, dict[str, object], str, str | None, int]]] = []
        for memory_type, items in (
            ("profile", draft.profile),
            ("preference", draft.preference),
            ("procedure", draft.procedure),
        ):
            batch: list[tuple[str, str, dict[str, object], str, str | None, int]] = []
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
                    if memory_type == "procedure":
                        extra["scenario"] = str(item.get("scenario") or "")
                        extra["constraints"] = item.get("constraints") if isinstance(item.get("constraints"), list) else []
                    if memory_type == "procedure" and isinstance(item.get("rule_schema"), dict):
                        extra["rule_schema"] = item["rule_schema"]
                happened_at = item.get("happened_at") if isinstance(item.get("happened_at"), str) else None
                batch.append((
                    summary, memory_type, extra, f"{event.source_ref}#{memory_type}:{index}",
                    happened_at, _emotional_weight(item.get("emotional_weight")),
                ))
            if batch:
                batches.append(batch)
        save_batch = getattr(self._memorizer, "save_items_batch", None)
        if callable(save_batch):
            async with asyncio.TaskGroup() as group:
                for batch in batches:
                    group.create_task(save_batch(batch))
            return
        for batch in batches:
            for summary, memory_type, extra, source_ref, happened_at, emotional_weight in batch:
                await self._memorizer.save_item_with_supersede(
                    summary, memory_type, extra, source_ref,
                    happened_at=happened_at, emotional_weight=emotional_weight,
                )

    async def drain(self) -> None:
        """等待 Turn 后台任务和 checkpoint outbox 任务清空。"""

        await self._post_response_queue.join()
        await self._compaction_worker.drain()

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
        await self._compaction_worker.close(drain=False)
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

    async def extract(self, messages: list[dict[str, object]], previous_summary: str = "", *, current_memory: str = "") -> ConsolidationDraft:
        conversation = render_consolidation_conversation(messages)
        # 事件摘要与 PENDING 候选必须由同一个函数调用生成；profile、preference、
        # procedure 则在 checkpoint 中使用同一 conversation 进入隐式提取器。
        event_data = await complete_forced_function(
            self._provider,
            _event_extraction_prompt(
                conversation,
                current_memory=current_memory,
            ),
            CONSOLIDATION_EVENTS_TOOL,
            required_arrays=("history_entries", "pending_items"),
        )
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


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _checkpoint_summary_prompt(previous_summary: str, conversation: str) -> str:
    return f"""请更新上下文压缩摘要。

只依据材料中出现的事实，不补充推测；摘要代表已经归档的旧历史，不能写入当前用户
输入、未完成工具调用或尚未持久化的内容。保留目标、关键决定、已完成进展、阻塞点、
下一步和仍需记住的技术细节。使用简洁中文 Markdown，上一代摘要为空时从本轮 source
建立摘要；上一代摘要非空时要合并而不是机械复制。

【上一代 checkpoint.summary】
{previous_summary or "（空）"}

【本轮 selected_source_messages】
{conversation or "（空）"}
"""


def _consolidation_payload(event: ConsolidationCommitted) -> dict[str, object]:
    return {
        "history_entry_payloads": [list(item) for item in event.history_entry_payloads],
        "source_ref": event.source_ref,
        "scope_channel": event.scope_channel,
        "scope_chat_id": event.scope_chat_id,
        "conversation": event.conversation,
        "session_key": event.session_key,
        "generation": event.generation,
        "pending_items": [dict(item) for item in event.pending_items],
        "implicit_memory": {
            str(kind): [dict(item) for item in items]
            for kind, items in event.implicit_memory.items()
            if isinstance(items, list)
        },
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
        session_key=str(payload.get("session_key") or ""),
        generation=max(0, int(payload.get("generation") or 0)),
        pending_items=[dict(item) for item in payload.get("pending_items", []) if isinstance(item, dict)]
        if isinstance(payload.get("pending_items"), list)
        else [],
        implicit_memory={
            str(kind): [dict(item) for item in items if isinstance(item, dict)]
            for kind, items in payload.get("implicit_memory", {}).items()
            if isinstance(items, list)
        }
        if isinstance(payload.get("implicit_memory"), dict)
        else {},
    )


def _implicit_payload(draft: ImplicitMemoryDraft) -> dict[str, list[dict[str, object]]]:
    """把统一提取结果转为可 JSON 化 outbox，重放时不重新请求模型。"""

    return {
        "profile": [dict(item) for item in draft.profile],
        "preference": [dict(item) for item in draft.preference],
        "procedure": [dict(item) for item in draft.procedure],
    }


def _implicit_from_payload(payload: dict[str, list[dict[str, object]]]) -> ImplicitMemoryDraft:
    return ImplicitMemoryDraft(
        profile=[dict(item) for item in payload.get("profile", []) if isinstance(item, dict)],
        preference=[dict(item) for item in payload.get("preference", []) if isinstance(item, dict)],
        procedure=[dict(item) for item in payload.get("procedure", []) if isinstance(item, dict)],
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
