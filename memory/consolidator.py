"""已提交 Session 消息到 Markdown 草稿的自动归档。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from memory.md_store import MarkdownMemoryStore, format_assistant_preview
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
    async def extract(self, messages: list[dict[str, object]], previous_recent_context: str, *, recent_turns: str = "", current_memory: str = "") -> ConsolidationDraft: ...


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


def _format_recent_turns_for_prompt(messages: list[dict[str, object]]) -> str:
    """将热历史消息转为 recent context prompt 可读的简短格式。

    只取 user 和 assistant 角色，assistant 只保留短预览，不暴露完整回复。
    """

    lines: list[str] = []
    for item in messages:
        role = str(item.get("role") or "").lower()
        content = str(item.get("content") or "").strip()
        if not content or role not in {"user", "assistant"}:
            continue
        if role == "assistant" and item.get("proactive"):
            continue
        if role == "assistant":
            preview = format_assistant_preview(content)
            if preview:
                lines.append(f"[a-preview] {preview}")
        else:
            lines.append(f"[user] {content}")
    return "\n".join(lines).strip()


def _stable_recent_context(content: str) -> str:
    """剥离易变预览，避免旧 Recent Turns 与当前 Session 热历史重复入模。"""

    return str(content or "").partition("## Recent Turns")[0].rstrip()


def _with_compression_until(content: str, timestamp: object) -> str:
    """在标准 Compression 区块记录已归档原文的确定边界。"""

    text = str(content or "").strip()
    marker = "## Compression"
    if not text or marker not in text:
        return text
    boundary = str(timestamp or "unknown").strip() or "unknown"
    prefix, suffix = text.split(marker, 1)
    body = suffix.lstrip("\n")
    if body.startswith("until:"):
        _, _, body = body.partition("\n")
    return f"{prefix}{marker}\nuntil: {boundary}\n{body}".rstrip() + "\n"


class Consolidator:
    def __init__(self, sessions: SessionStore, markdown: MarkdownMemoryStore, extractor: ConsolidationExtractor, *, keep_count: int = 20, threshold: int | None = None) -> None:
        self._sessions = sessions
        self._markdown = markdown
        self._extractor = extractor
        self._keep_count = max(0, int(keep_count))
        self._threshold = max(1, int(threshold if threshold is not None else max(5, self._keep_count // 2)))

    def _select_recent_messages(
        self,
        messages: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """按 Akashic 派生规则选择用于预览的最新热历史。"""

        tail = messages[-self._keep_count:] if self._keep_count else messages
        recent_count = max(1, self._keep_count // 2) if self._keep_count else len(tail)
        return tail[-recent_count:] if recent_count else []

    async def consolidate(self, session_key: str) -> ConsolidationResult | None:
        messages = self._sessions.fetch_session_messages(session_key)
        cursor = self._sessions.get_cursor(session_key)
        end = max(cursor, len(messages) - self._keep_count)
        window = messages[cursor:end]
        recent = self._select_recent_messages(messages)
        if len(window) < self._threshold:
            # 未达到归档阈值时不消耗 LLM，只刷新最近对话的可读窗口；cursor 保持不变，
            # 后续消息累计到阈值后仍从同一位置进入 Consolidation。
            self._markdown.refresh_recent_turns(recent)
            return None

        recent_turns = _format_recent_turns_for_prompt(recent)
        current_memory = self._markdown.read_long_term().strip()
        previous_recent_context = _stable_recent_context(
            self._markdown.read_recent_context()
        )
        draft = await self._extractor.extract(
            window,
            previous_recent_context,
            recent_turns=recent_turns,
            current_memory=current_memory,
        )
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
            # LLM 运行期间可能提交新 Turn；这里只重读热历史用于展示，不扩大本次
            # 归档窗口，也不改变候选 cursor=end。
            latest_messages = self._sessions.fetch_session_messages(session_key)
            latest_recent = self._select_recent_messages(latest_messages)
            self._markdown.write_recent_context_snapshot(
                _with_compression_until(
                    draft.recent_context,
                    window[-1].get("timestamp") if window else None,
                ),
                latest_recent,
            )
        conversation = render_consolidation_conversation(window)
        # 此处只返回候选 cursor。MemoryEngine 必须先持久化 outbox 再推进它，
        # 否则进程在两步之间失败会让原文退出模型历史却没有可恢复的派生任务。
        return ConsolidationResult(session_key, source_ref, end, draft.history_entries, conversation)


__all__ = [
    "ConsolidationDraft",
    "ConsolidationResult",
    "Consolidator",
    "render_consolidation_conversation",
    "_format_recent_turns_for_prompt",
]
