"""BeanAgent 长期记忆模块统一入口与生命周期管理。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.config_models import MemoryConfig
from memory.consolidator import ConsolidationDraft, ConsolidationExtractor, ConsolidationResult, Consolidator
from memory.contracts import (
    EvidenceRef, MemoryMutation, MemoryMutationResult, MemoryQuery, MemoryQueryResult,
    MemoryRecord, MemoryToolProfile, MemoryToolSpec,
)
from memory.md_store import MarkdownMemoryStore
from memory.memorizer import Memorizer
from memory.optimizer import MemoryOptimizer
from memory.query_rewriter import QueryRewriter
from memory.retriever import Retriever
from memory.store import MemoryStore2
from session.store import SessionStore


class MemoryEngine:
    """组装记忆读写、归档和工具能力，不持有共享 LLMProvider 的所有权。"""

    def __init__(self, workspace: Path, embedder: Any, provider: Any, sessions: SessionStore, *, config: MemoryConfig | None = None, consolidation_extractor: ConsolidationExtractor | None = None, keep_count: int = 20, consolidation_threshold: int | None = None) -> None:
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
        self._closed = False

    async def query(self, request: MemoryQuery) -> MemoryQueryResult:
        scope = request.scope
        items = await self._retriever.retrieve(
            request.text,
            memory_types=list(request.filters.kinds) or None,
            top_k=request.limit,
            scope_channel=scope.channel or None,
            scope_chat_id=scope.chat_id or None,
            require_scope_match=bool(scope.channel and scope.chat_id),
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
        metadata.update({"scope_channel": mutation.scope.channel, "scope_chat_id": mutation.scope.chat_id})
        result = await self._memorizer.save_item_with_supersede(
            summary, actual_kind, metadata, mutation.source_ref,
            emotional_weight=int(metadata.get("emotional_weight", 0) or 0),
            supersede_threshold=self._config.dedup.supersede_threshold,
        )
        status, item_id = result.split(":", 1)
        return MemoryMutationResult(item_id=item_id, status=status, actual_kind=actual_kind)

    def tool_profile(self) -> MemoryToolProfile:
        return _tool_profile()

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
        result = await self.query(MemoryQuery(" ".join(queries), scope=_scope(channel, chat_id)))
        return _injection_block(result.records)

    async def on_turn_committed(self, event: Any) -> ConsolidationResult | None:
        result = await self._consolidator.consolidate(str(event.session_key))
        if result is None:
            return None
        channel = str(getattr(event, "channel", "") or "")
        chat_id = str(getattr(event, "chat_id", "") or "")
        for index, entry in enumerate(result.history_entries):
            summary = str(entry.get("summary") or "").strip()
            if summary:
                await self._memorizer.save_from_consolidation(
                    summary, [], f"{result.source_ref}#{index}", channel, chat_id,
                    emotional_weight=int(entry.get("emotional_weight", 0) or 0),
                )
        return result

    async def optimize(self) -> dict[str, int]:
        return await self._optimizer.optimize()

    async def close(self) -> None:
        if self._closed:
            return
        # 先关闭本地数据库，最后关闭 Embedder HTTP 客户端；Provider 由应用组装层共享。
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
        response = await self._provider.complete([{"role": "user", "content": "从对话提取 JSON：history_entries、pending_items、recent_context。不要编造。\n" + conversation + "\n旧近期语境：\n" + previous_recent_context}], tools=[])
        text = str(getattr(response, "content", response) or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Consolidation LLM 必须返回 JSON object")
        return ConsolidationDraft(
            history_entries=list(data.get("history_entries") or []),
            pending_items=list(data.get("pending_items") or []),
            recent_context=str(data.get("recent_context") or ""),
        )


def _scope(channel: str, chat_id: str):
    from memory.contracts import MemoryScope
    return MemoryScope(session_key=f"{channel}:{chat_id}" if channel and chat_id else "", channel=channel, chat_id=chat_id)


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
