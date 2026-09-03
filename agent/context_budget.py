"""上下文 token 预算与逻辑单元选择工具。"""

from __future__ import annotations

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


def estimate_payload_breakdown(
    messages: Iterable[dict[str, Any]],
    tools: Iterable[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """按前端可读的四类拆分完整 payload，并保证各项可相加到总估算。"""

    message_list = list(messages)
    tool_list = list(tools or [])
    message_tokens = estimate_tokens(message_list)
    system_tokens = estimate_tokens([message_list[0]]) if message_list else 0
    # 把消息协议开销单独列为 overhead；conversation 是除首条 system 外的
    # checkpoint、历史、动态 frame、ReAct 和当前用户消息的估算总量。
    conversation_tokens = max(0, message_tokens - system_tokens)
    tools_tokens = estimate_tokens(tool_list)
    protocol_tokens = len(message_list) * 4 + len(tool_list) * 8
    return {
        "system_prompt_tokens": system_tokens,
        "tools_tokens": tools_tokens,
        "conversation_tokens": conversation_tokens,
        "overhead_tokens": protocol_tokens,
    }


def soft_limit_tokens(context_window: int) -> int:
    """返回 74% 软阈值。"""

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


__all__ = [
    "SOFT_LIMIT_RATIO",
    "estimate_payload_tokens",
    "estimate_payload_breakdown",
    "estimate_tokens",
    "hard_input_limit",
    "should_compact",
    "soft_limit_tokens",
]
