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


class _UnhealthyClient(_Client):
    def __init__(self, name, command, env=None, cwd=None):
        super().__init__(name, command, env, cwd)
        self.connected = True

    async def ping(self):
        return False


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


@pytest.mark.asyncio
async def test_registry_disabled_update_enable_refresh_and_test_are_idempotent(tmp_path: Path) -> None:
    tools = ToolRegistry()
    registry = McpServerRegistry(tmp_path / "mcp_servers.json", tools, client_factory=_Client)

    await registry.create(
        "demo",
        {"type": "stdio", "command": ["python", "server.py"], "enabled": False},
    )
    record = registry.list_server_records()[0]
    assert record["status"] == "disabled"
    assert record["enabled"] is False
    assert registry.connected_server_names() == set()

    tested = await registry.test("demo", {"type": "stdio", "command": ["python", "server.py"]})
    assert tested["success"] is True
    assert registry.connected_server_names() == set()

    await registry.enable("demo")
    assert "demo" in registry.connected_server_names()
    refreshed = await registry.refresh("demo")
    assert refreshed["status"] == "connected"
    await registry.disable("demo")
    await registry.disable("demo")
    assert registry.list_server_records()[0]["status"] == "disabled"


@pytest.mark.asyncio
async def test_registry_preserves_http_config_and_does_not_expose_headers(tmp_path: Path) -> None:
    tools = ToolRegistry()
    registry = McpServerRegistry(tmp_path / "mcp_servers.json", tools, client_factory=_Client)

    # HTTP 连接使用独立传输；停用时不连外部网络，但配置仍可持久化。
    await registry.create(
        "remote",
        {
            "type": "http",
            "url": "https://127.0.0.1:1/mcp",
            "headers": {"Authorization": "Bearer secret"},
            "enabled": False,
        },
    )
    record = registry.list_server_records()[0]
    assert record["transport"] == "http"
    assert record["header_names"] == ["Authorization"]
    assert "secret" not in json.dumps(record)

    await registry.update("remote", {"type": "http", "url": "https://127.0.0.1:1/next", "enabled": False})
    saved = json.loads((tmp_path / "mcp_servers.json").read_text(encoding="utf-8"))
    assert saved["servers"]["remote"]["headers"]["Authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_registry_default_scope_keeps_user_and_workspace_files_separate(tmp_path: Path) -> None:
    tools = ToolRegistry()
    user = McpServerRegistry(
        tmp_path / "user" / "mcp_servers.json",
        tools,
        client_factory=_Client,
        default_scope="user",
    )
    workspace = McpServerRegistry(
        tmp_path / "workspace" / "mcp_servers.json",
        tools,
        client_factory=_Client,
        default_scope="workspace",
    )

    await user.create("user_server", {"type": "stdio", "command": ["user"]})
    await workspace.create("workspace_server", {"type": "stdio", "command": ["workspace"]})

    assert [item["scope"] for item in user.list_server_records(scope="user")] == ["user"]
    assert [item["scope"] for item in workspace.list_server_records(scope="workspace")] == ["workspace"]
    assert "workspace_server" not in {item["id"] for item in user.list_server_records(scope="user")}
    assert "user_server" not in {item["id"] for item in workspace.list_server_records(scope="workspace")}
    assert (tmp_path / "user" / "mcp_servers.json").is_file()
    assert (tmp_path / "workspace" / "mcp_servers.json").is_file()

    await user.shutdown()
    await workspace.shutdown()


@pytest.mark.asyncio
async def test_refresh_marks_failed_health_check_and_reports_reconnect(tmp_path: Path) -> None:
    tools = ToolRegistry()
    registry = McpServerRegistry(tmp_path / "mcp_servers.json", tools, client_factory=_UnhealthyClient)

    await registry.create("remote", {"type": "stdio", "command": ["remote"]})
    result = await registry.refresh("remote")

    assert result["health_checked"] is True
    assert result["reconnected"] is True
    assert result["status"] == "connected"
    await registry.shutdown()
