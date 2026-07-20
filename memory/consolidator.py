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
    conversation: str


class ConsolidationExtractor(Protocol):
    async def extract(self, messages: list[dict[str, object]], previous_recent_context: str) -> ConsolidationDraft: ...


def render_consolidation_conversation(messages: list[dict[str, object]]) -> str:
    """保留 Session 的原始时间证据，供所有归档提取链路共同使用。

    时间缺失时显式标记 unknown，不用当前时间代填，避免模型把写入时间误当成
    事件发生时间，或自行猜测年份。
    """

    return "\n".join(
        f"[{str(item.get('timestamp') or 'unknown')}][{str(item.get('role') or 'unknown')}] "
        f"{str(item.get('content') or '')}"
        for item in messages
    )


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
            # 未达到归档阈值时不消耗 LLM，只刷新最近对话的可读窗口；cursor 保持不变，
            # 后续消息累计到阈值后仍从同一位置进入 Consolidation。
            recent = messages[-self._keep_count:] if self._keep_count else messages
            self._markdown.refresh_recent_turns(recent)
            return None

        draft = await self._extractor.extract(window, self._markdown.read_recent_context())
        source_ref = f"{session_key}@{cursor}-{end - 1}"
        pending_lines = [
            f"- [{str(item.get('tag') or 'key_info')}] {str(item.get('content') or '').strip()}"
            for item in draft.pending_items if str(item.get("content") or "").strip()
        ]
        # Markdown 先用稳定 source_ref 幂等落盘，但此处不推进 cursor。Engine 还需要完成
        # event 和隐式长期记忆写入，全部成功后才调用 commit_cursor() 提交整个窗口。
        if pending_lines:
            self._markdown.append_pending_once("\n".join(pending_lines), source_ref=source_ref)
        if draft.recent_context.strip():
            self._markdown.write_recent_context(draft.recent_context)
        conversation = render_consolidation_conversation(window)
        # 此处只返回候选 cursor。MemoryEngine 必须先持久化 outbox 再推进它，
        # 否则进程在两步之间失败会让原文退出模型历史却没有可恢复的派生任务。
        return ConsolidationResult(session_key, source_ref, end, draft.history_entries, conversation)


__all__ = [
    "ConsolidationDraft",
    "ConsolidationResult",
    "Consolidator",
    "render_consolidation_conversation",
]
