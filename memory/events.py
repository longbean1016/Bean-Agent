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
    # checkpoint 预先完成的统一提取结果；旧 outbox 没有这些字段时，消费端
    # 仍可按兼容路径重新提取 implicit memory。
    session_key: str = ""
    generation: int = 0
    pending_items: list[dict[str, object]] = field(default_factory=list)
    implicit_memory: dict[str, list[dict[str, object]]] = field(default_factory=dict)


__all__ = ["ConsolidationCommitted", "MemoryWritten", "TurnIngested"]
