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
    """按稳定时间和角色渲染同一批 source，供不同记忆提取器共同读取。"""

    return "\n".join(
        f"[{str(item.get('timestamp') or 'unknown')}][{str(item.get('role') or 'unknown')}] "
        f"{str(item.get('content') or '')}"
        for item in messages
    )


__all__ = [
    "ConsolidationDraft",
    "ConsolidationExtractor",
    "render_consolidation_conversation",
]
