"""原始会话消息的精确获取与文本定位工具。"""

from __future__ import annotations

import json
from typing import Any, cast

from session.store import SessionStore
from tools.base import Tool

_MAX_CONTEXT = 10
_MAX_PREVIEW_LINES = 50


class FetchMessagesTool(Tool):
    """把记忆或搜索结果中的引用解析为可核验的原始消息。"""

    name = "fetch_messages"
    description = (
        "fetch_messages 根据消息 ID 或 source_ref 读取原始历史消息原文与上下文。\n"
        "这是 recall_memory / search_messages / 记忆注入三条路里唯一可以直接作为最终证据的工具。\n"
        "何时必须调用：回答依赖具体时间、原话、金额、配置值、是否发生过——只要结论需要事实支撑，就在回复前调用此工具。\n"
        "recall_memory 返回 evidence 时优先传 evidence；search_messages 返回 source_ref 时传 source_ref。\n"
        "支持 context 参数扩展前后文，适合还原完整上下文片段。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "消息 ID 列表",
            },
            "source_ref": {"type": "string", "description": "单个 source_ref"},
            "source_refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "多个 source_ref",
            },
            "evidence": {
                "type": "array",
                "items": {"type": "object"},
                "description": "recall_memory 返回的 evidence 列表",
            },
            "context": {
                "type": "integer",
                "description": "每条消息前后各扩展的上下文条数，默认 0，最大 10",
                "minimum": 0,
                "maximum": _MAX_CONTEXT,
                "default": 0,
            },
        },
    }

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def execute(
        self,
        ids: list[str] | None = None,
        source_ref: str | None = None,
        source_refs: list[str] | None = None,
        evidence: list[dict[str, object]] | None = None,
        context: int = 0,
        **_: Any,
    ) -> str:
        clean_ids = _resolve_ids(
            ids or [], source_ref, source_refs or [], evidence or []
        )
        if not clean_ids:
            return json.dumps(
                {"count": 0, "matched_count": 0, "messages": []},
                ensure_ascii=False,
            )

        safe_context = max(0, min(int(context), _MAX_CONTEXT))
        if safe_context:
            rows = self._store.fetch_by_ids_with_context(clean_ids, safe_context)
            matched_count = sum(bool(row.get("in_source_ref")) for row in rows)
        else:
            rows = self._store.fetch_by_ids(clean_ids)
            matched_count = len(rows)
        messages = [_public_message(row) for row in rows]
        return json.dumps(
            {
                "count": len(messages),
                "matched_count": matched_count,
                "messages": messages,
            },
            ensure_ascii=False,
        )


class SearchMessagesTool(Tool):
    """对原始消息正文做定位搜索，结果预览不能替代原文证据。"""

    name = "search_messages"
    description = (
        "对原始历史消息做 grep 式搜索，返回命中候选消息的预览和 source_ref。\n"
        "适合查找某个词、句子、文件名、报错、命令、配置项曾出现在哪些消息里。\n"
        "命中后若需确认上下文或以结果作为证据，必须继续 fetch_messages(source_ref)。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词或短语"},
            "session_key": {"type": "string", "description": "限定 session（可选）"},
            "role": {
                "type": "string",
                "enum": ["user", "assistant"],
                "description": "限定发言方（可选）",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回条数，默认 10，最大 50",
                "minimum": 1,
                "maximum": 50,
                "default": 10,
            },
            "offset": {
                "type": "integer",
                "description": "分页偏移量，默认 0",
                "minimum": 0,
                "default": 0,
            },
        },
        "required": ["query"],
    }

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def execute(self, query: str, **kwargs: Any) -> str:
        term = str(query or "").strip()
        limit = max(1, min(int(kwargs.get("limit", 10)), 50))
        offset = max(0, int(kwargs.get("offset", 0)))
        if not term:
            return _search_result([], 0, limit, offset)

        rows, total = cast(
            tuple[list[dict[str, Any]], int],
            self._store.search_messages(
                term,
                session_key=str(kwargs.get("session_key") or "").strip() or None,
                role=str(kwargs.get("role") or "").strip() or None,
                limit=limit,
                offset=offset,
            ),
        )
        terms = [value for value in term.split() if value]
        messages = [_search_preview(row, terms) for row in rows]
        return _search_result(messages, total, limit, offset)


def _resolve_ids(
    ids: list[str],
    source_ref: str | None,
    source_refs: list[str],
    evidence: list[dict[str, object]],
) -> list[str]:
    values = [*ids, *([source_ref] if source_ref else []), *source_refs]
    for item in evidence:
        if item.get("source_ref"):
            values.append(str(item["source_ref"]))
        refs = item.get("refs")
        if isinstance(refs, list):
            values.extend(str(ref) for ref in cast(list[object], refs))

    resolved: list[str] = []
    seen: set[str] = set()
    for value in values:
        for message_id in _expand_source_ref(value):
            if message_id not in seen:
                seen.add(message_id)
                resolved.append(message_id)
    return resolved


def _expand_source_ref(value: str | None) -> list[str]:
    prefix = str(value or "").strip().split("#", 1)[0].strip()
    if not prefix:
        return []
    try:
        parsed: object = json.loads(prefix)
    except (json.JSONDecodeError, ValueError):
        return [prefix]
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    if isinstance(parsed, str) and parsed.strip():
        return [parsed.strip()]
    return []


def _public_message(message: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "id", "session_key", "seq", "role", "content", "timestamp", "in_source_ref"
    }
    return {key: value for key, value in message.items() if key in fields}


def _search_preview(message: dict[str, Any], terms: list[str]) -> dict[str, Any]:
    content = str(message.get("content") or "")
    lines = content.splitlines()
    selected = lines[:_MAX_PREVIEW_LINES]
    truncated = len(lines) > _MAX_PREVIEW_LINES
    preview = "\n".join(selected)
    if truncated:
        preview += f"\n...[已截断，剩余 {len(lines) - _MAX_PREVIEW_LINES} 行]"
    message_id = str(message.get("id") or "")
    return {
        "id": message_id,
        "source_ref": message_id,
        "session_key": str(message.get("session_key") or ""),
        "seq": int(message.get("seq") or 0),
        "role": str(message.get("role") or ""),
        "timestamp": str(message.get("timestamp") or ""),
        "matched_terms": [term for term in terms if term.lower() in content.lower()],
        "preview": preview,
        "preview_line_count": min(len(lines), _MAX_PREVIEW_LINES),
        "total_line_count": len(lines),
        "truncated": truncated,
    }


def _search_result(
    messages: list[dict[str, Any]], total: int, limit: int, offset: int
) -> str:
    next_offset = offset + len(messages)
    has_more = next_offset < total
    return json.dumps(
        {
            "count": len(messages),
            "matched_count": total,
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "messages": messages,
        },
        ensure_ascii=False,
    )


__all__ = ["FetchMessagesTool", "SearchMessagesTool"]
