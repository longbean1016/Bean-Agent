"""记忆写入、强化、语义去重、合并与替代。"""

from __future__ import annotations

import logging
import re
from typing import Protocol

from memory.store import MemoryStore2
from memory.rule_schema import build_procedure_rule_schema

logger = logging.getLogger(__name__)

_TIME_PREFIX_RE = re.compile(
    r"^\[(?P<date>\d{4}-\d{2}-\d{2})(?:[ T](?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?\]"
)


class EmbeddingApi(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class Memorizer:
    """所有人工写入与 consolidation 写入共用的记忆变更边界。"""

    def __init__(self, store: MemoryStore2, embedder: EmbeddingApi) -> None:
        self._store = store
        self._embedder = embedder

    async def save_item(self, summary: str, memory_type: str, extra: dict[str, object], source_ref: str, happened_at: str | None = None, emotional_weight: int = 0) -> str:
        embedding = await self._embedder.embed(summary)
        return self._store.upsert_item(
            memory_type, summary, embedding, source_ref,
            extra=extra, happened_at=happened_at,
            emotional_weight=_emotional_weight(emotional_weight),
        )

    async def save_item_with_supersede(self, summary: str, memory_type: str, extra: dict[str, object], source_ref: str, happened_at: str | None = None, emotional_weight: int = 0, merge_threshold: float = 0.70, supersede_threshold: float = 0.90) -> str:
        embedding = await self._embedder.embed(summary)
        if memory_type in {"procedure", "preference"}:
            similar = self._store.vector_search(
                embedding, top_k=5, memory_types=[memory_type],
                score_threshold=min(merge_threshold, supersede_threshold),
                hotness_alpha=0.0,
            )
            if memory_type == "procedure":
                target = _merge_target(similar, extra, merge_threshold)
                if target is not None:
                    await self.merge_item(
                        str(target["id"]),
                        _merge_summary(str(target.get("summary", "")), summary),
                        extra,
                    )
                    return f"merged:{target['id']}"
            supersede_ids = [
                str(item["id"]) for item in similar
                if float(item.get("vector_score", item.get("score", 0))) >= supersede_threshold
            ]
            if supersede_ids:
                self._store.mark_superseded_batch(supersede_ids)
        elif memory_type == "profile" and str(extra.get("category", "")) in {"status", "purchase"}:
            similar = self._store.vector_search(
                embedding, top_k=5, memory_types=["profile"],
                score_threshold=supersede_threshold, hotness_alpha=0.0,
            )
            category = str(extra["category"])
            supersede_ids = [
                str(item["id"]) for item in similar
                if isinstance(item.get("extra_json"), dict)
                and item["extra_json"].get("category") == category
                and float(item.get("vector_score", item.get("score", 0))) >= supersede_threshold
            ]
            if supersede_ids:
                self._store.mark_superseded_batch(supersede_ids)
        return self._store.upsert_item(
            memory_type, summary, embedding, source_ref,
            extra=extra, happened_at=happened_at,
            emotional_weight=_emotional_weight(emotional_weight),
        )

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
        steps = [*(new_extra.get("steps") or []), *(patch.get("steps") or [])]
        new_extra["steps"] = list(dict.fromkeys(str(step).strip() for step in steps if str(step).strip()))
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
        if not text or self._store.has_consolidation_source_ref(source_ref):
            return
        try:
            embedding = await self._embedder.embed(text)
            similar = self._store.find_similar_recent_events(embedding, threshold=0.92, days_back=7)
            if similar:
                # 语义重复不新增条目，只强化最相似记录，保留事件去重的可观测次数。
                self._store.reinforce_items_batch(similar[:1], emotional_weight)
                return
            self._store.upsert_consolidation_event(
                source_ref, text, embedding,
                extra={"scope_channel": scope_channel, "scope_chat_id": scope_chat_id},
                happened_at=parse_happened_at(text),
                emotional_weight=_emotional_weight(emotional_weight),
            )
        except Exception as error:
            # consolidation 的单次向量写入失败由 cursor 机制重试，不能写半条事件。
            logger.warning("consolidation 记忆写入失败: %s", error)
            raise
        if behavior_updates:
            logger.info("behavior_updates 由响应后写入链路处理，跳过 %d 条", len(behavior_updates))

    def supersede_batch(self, ids: list[str]) -> tuple[list[str], list[str]]:
        return self._store.mark_superseded_batch(ids)

    def reinforce_items_batch(self, ids: list[str]) -> None:
        self._store.reinforce_items_batch(ids)


def parse_happened_at(summary: str) -> str | None:
    match = _TIME_PREFIX_RE.match(str(summary).strip())
    if not match:
        return None
    return f"{match.group('date')}T{match.group('hour') or '00'}:{match.group('minute') or '00'}:{match.group('second') or '00'}"


def _emotional_weight(value: object) -> int:
    try:
        return max(0, min(int(value), 10))
    except (TypeError, ValueError):
        return 0


def _merge_target(items: list[dict[str, object]], extra: dict[str, object], threshold: float) -> dict[str, object] | None:
    tool = str(extra.get("tool_requirement") or "").strip()
    if not tool:
        return None
    for item in items:
        item_extra = item.get("extra_json")
        score = float(item.get("vector_score", item.get("score", 0)))
        if score >= threshold and isinstance(item_extra, dict) and str(item_extra.get("tool_requirement") or "").strip() == tool:
            return item
    return None


def _merge_summary(old: str, new: str) -> str:
    old, new = old.strip(), new.strip()
    if new in old:
        return old
    if old in new:
        return new
    return f"{old.rstrip('。；;，, ')}；{new}" if old else new


__all__ = ["Memorizer", "parse_happened_at"]
