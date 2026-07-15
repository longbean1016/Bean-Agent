"""长期记忆检索工具。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from memory.contracts import (
    MemoryQuery, MemoryQueryFilters, MemoryQueryIntent, MemoryRetrievalApi,
    MemoryScope, MemoryToolSpec,
)
from tools.base import Tool

_LOCAL_TZ = ZoneInfo("Asia/Shanghai")
_RECENT_PRESETS = {"recent_3d": 3, "recent_7d": 7, "recent_30d": 30}


class RecallMemoryTool(Tool):
    name = "recall_memory"
    description = "由当前 memory engine 的 tool_profile 注入工具描述。"
    parameters = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    def __init__(self, memory: MemoryRetrievalApi, spec: MemoryToolSpec) -> None:
        self._memory = memory
        # Profile 是引擎能力声明，工具不能另写一套 Schema 与实际引擎漂移。
        self.description = spec.description
        self.parameters = cast(dict[str, Any], spec.parameters)

    async def execute(self, query: str, intent: str = "answer", memory_kind: str = "", time_filter: str = "", limit: int = 8, channel: str | None = None, chat_id: str | None = None, **extra: Any) -> str:
        text = str(query or "").strip()
        if not text:
            return _render([], {})
        window = _parse_time_filter(time_filter)
        if time_filter and window is None:
            return json.dumps({"count": 0, "items": [], "error": "invalid_time_filter"}, ensure_ascii=False)
        result = await self._memory.query(MemoryQuery(
            text=text,
            intent=_intent(intent),
            scope=_scope(channel, chat_id),
            filters=MemoryQueryFilters(
                kinds=(memory_kind.strip(),) if memory_kind.strip() else (),
                time_start=window[0] if window else None,
                time_end=window[1] if window else None,
            ),
            limit=max(1, min(int(limit), 200)),
            context=dict(extra),
            timestamp=_timestamp(extra.get("current_timestamp")),
        ))
        return _render(result.records, result.trace)


def _scope(channel: str | None, chat_id: str | None) -> MemoryScope:
    # session_key 只在 channel/chat_id 同时存在时派生，避免产生半截 scope。
    return MemoryScope(session_key=f"{channel}:{chat_id}" if channel and chat_id else "", channel=channel or "", chat_id=chat_id or "")


def _intent(value: str) -> MemoryQueryIntent:
    allowed: dict[str, MemoryQueryIntent] = {v: cast(MemoryQueryIntent, v) for v in ("context", "answer", "timeline", "interest", "procedure")}
    return allowed.get(value, "answer")


def _timestamp(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) and value.strip() else None


def _parse_time_filter(value: str) -> tuple[datetime, datetime] | None:
    text = str(value or "").strip()
    if not text:
        return None
    now = datetime.now(_LOCAL_TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "today": return today, today + timedelta(days=1)
    if text == "yesterday": return today - timedelta(days=1), today
    if text in _RECENT_PRESETS: return now - timedelta(days=_RECENT_PRESETS[text]), now
    try:
        if "~" in text:
            left, right = text.split("~", 1)
            start = datetime.strptime(left.strip(), "%Y-%m-%d").replace(tzinfo=_LOCAL_TZ)
            end = datetime.strptime(right.strip(), "%Y-%m-%d").replace(tzinfo=_LOCAL_TZ)
            return start, end + timedelta(days=1)
        day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=_LOCAL_TZ)
        return day, day + timedelta(days=1)
    except ValueError:
        return None


def _render(records: list[Any], trace: dict[str, object]) -> str:
    items = []
    for record in records:
        evidence = [{"kind": e.kind, "refs": e.refs, "resolver": e.resolver, "source_ref": e.source_ref, "metadata": e.metadata} for e in record.evidence]
        source_ref = next((e["source_ref"] for e in evidence if e["source_ref"]), "")
        item = {"id": record.id, "memory_type": record.kind, "summary": record.summary, "score": round(record.score, 4), "evidence": evidence, "signals": record.signals}
        if source_ref: item["source_ref"] = source_ref
        items.append(item)
    return json.dumps({"count": len(items), "items": items, "trace": trace, "citation_required": True, "citation_format": "§cited:[id1,id2,...]§", "cited_item_ids": [item["id"] for item in items], "citation_rule": "若最终回复使用了本工具返回的任何记忆条目，必须在正文末尾输出 §cited:[实际使用的id列表]§"}, ensure_ascii=False)


__all__ = ["RecallMemoryTool"]
