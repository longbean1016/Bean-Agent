"""工具结果写回模型消息列表的最小运行时辅助函数。"""

from __future__ import annotations

from typing import Any

from tools.base import ToolResult, normalize_tool_result


def append_tool_result(
    messages: list[dict[str, Any]],
    *,
    tool_call_id: str,
    content: str | ToolResult,
    tool_name: str | None = None,
) -> None:
    """追加标准 tool 消息，并把图片块作为后续 user 多模态消息传入。"""

    result = normalize_tool_result(content)
    messages.append(
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result.text or "工具执行完成。",
        }
    )
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


__all__ = ["append_tool_result"]
