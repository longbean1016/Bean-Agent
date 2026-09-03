"""工具结果写回模型消息列表的最小运行时辅助函数。"""

from __future__ import annotations

from typing import Any

from tools.base import ToolResult, normalize_tool_result

_TOOL_RESULT_CHAR_BUDGET = 10_000


def _truncate_tool_text(text: str) -> str:
    """将发给模型的工具文本限制在固定预算内，并明确标注省略量。

    截断只发生在 LLM 消息边界；调用方仍持有原始 ToolResult，可用于 Session
    持久化和前端展示。标记本身计入预算，避免看似截断后仍超过上下文上限。
    """

    if len(text) <= _TOOL_RESULT_CHAR_BUDGET:
        return text
    omitted = len(text) - _TOOL_RESULT_CHAR_BUDGET
    while True:
        marker = f"\n[已截断工具结果，省略 {omitted} 个字符]"
        keep = max(0, _TOOL_RESULT_CHAR_BUDGET - len(marker))
        actual_omitted = len(text) - keep
        if actual_omitted == omitted:
            break
        omitted = actual_omitted
    return f"{text[:keep]}{marker}"


def append_tool_result(
    messages: list[dict[str, Any]],
    *,
    tool_call_id: str,
    content: str | ToolResult,
    tool_name: str | None = None,
) -> None:
    """追加标准 tool 消息，并把图片块作为后续 user 多模态消息传入。"""

    messages.extend(
        serialize_tool_result_messages(
            tool_call_id=tool_call_id,
            content=content,
            tool_name=tool_name,
        )
    )


def serialize_tool_result_messages(
    *,
    tool_call_id: str,
    content: str | ToolResult,
    tool_name: str | None = None,
) -> list[dict[str, Any]]:
    """生成实时和历史重放共用的完整工具消息序列。"""

    result = normalize_tool_result(content)
    model_text = _truncate_tool_text(result.text)
    messages: list[dict[str, Any]] = [
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": model_text or "工具执行完成。",
        }
    ]
    if result.content_blocks:
        # OpenAI 工具消息的 content 保持文本；图片放在紧随其后的 user
        # 消息中，既保留 tool_call_id 配对，也让多模态模型真正看到图片。
        prefix = (
            f"以下是工具 {tool_name} 读取到的文件内容，请直接查看。"
            if tool_name
            else "以下是工具读取到的文件内容，请直接查看。"
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prefix},
                    *result.content_blocks,
                ],
            }
        )
    return messages


__all__ = ["append_tool_result", "serialize_tool_result_messages"]
