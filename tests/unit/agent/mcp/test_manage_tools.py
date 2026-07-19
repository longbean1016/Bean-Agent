"""MCP 管理工具的 Schema 与转发测试。"""

from __future__ import annotations

import pytest

from agent.mcp.manage_tools import McpAddTool, McpListTool, McpRemoveTool


class _Registry:
    async def add(self, name, command, env=None, cwd=None):
        return f"add:{name}:{command}:{env}:{cwd}"

    async def remove(self, name):
        return f"remove:{name}"

    def list_servers(self):
        return "list"


@pytest.mark.asyncio
async def test_manage_tools_expose_schema_and_forward_calls() -> None:
    registry = _Registry()
    add = McpAddTool(registry)  # type: ignore[arg-type]
    remove = McpRemoveTool(registry)  # type: ignore[arg-type]
    listing = McpListTool(registry)  # type: ignore[arg-type]

    assert add.parameters["required"] == ["name", "command"]
    assert await add.execute(name="demo", command=["run"], cwd="D:/mcp") == "add:demo:['run']:None:D:/mcp"
    assert await remove.execute(name="demo") == "remove:demo"
    assert await listing.execute() == "list"
