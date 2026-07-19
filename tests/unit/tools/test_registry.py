"""ToolRegistry 注册、上下文合并和异常边界测试。"""

from __future__ import annotations

import pytest

from tools.base import Tool
from tools.registry import ToolRegistry


class _RecordingTool(Tool):
    name = "record"
    description = "记录最终调用参数"
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
    }

    def __init__(self, label: str = "first") -> None:
        self.label = label
        self.calls: list[dict[str, object]] = []

    async def execute(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        return self.label


class _SecondTool(Tool):
    name = "second"
    description = "第二个工具"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs: object) -> str:
        return "second"


class _FailingTool(Tool):
    name = "failing"
    description = "执行时抛出异常"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs: object) -> str:
        raise ValueError("执行失败")


def test_register_overwrites_same_name_and_preserves_schema_order() -> None:
    registry = ToolRegistry()
    first = _RecordingTool("first")
    replacement = _RecordingTool("replacement")
    registry.register(first)
    registry.register(_SecondTool())
    registry.register(replacement)

    assert registry.get_tool("record") is replacement
    assert registry.has_tool("second") is True
    assert registry.get_registered_names() == {"record", "second"}
    assert [item["function"]["name"] for item in registry.get_schemas()] == [
        "record",
        "second",
    ]

    registry.unregister("second")
    registry.unregister("missing")
    assert registry.has_tool("second") is False


def test_registry_tracks_metadata_source_and_visible_schema_order() -> None:
    registry = ToolRegistry()
    registry.register(_RecordingTool(), risk="write", search_hint="保存记录")
    registry.register(
        _SecondTool(),
        always_on=False,
        risk="external-side-effect",
        source_type="mcp",
        source_name="demo",
    )

    assert registry.get_always_on_names() == {"record"}
    assert registry.get_always_on_order() == ["record"]
    assert registry.get_tool_names_by_source("mcp", "demo") == ["second"]
    assert registry.get_metadata("second").risk == "external-side-effect"
    assert [
        item["function"]["name"]
        for item in registry.get_schemas(
            visible_names={"record", "second"},
            visible_order=["second", "record"],
        )
    ] == ["second", "record"]

    registry.unregister("second")
    assert registry.get_metadata("second") is None
    assert registry.search("第二个") == []


def test_registry_search_is_ranked_stable_and_filters_risk() -> None:
    registry = ToolRegistry()
    registry.register(_RecordingTool(), risk="write", search_hint="保存记录")
    registry.register(
        _SecondTool(),
        always_on=False,
        risk="read-only",
        search_hint="记录查询",
        source_type="mcp",
        source_name="demo",
    )

    assert [item["name"] for item in registry.search("second")] == ["second"]
    assert [item["name"] for item in registry.search("记录")] == [
        "record",
        "second",
    ]
    assert [
        item["name"]
        for item in registry.search("记录", allowed_risk={"read-only"})
    ] == ["second"]


@pytest.mark.asyncio
async def test_execute_merges_context_as_low_priority_defaults() -> None:
    registry = ToolRegistry()
    tool = _RecordingTool()
    registry.register(tool)

    result = await registry.execute(
        "record",
        {"query": "BeanAgent", "chat_id": "argument-chat"},
        context={"channel": "web", "chat_id": "context-chat"},
    )

    assert result == "first"
    assert tool.calls == [
        {
            "channel": "web",
            "chat_id": "argument-chat",
            "query": "BeanAgent",
        }
    ]
    assert not hasattr(registry, "set_context")
    assert not hasattr(registry, "get_context")


@pytest.mark.asyncio
async def test_execute_protects_current_user_source_ref_from_model_arguments() -> None:
    registry = ToolRegistry()
    tool = _RecordingTool()
    registry.register(tool)

    await registry.execute(
        "record",
        {"current_user_source_ref": "forged:9"},
        context={"current_user_source_ref": "web:c:4"},
    )

    assert tool.calls[0]["current_user_source_ref"] == "web:c:4"


@pytest.mark.asyncio
async def test_unknown_tool_degrades_or_raises_on_request() -> None:
    registry = ToolRegistry()

    assert await registry.execute("missing", {}) == "工具 'missing' 不存在"
    with pytest.raises(RuntimeError, match="工具 'missing' 不存在"):
        await registry.execute("missing", {}, raise_errors=True)


@pytest.mark.asyncio
async def test_tool_exception_is_logged_and_can_be_reraised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = ToolRegistry()
    registry.register(_FailingTool())

    assert await registry.execute("failing", {}) == "工具执行出错: 执行失败"
    assert "工具 failing 执行出错" in caplog.text

    with pytest.raises(ValueError, match="执行失败"):
        await registry.execute("failing", {}, raise_errors=True)
