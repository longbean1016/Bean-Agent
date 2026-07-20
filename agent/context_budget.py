"""定义上下文超限时可复现的分层退避计划。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ContextRetryPlan:
    """描述一次 Prompt 重组；计划不修改持久 Session，只生成模型视图。"""

    name: str
    drop_sections: frozenset[str] = frozenset()
    history_ratio: float = 1.0


_AUXILIARY_SECTIONS = frozenset(
    {"skills_catalog", "active_tools", "session_context", "self_model"}
)
_WITHOUT_RETRIEVED = _AUXILIARY_SECTIONS | {"retrieved_memory"}
_WITHOUT_LONG_TERM = _WITHOUT_RETRIEVED | {"long_term_memory"}

DEFAULT_CONTEXT_RETRY_PLANS: tuple[ContextRetryPlan, ...] = (
    ContextRetryPlan("full"),
    ContextRetryPlan("trim_skills_catalog", frozenset({"skills_catalog"})),
    ContextRetryPlan("trim_auxiliary_context", _AUXILIARY_SECTIONS),
    ContextRetryPlan("trim_retrieved_memory", _WITHOUT_RETRIEVED),
    ContextRetryPlan("trim_long_term_memory", _WITHOUT_LONG_TERM),
    ContextRetryPlan("half_history", _WITHOUT_LONG_TERM, 0.5),
    ContextRetryPlan("no_history", _WITHOUT_LONG_TERM, 0.0),
)


def slice_complete_turns(
    history: list[dict[str, Any]],
    ratio: float,
) -> list[dict[str, Any]]:
    """按比例保留尾部历史，并从合法 user 边界开始。

    展开的 assistant/tool 消息可能让单个 Turn 超过目标比例。此时宁可保留
    最近完整 Turn，也不能从工具链中间截断；完全没有 user 的孤链直接丢弃。
    """

    safe_ratio = max(0.0, min(float(ratio), 1.0))
    if not history or safe_ratio <= 0:
        return []
    if safe_ratio >= 1:
        return list(history)

    target = max(1, int(len(history) * safe_ratio))
    tentative_start = max(0, len(history) - target)
    user_indexes = [
        index for index, item in enumerate(history) if item.get("role") == "user"
    ]
    if not user_indexes:
        return []
    start = next(
        (index for index in user_indexes if index >= tentative_start),
        user_indexes[-1],
    )
    return list(history[start:])


__all__ = [
    "ContextRetryPlan",
    "DEFAULT_CONTEXT_RETRY_PLANS",
    "slice_complete_turns",
]
