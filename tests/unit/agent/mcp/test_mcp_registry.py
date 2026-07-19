"""MCP 服务注册、回滚、持久化与关闭测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.mcp.client import McpToolInfo
from agent.mcp.registry import McpServerRegistry
from tools.base import Tool
from tools.registry import ToolRegistry


class _Client:
    instances: list["_Client"] = []

    def __init__(self, name, command, env=None, cwd=None) -> None:
        self.name = name
        self.command = list(command)
        self.env = dict(env or {})
        self.cwd = cwd
        self.disconnected = False
        self.instances.append(self)

    async def connect(self):
        if self.name == "broken":
            raise RuntimeError("connect failed")
        return [
            McpToolInfo(
                name="lookup",
                description="查询记录",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    async def call(self, name, arguments):
        return f"{name}:{arguments}"

    async def disconnect(self):
        self.disconnected = True


class _ConflictTool(Tool):
    name = "mcp_demo__lookup"
    description = "已有工具"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return "existing"


@pytest.fixture(autouse=True)
def _clear_clients() -> None:
    _Client.instances.clear()


@pytest.mark.asyncio
async def test_registry_add_list_remove_and_persist(tmp_path: Path) -> None:
    tools = ToolRegistry()
    registry = McpServerRegistry(
        tmp_path / "mcp_servers.json",
        tools,
        client_factory=_Client,
    )

    added = await registry.add("demo", ["python", "server.py"], {"TOKEN": "secret"})

    assert "mcp_demo__lookup" in added
    assert tools.has_tool("mcp_demo__lookup")
    assert tools.get_metadata("mcp_demo__lookup").always_on is False
    assert "secret" not in registry.list_servers()
    saved = json.loads((tmp_path / "mcp_servers.json").read_text(encoding="utf-8"))
    assert saved["servers"]["demo"]["command"] == ["python", "server.py"]

    removed = await registry.remove("demo")

    assert "已注销" in removed
    assert tools.has_tool("mcp_demo__lookup") is False
    assert _Client.instances[0].disconnected is True
    assert json.loads((tmp_path / "mcp_servers.json").read_text(encoding="utf-8")) == {"servers": {}}


@pytest.mark.asyncio
async def test_registry_rejects_conflict_and_rolls_back_client(tmp_path: Path) -> None:
    tools = ToolRegistry()
    tools.register(_ConflictTool())
    registry = McpServerRegistry(
        tmp_path / "mcp_servers.json",
        tools,
        client_factory=_Client,
    )

    result = await registry.add("demo", ["python", "server.py"])

    assert "冲突" in result
    assert tools.get_tool("mcp_demo__lookup").description == "已有工具"
    assert _Client.instances[0].disconnected is True
    assert not (tmp_path / "mcp_servers.json").exists()


@pytest.mark.asyncio
async def test_registry_restore_isolates_failure_and_shutdown_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "mcp_servers.json"
    path.write_text(
        json.dumps(
            {
                "servers": {
                    "good": {"command": ["good"]},
                    "broken": {"command": ["broken"]},
                }
            }
        ),
        encoding="utf-8",
    )
    tools = ToolRegistry()
    registry = McpServerRegistry(path, tools, client_factory=_Client)

    await registry.load_and_connect_all()
    await registry.shutdown()
    await registry.shutdown()

    assert tools.has_tool("mcp_good__lookup") is False
    assert registry.connected_server_names() == set()
    assert all(client.disconnected for client in _Client.instances)
