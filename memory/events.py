"""记忆后台摄入与写入结果事件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class TurnIngested:
    """完整 Turn 持久化后交给记忆后台 worker 的不可变快照。"""

    session_key: str
    channel: str
    chat_id: str
    user_message: str
    assistant_response: str
    tool_chain: list[dict[str, object]]
    source_ref: str


@dataclass(frozen=True, slots=True)
class MemoryWritten:
    session_key: str
    channel: str
    chat_id: str
    action: Literal["write", "supersede"]
    source_ref: str
    memory_type: str | None = None
    item_id: str | None = None
    summary: str | None = None
    superseded_ids: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ConsolidationCommitted:
    history_entry_payloads: list[tuple[str, int]]
    source_ref: str
    scope_channel: str
    scope_chat_id: str
    conversation: str


__all__ = ["ConsolidationCommitted", "MemoryWritten", "TurnIngested"]
