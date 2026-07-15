"""Agent 工具基础协议与注册中心。"""

from __future__ import annotations

from tools.base import Tool, ToolResult, normalize_tool_result
from tools.registration import register_all, register_filesystem_tools
from tools.registry import ToolRegistry
from tools.runtime import append_tool_result

__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "append_tool_result",
    "normalize_tool_result",
    "register_all",
    "register_filesystem_tools",
]
