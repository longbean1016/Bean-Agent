"""工具搜索的选择、过滤和稳定结果测试。"""

from __future__ import annotations

import json

import pytest

from tools.base import Tool
from tools.registry import ToolRegistry
from tools.tool_search import ToolSearchTool


class _HiddenTool(Tool):
    name = "mcp_demo__lookup"
    description = "查询演示服务中的记录"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs: object) -> str:
        return "ok"


@pytest.mark.asyncio
async def test_tool_search_supports_keyword_select_and_already_loaded() -> None:
    registry = ToolRegistry()
    registry.register(
        _HiddenTool(),
        always_on=False,
        risk="external-side-effect",
        source_type="mcp",
        source_name="demo",
    )
    tool = ToolSearchTool(registry)

    keyword = json.loads(await tool.execute(query="演示", excluded_names=set()))
    selected = json.loads(
        await tool.execute(
            query="select:mcp_demo__lookup",
            excluded_names={"mcp_demo__lookup"},
        )
    )

    assert keyword["unlocked"] == ["mcp_demo__lookup"]
    assert selected["unlocked"] == []
    assert selected["already_loaded"] == ["mcp_demo__lookup"]


@pytest.mark.asyncio
async def test_tool_search_filters_risk_and_reports_missing_names() -> None:
    registry = ToolRegistry()
    registry.register(
        _HiddenTool(),
        always_on=False,
        risk="external-side-effect",
        source_type="mcp",
        source_name="demo",
    )
    tool = ToolSearchTool(registry)

    blocked = json.loads(
        await tool.execute(query="演示", allowed_risk=["read-only"])
    )
    missing = json.loads(await tool.execute(query="select:not_found"))

    assert blocked["unlocked"] == []
    assert missing["unlocked"] == []
    assert "not_found" in missing["tip"]
