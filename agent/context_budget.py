"""上下文 token 预算与逻辑单元选择工具。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Iterable


SOFT_LIMIT_RATIO = 0.74


def estimate_tokens(value: object) -> int:
    """使用稳定的字符近似估算 token；具体模型 tokenizer 不可用时仍可做 gate。"""

    if value is None:
        return 0
    if isinstance(value, str):
        # 中文和 JSON 标点通常比 ASCII 更接近一字一 token，使用 3 字符/token
        # 是保守的跨供应商近似；真实 usage 只能用于后续 delta 校准。
        return max(1, math.ceil(len(value) / 3))
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        encoded = str(value)
    return estimate_tokens(encoded)


def estimate_payload_tokens(
    messages: Iterable[dict[str, Any]],
    tools: Iterable[dict[str, Any]] | None = None,
) -> int:
    """估算完整请求 payload，包含消息、tool schema 和协议开销。"""

    message_list = list(messages)
    tool_list = list(tools or [])
    # 每条消息和每个工具都保留固定协议开销，避免只计算正文导致 gate 偏乐观。
    return max(1, estimate_tokens(message_list) + estimate_tokens(tool_list) + len(message_list) * 4 + len(tool_list) * 8)


def soft_limit_tokens(context_window: int) -> int:
    """返回 Akashic 式 74% 软阈值。"""

    return math.floor(max(0, int(context_window)) * SOFT_LIMIT_RATIO)


def hard_input_limit(context_window: int, max_output_tokens: int) -> int:
    """为输出预算预留空间，得到输入硬上限。"""

    window = int(context_window)
    output = int(max_output_tokens)
    if window <= 0:
        raise ValueError("context_window 必须是正整数")
    if output < 0 or output >= window:
        raise ValueError("max_output_tokens 必须在 [0, context_window) 内")
    return window - output


def should_compact(
    estimated_tokens: int,
    *,
    context_window: int,
    max_output_tokens: int,
) -> bool:
    """判断是否越过 soft 或 hard gate；未知窗口交给 provider 自己处理。"""

    if int(context_window) <= 0:
        return False
    hard_limit = hard_input_limit(context_window, max_output_tokens)
    return int(estimated_tokens) >= soft_limit_tokens(context_window) or int(estimated_tokens) >= hard_limit


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
