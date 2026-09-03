"""记忆写入、强化、语义去重、合并与替代。"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Protocol

from memory.store import MemoryStore2
from memory.rule_schema import build_procedure_rule_schema, procedure_rules_conflict, resolve_procedure_rule_schema
from memory.write_decider import MemoryWriteDecider, MemoryWriteDecision

logger = logging.getLogger(__name__)

_TIME_PREFIX_RE = re.compile(
    r"^\[(?P<date>\d{4}-\d{2}-\d{2})(?:[ T](?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?\]"
)


class EmbeddingApi(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class Memorizer:
    """所有人工写入与 consolidation 写入共用的记忆变更边界。"""

    def __init__(
        self,
        store: MemoryStore2,
        embedder: EmbeddingApi,
        *,
        provider: Any,
        candidate_thresholds: dict[str, float] | None = None,
        candidate_top_k: int = 5,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._decider = MemoryWriteDecider(provider)
        self._candidate_thresholds = candidate_thresholds or {
            "event": 0.75, "profile": 0.55, "preference": 0.55, "procedure": 0.50,
        }
        self._candidate_top_k = max(1, int(candidate_top_k))

    async def save_item(self, summary: str, memory_type: str, extra: dict[str, object], source_ref: str, happened_at: str | None = None, emotional_weight: int = 0) -> str:
        embedding = await self._embedder.embed(summary)
        return self._store.upsert_item(
            memory_type, summary, embedding, source_ref,
            extra=extra, happened_at=happened_at,
            emotional_weight=_emotional_weight(emotional_weight),
        )

    async def save_item_with_supersede(self, summary: str, memory_type: str, extra: dict[str, object], source_ref: str, happened_at: str | None = None, emotional_weight: int = 0) -> str:
        embedding = await self._embedder.embed(summary)
        return await self._save_semantic(
            summary, memory_type, extra, source_ref, embedding,
            happened_at=happened_at, emotional_weight=emotional_weight,
        )

    async def save_items_batch(
        self,
        items: list[tuple[str, str, dict[str, object], str, str | None, int]],
    ) -> list[str]:
        """批量向量化后按输入顺序决策，供不同 memory_type 的任务并发调用。"""
        if not items:
            return []
        embed_batch = getattr(self._embedder, "embed_batch", None)
        if callable(embed_batch):
            vectors = await embed_batch([item[0] for item in items])
        else:
            vectors = [await self._embedder.embed(item[0]) for item in items]
        results: list[str] = []
        for item, vector in zip(items, vectors):
            summary, memory_type, extra, source_ref, happened_at, emotional_weight = item
            if self._store.has_consolidation_write(source_ref):
                results.append(f"skipped:{source_ref}")
                continue
            results.append(await self._save_semantic(
                summary, memory_type, extra, source_ref, vector,
                happened_at=happened_at, emotional_weight=emotional_weight,
            ))
            self._store.record_consolidation_write(
                source_ref,
                _write_digest(memory_type, summary),
            )
        return results

    async def merge_item(self, item_id: str, merged_summary: str, extra_patch: dict[str, object] | None = None) -> None:
        rows = self._store.get_items_by_ids([item_id])
        if not rows:
            return
        old_extra = rows[0].get("extra_json")
        new_extra = dict(old_extra) if isinstance(old_extra, dict) else {}
        new_extra["_merge_note"] = merged_summary
        patch = extra_patch or {}
        if patch.get("tool_requirement"):
            new_extra["tool_requirement"] = patch["tool_requirement"]
        if patch.get("scenario"):
            new_extra["scenario"] = patch["scenario"]
        steps = [*(new_extra.get("steps") or []), *(patch.get("steps") or [])]
        new_extra["steps"] = list(dict.fromkeys(str(step).strip() for step in steps if str(step).strip()))
        constraints = [*(new_extra.get("constraints") or []), *(patch.get("constraints") or [])]
        new_extra["constraints"] = list(dict.fromkeys(
            str(value).strip() for value in constraints if str(value).strip()
        ))
        new_extra["rule_schema"] = build_procedure_rule_schema(
            merged_summary,
            tool_requirement=str(new_extra.get("tool_requirement") or "") or None,
            steps=[str(step) for step in new_extra["steps"]],
            rule_schema=patch.get("rule_schema") if isinstance(patch.get("rule_schema"), dict) else None,
        )
        # 旧 trigger_tags 可能不再描述合并后的规则，保留会造成错误触发。
        new_extra.pop("trigger_tags", None)
        embedding = await self._embedder.embed(merged_summary)
        self._store.merge_item_raw(item_id, merged_summary, embedding, new_extra)

    async def save_from_consolidation(self, history_entry: str, behavior_updates: list[dict[str, object]], source_ref: str, scope_channel: str, scope_chat_id: str, emotional_weight: int = 0) -> None:
        text = str(history_entry or "").strip()
        if not text or self._store.has_consolidation_source_ref(source_ref) or self._store.has_consolidation_write(source_ref):
            return
        try:
            embedding = await self._embedder.embed(text)
            result = await self._save_semantic(
                text, "event",
                {"scope_channel": scope_channel, "scope_chat_id": scope_chat_id},
                source_ref, embedding,
                happened_at=parse_happened_at(text), emotional_weight=emotional_weight,
            )
            self._store.record_consolidation_source_ref(source_ref, result.split(":", 1)[1])
            self._store.record_consolidation_write(source_ref, _write_digest("event", text))
            return
        except Exception as error:
            # consolidation 的单次向量写入失败由 cursor 机制重试，不能写半条事件。
            logger.warning("consolidation 记忆写入失败: %s", error)
            raise
        if behavior_updates:
            logger.info("behavior_updates 由响应后写入链路处理，跳过 %d 条", len(behavior_updates))

    async def save_events_batch(
        self,
        events: list[tuple[str, str, str, str, int]],
    ) -> None:
        """批量生成 Event 向量，并按事件顺序完成语义决策和来源提交。"""
        if not events:
            return
        embed_batch = getattr(self._embedder, "embed_batch", None)
        vectors = (
            await embed_batch([event[0] for event in events])
            if callable(embed_batch)
            else [await self._embedder.embed(event[0]) for event in events]
        )
        for (summary, source_ref, scope_channel, scope_chat_id, weight), vector in zip(events, vectors):
            if self._store.has_consolidation_source_ref(source_ref) or self._store.has_consolidation_write(source_ref):
                continue
            result = await self._save_semantic(
                summary, "event", {"scope_channel": scope_channel, "scope_chat_id": scope_chat_id},
                source_ref, vector, happened_at=parse_happened_at(summary), emotional_weight=weight,
            )
            self._store.record_consolidation_source_ref(source_ref, result.split(":", 1)[1])
            self._store.record_consolidation_write(source_ref, _write_digest("event", summary))

    async def _save_semantic(
        self,
        summary: str,
        memory_type: str,
        extra: dict[str, object],
        source_ref: str,
        embedding: list[float],
        *,
        happened_at: str | None = None,
        emotional_weight: int = 0,
    ) -> str:
        candidates = self._store.vector_search(
            embedding, top_k=3 if memory_type == "event" else self._candidate_top_k,
            memory_types=[memory_type], score_threshold=self._candidate_thresholds.get(memory_type, 0.55),
            hotness_alpha=0.0,
        )
        decision = await self._decider.decide(memory_type, summary, extra, candidates)
        decision = _validate_decision(memory_type, summary, extra, happened_at, candidates, decision)
        if decision.action == "reinforce":
            self._store.reinforce_items_batch([decision.target_id], emotional_weight)
            return f"reinforced:{decision.target_id}"
        if decision.action == "no_change":
            return f"unchanged:{decision.target_id}"
        if decision.action == "merge":
            merged_summary = decision.merged_summary or _merge_summary(
                _candidate_summary(candidates, decision.target_id), summary
            )
            await self.merge_item(decision.target_id, merged_summary, extra)
            return f"merged:{decision.target_id}"
        if decision.action == "supersede":
            return self._store.replace_item_atomic(
                decision.target_id, memory_type, summary, embedding, source_ref,
                extra=extra, happened_at=happened_at,
                emotional_weight=_emotional_weight(emotional_weight),
            )
        return self._store.upsert_item(
            memory_type, summary, embedding, source_ref,
            extra=extra, happened_at=happened_at,
            emotional_weight=_emotional_weight(emotional_weight),
        )

    def supersede_batch(self, ids: list[str]) -> tuple[list[str], list[str]]:
        return self._store.mark_superseded_batch(ids)

    def reinforce_items_batch(self, ids: list[str]) -> None:
        self._store.reinforce_items_batch(ids)


def parse_happened_at(summary: str) -> str | None:
    match = _TIME_PREFIX_RE.match(str(summary).strip())
    if not match:
        return None
    return f"{match.group('date')}T{match.group('hour') or '00'}:{match.group('minute') or '00'}:{match.group('second') or '00'}"


def _write_digest(memory_type: str, summary: str) -> str:
    return hashlib.sha256(f"{memory_type}\0{summary.strip()}".encode("utf-8")).hexdigest()


def _emotional_weight(value: object) -> int:
    try:
        return max(0, min(int(value), 10))
    except (TypeError, ValueError):
        return 0


def _merge_summary(old: str, new: str) -> str:
    old, new = old.strip(), new.strip()
    if new in old:
        return old
    if old in new:
        return new
    return f"{old.rstrip('。；;，, ')}；{new}" if old else new


def _candidate_summary(candidates: list[dict[str, object]], target_id: str) -> str:
    return next((str(item.get("summary") or "") for item in candidates if str(item.get("id")) == target_id), "")


def _validate_decision(
    memory_type: str,
    summary: str,
    extra: dict[str, object],
    happened_at: str | None,
    candidates: list[dict[str, object]],
    decision: MemoryWriteDecision,
) -> MemoryWriteDecision:
    if decision.action == "create":
        return decision
    target = next((item for item in candidates if str(item.get("id")) == decision.target_id), None)
    if target is None:
        return MemoryWriteDecision()
    old_summary = str(target.get("summary") or "").strip()
    if (
        memory_type != "event"
        and decision.action in {"merge", "reinforce"}
        and summary.strip()
        and summary.strip() in old_summary
    ):
        return MemoryWriteDecision(action="no_change", target_id=decision.target_id)
    if memory_type == "event":
        if decision.action == "merge":
            return MemoryWriteDecision()
        old_time = str(target.get("happened_at") or "")
        correction = any(cue in summary for cue in ("纠正", "更正", "不是", "实际", "改为"))
        if old_time and happened_at and old_time != happened_at and not correction:
            return MemoryWriteDecision()
        if decision.action == "supersede" and not correction:
            return MemoryWriteDecision()
    if memory_type == "procedure" and decision.action == "merge":
        old_extra = target.get("extra_json")
        old_schema = resolve_procedure_rule_schema(
            str(target.get("summary") or ""), old_extra if isinstance(old_extra, dict) else {}
        )
        if procedure_rules_conflict(resolve_procedure_rule_schema(summary, extra), old_schema):
            return MemoryWriteDecision()
    return decision


__all__ = ["Memorizer", "parse_happened_at"]
