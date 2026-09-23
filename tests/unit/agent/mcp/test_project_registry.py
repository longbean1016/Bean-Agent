"""项目级 MCP 注册表路径、隔离和旧配置迁移测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.mcp.client import McpToolInfo
from agent.mcp.project_registry import ProjectMcpRegistryPool, migrate_legacy_workspace_mcp
from tools.registry import ToolRegistry


class _Client:
    def __init__(self, name: str, *_args, **_kwargs) -> None:
        self.name = name

    async def connect(self) -> list[McpToolInfo]:
        return [McpToolInfo("lookup", "查询", {"type": "object", "properties": {}})]

    async def call(self, name: str, arguments: dict[str, object]) -> str:
        return f"{name}:{arguments}"

    async def disconnect(self) -> None:
        return None


@pytest.mark.asyncio
async def test_project_registry_uses_project_config_and_namespaced_tools(tmp_path: Path) -> None:
    tools = ToolRegistry()
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    for project in (project_a, project_b):
        config = project / ".beanagent" / "mcp_servers.json"
        config.parent.mkdir()
        config.write_text(
            json.dumps({"servers": {"demo": {"type": "stdio", "command": ["x"]}}}),
            encoding="utf-8",
        )
    pool = ProjectMcpRegistryPool(tools, client_factory=_Client)

    names_a = await pool.tool_names(project_a)
    names_b = await pool.tool_names(project_b)

    assert len(names_a) == 1
    assert len(names_b) == 1
    assert names_a.isdisjoint(names_b)
    assert all(name.startswith("mcp_project_") for name in names_a | names_b)
    assert ProjectMcpRegistryPool.config_path(project_a) == (
        project_a / ".beanagent" / "mcp_servers.json"
    )
    await pool.shutdown()


def test_legacy_workspace_config_migrates_once_without_overwrite(tmp_path: Path) -> None:
    legacy = tmp_path / "workspace" / "mcp_servers.json"
    user = tmp_path / "home" / ".beanagent" / "mcp_servers.json"
    marker = tmp_path / "workspace" / ".beanagent" / "mcp-user-migration-v1.json"
    legacy.parent.mkdir()
    user.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps({"servers": {
            "legacy": {"type": "stdio", "command": ["legacy"], "enabled": False},
            "same": {"type": "stdio", "command": ["old"], "enabled": False},
        }}),
        encoding="utf-8",
    )
    user.write_text(
        json.dumps({"servers": {
            "same": {"type": "stdio", "command": ["user"], "enabled": False},
        }}),
        encoding="utf-8",
    )

    result = migrate_legacy_workspace_mcp(legacy, user, marker)
    saved = json.loads(user.read_text(encoding="utf-8"))["servers"]

    assert result == {"migrated": ["legacy"], "skipped": ["same"], "status": "completed"}
    assert saved["legacy"]["scope"] == "user"
    assert saved["same"]["command"] == ["user"]
    assert marker.is_file()

    legacy.write_text(json.dumps({"servers": {"later": {"type": "stdio", "command": ["x"]}}}), encoding="utf-8")
    assert migrate_legacy_workspace_mcp(legacy, user, marker)["status"] == "not_needed"
    assert "later" not in json.loads(user.read_text(encoding="utf-8"))["servers"]
