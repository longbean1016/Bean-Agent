"""已提交 Session 消息到 Markdown 草稿的自动归档。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from memory.md_store import MarkdownMemoryStore
from session.store import SessionStore


@dataclass(slots=True)
class ConsolidationDraft:
    history_entries: list[dict[str, object]] = field(default_factory=list)
    pending_items: list[dict[str, object]] = field(default_factory=list)
    recent_context: str = ""


@dataclass(frozen=True, slots=True)
class ConsolidationResult:
    session_key: str
    source_ref: str
    cursor: int
    history_entries: list[dict[str, object]]


class ConsolidationExtractor(Protocol):
    async def extract(self, messages: list[dict[str, object]], previous_recent_context: str) -> ConsolidationDraft: ...


class Consolidator:
    def __init__(self, sessions: SessionStore, markdown: MarkdownMemoryStore, extractor: ConsolidationExtractor, *, keep_count: int = 20, threshold: int | None = None) -> None:
        self._sessions = sessions
        self._markdown = markdown
        self._extractor = extractor
        self._keep_count = max(0, int(keep_count))
        self._threshold = max(1, int(threshold if threshold is not None else max(5, self._keep_count // 2)))

    async def consolidate(self, session_key: str) -> ConsolidationResult | None:
        messages = self._sessions.fetch_session_messages(session_key)
        cursor = self._sessions.get_cursor(session_key)
        end = max(cursor, len(messages) - self._keep_count)
        window = messages[cursor:end]
        if len(window) < self._threshold:
            return None

        draft = await self._extractor.extract(window, self._markdown.read_recent_context())
        source_ref = f"{session_key}@{cursor}-{end - 1}"
        pending_lines = [
            f"- [{str(item.get('tag') or 'key_info')}] {str(item.get('content') or '').strip()}"
            for item in draft.pending_items if str(item.get("content") or "").strip()
        ]
        # cursor 更新是提交点：只有两个 Markdown 目标均成功后才推进，失败则完整重试窗口。
        if pending_lines:
            self._markdown.append_pending_once("\n".join(pending_lines), source_ref=source_ref)
        if draft.recent_context.strip():
            self._markdown.write_recent_context(draft.recent_context)
        self._sessions.set_cursor(session_key, end)
        return ConsolidationResult(session_key, source_ref, end, draft.history_entries)


__all__ = ["ConsolidationDraft", "ConsolidationResult", "Consolidator"]
