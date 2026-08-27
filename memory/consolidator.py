"""Checkpoint 统一记忆提取契约。

历史消息边界、token gate 和 generation ledger 由 MemoryEngine 与
SessionStore 管理；本模块只保留提取结果的数据结构，避免重新引入按消息数
驱动的旧归档器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class ConsolidationDraft:
    """同一 selected source 生成的事件摘要和 PENDING 候选。"""

    history_entries: list[dict[str, object]] = field(default_factory=list)
    pending_items: list[dict[str, object]] = field(default_factory=list)


class ConsolidationExtractor(Protocol):
    async def extract(
        self,
        messages: list[dict[str, object]],
        previous_summary: str = "",
        *,
        current_memory: str = "",
    ) -> ConsolidationDraft: ...


def render_consolidation_conversation(messages: list[dict[str, object]]) -> str:
    """生成记忆投影，不把系统帧、独立 tool 或主动消息当成用户证据。"""

    lines: list[str] = []
    for item in messages:
        role = str(item.get("role") or "").lower()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        if content.startswith("<system-reminder") or content.startswith("<session-context-compaction"):
            continue
        metadata = item.get("metadata")
        if role == "assistant" and (
            bool(item.get("proactive"))
            or (isinstance(metadata, dict) and bool(metadata.get("proactive")))
        ):
            continue
        lines.append(
            f"[{str(item.get('timestamp') or 'unknown')}][{role}] {content}"
        )
    return "\n".join(lines)


__all__ = [
    "ConsolidationDraft",
    "ConsolidationExtractor",
    "render_consolidation_conversation",
]
