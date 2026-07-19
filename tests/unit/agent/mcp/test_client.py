"""stdio MCP 客户端协议与生命周期测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent.mcp.client import McpClient


@pytest.mark.asyncio
async def test_client_handshake_lists_tools_calls_and_disconnects(tmp_path: Path) -> None:
    log_path = tmp_path / "requests.jsonl"
    server = Path(__file__).parents[3] / "fixtures" / "stdio_mcp_server.py"
    client = McpClient(
        name="demo",
        command=[sys.executable, "-u", str(server)],
        env={"BEANAGENT_MCP_TEST_LOG": str(log_path)},
    )

    tools = await client.connect()
    result = await client.call("echo", {"text": "hello"})
    await client.disconnect()
    await client.disconnect()

    requests = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [request["method"] for request in requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert requests[0]["params"]["protocolVersion"] == "2024-11-05"
    assert [(tool.name, tool.description) for tool in tools] == [("echo", "回显文本")]
    assert result == "echo:hello"
    assert client.connected is False


@pytest.mark.asyncio
async def test_client_connect_failure_cleans_up_process() -> None:
    client = McpClient(
        name="broken",
        command=[sys.executable, "-u", "-c", "raise SystemExit(2)"],
    )

    with pytest.raises(Exception):
        await client.connect()

    assert client.connected is False
