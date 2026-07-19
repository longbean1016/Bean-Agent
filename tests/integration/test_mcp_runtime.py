"""真实 stdio 子进程下的 MCP 动态注入与搜索调用集成测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent.config_models import Config
from bootstrap.app import AppRuntime, build_core_runtime


class _Provider:
    async def chat(self, *args, **kwargs):
        raise AssertionError("组件集成测试不应调用 LLM")

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_runtime_add_search_call_remove_mcp_tool(tmp_path: Path) -> None:
    config = Config()
    config.memory.enabled = False
    config.agent.workdir = str(tmp_path / "workdir")
    core = build_core_runtime(config, tmp_path / "workspace", provider=_Provider())
    runtime = AppRuntime(core)
    server = Path(__file__).parents[1] / "fixtures" / "stdio_mcp_server.py"
    log_path = tmp_path / "mcp.jsonl"
    await runtime.start()
    try:
        added = await core.tools.execute(
            "mcp_add",
            {
                "name": "demo",
                "command": [sys.executable, "-u", str(server)],
                "env": {"BEANAGENT_MCP_TEST_LOG": str(log_path)},
            },
        )
        searched = json.loads(
            str(
                await core.tools.execute(
                    "tool_search",
                    {"query": "回显"},
                    context={
                        "excluded_names": core.tools.get_always_on_names(),
                    },
                )
            )
        )
        called = await core.tools.execute(
            "mcp_demo__echo",
            {"text": "hello"},
        )
        removed = await core.tools.execute("mcp_remove", {"name": "demo"})

        assert "mcp_demo__echo" in str(added)
        assert searched["unlocked"] == ["mcp_demo__echo"]
        assert called == "echo:hello"
        assert "已注销" in str(removed)
        assert core.tools.has_tool("mcp_demo__echo") is False
    finally:
        await runtime.shutdown()
