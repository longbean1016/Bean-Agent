"""Tool 基类、结果标准化与 JSON Schema 参数校验测试。"""

from __future__ import annotations

from abc import ABC

import pytest

from tools.base import Tool, ToolResult, normalize_tool_result


class _ValidTool(Tool):
    name = "valid"
    description = "用于测试的有效工具"
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 2, "maxLength": 5},
            "count": {"type": "integer", "minimum": 1, "maximum": 3},
            "mode": {"type": "string", "enum": ["fast", "safe"]},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"enabled": {"type": "boolean"}},
                    "required": ["enabled"],
                },
            },
        },
        "required": ["name", "count"],
    }

    async def execute(self, **kwargs: object) -> str:
        return str(kwargs)


def test_tool_result_preview_and_normalization_match_base_contract() -> None:
    text_result = ToolResult(text="完整工具结果")
    blocks_result = ToolResult(content_blocks=[{"type": "image"}])

    assert text_result.preview() == "完整工具结果"
    assert blocks_result.preview() == "[多模态结果 1 blocks]"
    assert ToolResult().preview() == ""
    assert normalize_tool_result(text_result) is text_result
    assert normalize_tool_result("字符串结果") == ToolResult(text="字符串结果")


def test_concrete_tool_requires_non_empty_contract_fields() -> None:
    with pytest.raises(TypeError, match="必须定义字段.*description.*parameters"):

        class _MissingFieldsTool(Tool):
            name = "missing"

            async def execute(self, **kwargs: object) -> str:
                return ""

    with pytest.raises(TypeError, match="字段不能为空.*name"):

        class _EmptyNameTool(Tool):
            name = ""
            description = "描述"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs: object) -> str:
                return ""


def test_abstract_intermediate_tool_may_defer_contract_fields() -> None:
    class _AbstractTool(Tool, ABC):
        pass

    assert _AbstractTool.__abstractmethods__


def test_validate_params_recursively_reports_schema_errors() -> None:
    errors = _ValidTool().validate_params(
        {
            "name": "x",
            "count": 4,
            "mode": "unknown",
            "items": [{"enabled": "yes"}, {}],
        }
    )

    assert errors == [
        "name 最短 2 个字符",
        "count 须 <= 3",
        "mode 须为以下值之一：['fast', 'safe']",
        "items[0].enabled 应为 boolean 类型",
        "缺少必填字段：items[1].enabled",
    ]


def test_validate_params_rejects_missing_required_and_invalid_top_schema() -> None:
    assert _ValidTool().validate_params({}) == [
        "缺少必填字段：name",
        "缺少必填字段：count",
    ]

    class _InvalidSchemaTool(Tool):
        name = "invalid_schema"
        description = "顶层 Schema 非 object"
        parameters = {"type": "array", "items": {"type": "string"}}

        async def execute(self, **kwargs: object) -> str:
            return ""

    with pytest.raises(ValueError, match="Schema 顶层类型必须为 object"):
        _InvalidSchemaTool().validate_params({})


def test_to_schema_uses_openai_function_calling_shape() -> None:
    schema = _ValidTool().to_schema()

    assert schema == {
        "type": "function",
        "function": {
            "name": "valid",
            "description": "用于测试的有效工具",
            "parameters": _ValidTool.parameters,
        },
    }
