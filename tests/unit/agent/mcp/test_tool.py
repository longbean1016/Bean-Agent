"""MCP 远端工具包装测试。"""

from __future__ import annotations

import pytest

from agent.mcp.client import McpToolInfo
from agent.mcp.tool import McpToolWrapper


class _Client:
    name = "demo"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call(self, name: str, arguments: dict[str, object]) -> str:
        self.calls.append((name, arguments))
        return "ok"


@pytest.mark.asyncio
async def test_wrapper_exposes_prefixed_schema_and_forwards_arguments() -> None:
    client = _Client()
    wrapper = McpToolWrapper(
        client,  # type: ignore[arg-type]
        McpToolInfo(
            name="lookup",
            description="查询记录",
            input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
        ),
    )

    assert wrapper.name == "mcp_demo__lookup"
    assert wrapper.description == "[MCP:demo] 查询记录"
    assert wrapper.parameters["properties"]["id"]["type"] == "string"
    assert await wrapper.execute(id="42") == "ok"
    assert client.calls == [("lookup", {"id": "42"})]
