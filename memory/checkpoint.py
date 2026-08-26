"""Checkpoint source plan：按完整 Turn 选择可归档前缀。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.context_budget import estimate_tokens


@dataclass(slots=True)
class LogicalUnit:
    """一个不可拆分的持久化 Turn；tool_chain 已作为 assistant 的一部分保存。"""

    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def source_message_ids(self) -> list[str]:
        return [str(item["id"]) for item in self.messages if str(item.get("id") or "")]

    @property
    def source_from_seq(self) -> int:
        return int(self.messages[0].get("seq", 0))

    @property
    def consolidated_through_seq(self) -> int:
        return int(self.messages[-1].get("seq", -1)) + 1

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.messages)


def group_logical_units(messages: list[dict[str, Any]]) -> list[LogicalUnit]:
    """按 turn_id 和 user 边界聚合消息，任何工具链都随 assistant 一起移动。"""

    units: list[LogicalUnit] = []
    current: LogicalUnit | None = None
    current_turn_id = ""
    for message in messages:
        role = str(message.get("role") or "").lower()
        turn_id = str(message.get("turn_id") or "")
        starts_unit = current is None or role == "user" and (
            not current.messages or (turn_id and turn_id != current_turn_id)
        )
        if starts_unit:
            if current and current.messages:
                units.append(current)
            current = LogicalUnit()
            current_turn_id = turn_id
        current.messages.append(dict(message))
    if current and current.messages:
        units.append(current)
    return units


def select_compaction_units(
    units: list[LogicalUnit],
    *,
    keep_recent_tokens: int,
) -> tuple[list[LogicalUnit], list[LogicalUnit]]:
    """选择最老的完整前缀，使 retained tail 尽量落在 token 目标内。"""

    if not units:
        return [], []
    if len(units) == 1:
        # 当前只有一个完整 Turn 时只能把它整体归档；不能用半条消息伪造
        # retained tail，否则 checkpoint 会把 tool batch 或用户 anchor 拆开。
        return list(units), []
    target = max(0, int(keep_recent_tokens))
    retained: list[LogicalUnit] = []
    retained_tokens = 0
    for unit in reversed(units):
        if retained and retained_tokens + unit.estimated_tokens > target:
            break
        retained.insert(0, unit)
        retained_tokens += unit.estimated_tokens
    # 只有一个超大 Turn 时也必须允许归档它，否则 gate 永远无法收敛；
    # 该 Turn 仍保持完整，不从 user/tool/assistant 中间切断。
    if len(retained) == len(units):
        retained = units[-1:]
    selected_count = len(units) - len(retained)
    if selected_count <= 0:
        return [], list(units)
    return list(units[:selected_count]), list(units[selected_count:])


def flatten_units(units: list[LogicalUnit]) -> list[dict[str, Any]]:
    return [dict(message) for unit in units for message in unit.messages]


def render_source_messages(units: list[LogicalUnit]) -> str:
    """渲染同一 source plan，供 summary 与所有记忆提取共同读取。"""

    lines: list[str] = []
    for unit in units:
        for message in unit.messages:
            lines.append(
                f"[{str(message.get('timestamp') or 'unknown')}][{str(message.get('role') or 'unknown')}] "
                f"{str(message.get('content') or '')}"
            )
    return "\n".join(lines)


__all__ = [
    "LogicalUnit",
    "flatten_units",
    "group_logical_units",
    "render_source_messages",
    "select_compaction_units",
]
