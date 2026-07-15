"""工具结果、抽象基类与 JSON Schema 参数校验。"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """工具的文本结果与可选多模态内容块。"""

    text: str = ""
    content_blocks: list[dict[str, Any]] = field(default_factory=list)

    def preview(self) -> str:
        """返回适合日志和事件展示的结果摘要。"""

        if self.text:
            return self.text
        if self.content_blocks:
            return f"[多模态结果 {len(self.content_blocks)} blocks]"
        return ""


def normalize_tool_result(result: str | ToolResult) -> ToolResult:
    """把字符串结果统一包装为 ToolResult，方便上层使用同一种结构。"""

    if isinstance(result, ToolResult):
        return result
    return ToolResult(text=result)


class Tool(ABC):
    """所有内置工具必须遵守的最小接口。"""

    name: str
    description: str
    parameters: dict[str, Any]

    _TYPE_MAP: dict[str, type[Any] | tuple[type[Any], ...]] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # 抽象中间类可能把字段留给最终工具定义，因此只检查可实例化子类。
        if cls is Tool or inspect.isabstract(cls):
            return

        missing_fields = [
            field_name
            for field_name in ("name", "description", "parameters")
            if getattr(cls, field_name, None) is None
        ]
        if missing_fields:
            raise TypeError(
                f"{cls.__name__} 必须定义字段：{', '.join(missing_fields)}"
            )

        empty_fields: list[str] = []
        name = getattr(cls, "name")
        if not isinstance(name, property) and not str(name).strip():
            empty_fields.append("name")
        description = getattr(cls, "description")
        if not isinstance(description, property) and not str(description).strip():
            empty_fields.append("description")
        parameters = getattr(cls, "parameters")
        if not isinstance(parameters, property) and not parameters:
            empty_fields.append("parameters")
        if empty_fields:
            raise TypeError(
                f"{cls.__name__} 字段不能为空：{', '.join(empty_fields)}"
            )

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str | ToolResult:
        """执行工具并返回字符串或结构化结果。"""

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """递归校验参数，返回稳定、可直接反馈给模型的错误列表。"""

        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            raise ValueError(
                f"Schema 顶层类型必须为 object，当前为 {schema.get('type')!r}"
            )
        return self._validate(params, {**schema, "type": "object"}, "")

    def _validate(
        self,
        value: Any,
        schema: dict[str, Any],
        path: str,
    ) -> list[str]:
        """校验单个 Schema 节点，并用完整路径定位嵌套参数错误。"""

        label = path or "参数"
        schema_type = schema.get("type")
        expected_type = self._TYPE_MAP.get(str(schema_type))
        if expected_type is not None and not isinstance(value, expected_type):
            return [f"{label} 应为 {schema_type} 类型"]

        errors: list[str] = []
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{label} 须为以下值之一：{schema['enum']}")

        if schema_type in ("integer", "number"):
            if "minimum" in schema and value < schema["minimum"]:
                errors.append(f"{label} 须 >= {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                errors.append(f"{label} 须 <= {schema['maximum']}")

        if schema_type == "string":
            if "minLength" in schema and len(value) < schema["minLength"]:
                errors.append(f"{label} 最短 {schema['minLength']} 个字符")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                errors.append(f"{label} 最长 {schema['maxLength']} 个字符")

        if schema_type == "object":
            properties = schema.get("properties", {})
            for key in schema.get("required", []):
                if key not in value:
                    child_path = f"{path}.{key}" if path else key
                    errors.append(f"缺少必填字段：{child_path}")
            for key, child_value in value.items():
                if key in properties:
                    child_path = f"{path}.{key}" if path else key
                    errors.extend(
                        self._validate(child_value, properties[key], child_path)
                    )

        if schema_type == "array" and "items" in schema:
            for index, item in enumerate(value):
                child_path = f"{path}[{index}]" if path else f"[{index}]"
                errors.extend(self._validate(item, schema["items"], child_path))
        return errors

    def to_schema(self) -> dict[str, Any]:
        """转换为 OpenAI function calling 的工具 Schema。"""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


__all__ = ["Tool", "ToolResult", "normalize_tool_result"]
