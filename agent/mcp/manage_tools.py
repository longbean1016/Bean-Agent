"""Agent 可调用的 MCP 服务添加、移除和查询工具。"""

from __future__ import annotations

from typing import Any

from agent.mcp.registry import McpServerRegistry
from tools.base import Tool


class McpAddTool(Tool):
    """连接本地 stdio MCP Server，并立即注册其远端工具。"""

    name = "mcp_add"
    description = "连接并注册一个本地 stdio MCP Server，成功后其工具可被搜索使用。"
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "唯一短名称"},
            "command": {
                "type": "array",
                "items": {"type": "string"},
                "description": "不经过 shell 的启动命令参数数组",
            },
            "env": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
            "cwd": {"type": "string", "description": "可选工作目录"},
        },
        "required": ["name", "command"],
    }

    def __init__(self, registry: McpServerRegistry) -> None:
        self._registry = registry

    async def execute(
        self,
        name: str,
        command: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        **_: Any,
    ) -> str:
        return await self._registry.add(name, command, env, cwd)


class McpRemoveTool(Tool):
    """注销一个服务并移除其全部动态工具。"""

    name = "mcp_remove"
    description = "注销并断开一个 MCP Server，同时移除其全部工具。"
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "服务名称"}},
        "required": ["name"],
    }

    def __init__(self, registry: McpServerRegistry) -> None:
        self._registry = registry

    async def execute(self, name: str, **_: Any) -> str:
        return await self._registry.remove(name)


class McpListTool(Tool):
    """列出当前连接的服务和工具，但不暴露环境变量。"""

    name = "mcp_list"
    description = "列出当前已连接的 MCP Server 及其工具名称。"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, registry: McpServerRegistry) -> None:
        self._registry = registry

    async def execute(self, **_: Any) -> str:
        return self._registry.list_servers()


__all__ = ["McpAddTool", "McpListTool", "McpRemoveTool"]
