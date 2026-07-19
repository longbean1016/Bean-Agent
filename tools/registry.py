"""内置工具的注册、查询、上下文注入与执行。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

_RISKS = frozenset({"read-only", "write", "external-side-effect"})
_SOURCE_TYPES = frozenset({"builtin", "mcp"})


@dataclass(frozen=True, slots=True)
class ToolMeta:
    """描述工具的可见性、风险和所有者，供运行时筛选与批量清理。"""

    risk: str = "read-only"
    always_on: bool = True
    search_hint: str | None = None
    source_type: str = "builtin"
    source_name: str = ""


@dataclass(frozen=True, slots=True)
class ToolDocument:
    """工具的确定性搜索文档，不保存执行实例或 Turn 状态。"""

    name: str
    description: str
    risk: str
    search_hint: str
    source_type: str
    source_name: str
    registration_index: int

    def as_result(self) -> dict[str, str]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk,
            "source_type": self.source_type,
            "source_name": self.source_name,
        }


class ToolRegistry:
    """管理第一阶段全部内置工具。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._metadata: dict[str, ToolMeta] = {}
        self._documents: dict[str, ToolDocument] = {}
        self._next_registration_index = 0

    def register(
        self,
        tool: Tool,
        *,
        risk: str = "read-only",
        always_on: bool = True,
        search_hint: str | None = None,
        source_type: str = "builtin",
        source_name: str = "",
    ) -> None:
        """注册工具；同名工具覆盖旧实例且保留原注册位置。"""

        if risk not in _RISKS:
            raise ValueError(f"不支持的工具风险级别: {risk}")
        if source_type not in _SOURCE_TYPES:
            raise ValueError(f"不支持的工具来源类型: {source_type}")
        existing = self._documents.get(tool.name)
        registration_index = (
            existing.registration_index
            if existing is not None
            else self._next_registration_index
        )
        if existing is None:
            self._next_registration_index += 1
        self._tools[tool.name] = tool
        meta = ToolMeta(
            risk=risk,
            always_on=bool(always_on),
            search_hint=str(search_hint or "").strip() or None,
            source_type=source_type,
            source_name=str(source_name or "").strip(),
        )
        self._metadata[tool.name] = meta
        self._documents[tool.name] = ToolDocument(
            name=tool.name,
            description=tool.description,
            risk=meta.risk,
            search_hint=meta.search_hint or "",
            source_type=meta.source_type,
            source_name=meta.source_name,
            registration_index=registration_index,
        )
        logger.debug("注册工具: %s", tool.name)

    def unregister(self, name: str) -> None:
        """注销工具；名称不存在时保持幂等。"""

        self._tools.pop(name, None)
        self._metadata.pop(name, None)
        self._documents.pop(name, None)
        logger.debug("注销工具: %s", name)

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def get_tool(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def get_registered_names(self) -> set[str]:
        """返回当前已注册工具名集合。"""

        return set(self._tools.keys())

    def get_metadata(self, name: str) -> ToolMeta | None:
        """返回工具只读元数据；名称不存在时返回空。"""

        return self._metadata.get(name)

    def get_always_on_names(self) -> set[str]:
        """返回每个 Turn 初始应当可见的工具名称。"""

        return {
            name
            for name, meta in self._metadata.items()
            if meta.always_on and name in self._tools
        }

    def get_always_on_order(self) -> list[str]:
        """按注册顺序返回常驻工具，保证模型 Schema 前缀稳定。"""

        return [
            name
            for name in self._tools
            if (meta := self._metadata.get(name)) is not None and meta.always_on
        ]

    def get_tool_names_by_source(self, source_type: str, source_name: str) -> list[str]:
        """按注册顺序返回同一来源的工具，供外部服务整体卸载。"""

        return [
            name
            for name, document in sorted(
                self._documents.items(),
                key=lambda item: item[1].registration_index,
            )
            if document.source_type == source_type
            and document.source_name == source_name
            and name in self._tools
        ]

    def get_schemas(
        self,
        *,
        visible_names: set[str] | None = None,
        visible_order: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """按当前 Turn 的可见集合返回稳定的 function calling Schema。"""

        if visible_names is None:
            return [tool.to_schema() for tool in self._tools.values()]
        ordered = visible_order or list(self._tools.keys())
        return [
            self._tools[name].to_schema()
            for name in ordered
            if name in visible_names and name in self._tools
        ]

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        allowed_risk: set[str] | None = None,
        excluded_names: set[str] | None = None,
    ) -> list[dict[str, str]]:
        """在全局工具目录中执行无依赖、可复现的文本搜索。"""

        normalized = str(query or "").strip().casefold()
        if not normalized:
            return []
        excluded = excluded_names or set()
        ranked: list[tuple[int, int, ToolDocument]] = []
        for document in self._documents.values():
            if document.name in excluded or document.name not in self._tools:
                continue
            if allowed_risk is not None and document.risk not in allowed_risk:
                continue
            name = document.name.casefold()
            description = document.description.casefold()
            hint = document.search_hint.casefold()
            if normalized == name:
                score = 0
            elif normalized in name:
                score = 1
            elif normalized in description or normalized in hint:
                score = 2
            else:
                continue
            ranked.append((score, document.registration_index, document))
        ranked.sort(key=lambda item: (item[0], item[1]))
        limit = min(max(1, int(top_k)), 10)
        return [document.as_result() for _, _, document in ranked[:limit]]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
        raise_errors: bool = False,
    ) -> str | ToolResult:
        """执行工具；默认把未知工具和运行异常降级为稳定文本结果。"""

        tool = self._tools.get(name)
        if tool is None:
            if raise_errors:
                raise RuntimeError(f"工具 '{name}' 不存在")
            return f"工具 '{name}' 不存在"

        try:
            # 系统身份只属于当前 Turn，通过调用参数显式传入；模型参数放在
            # 后面，继续允许显式值覆盖同名默认值。
            merged: dict[str, Any] = {**(context or {}), **arguments}
            return await tool.execute(**merged)
        except Exception as error:
            logger.error("工具 %s 执行出错: %s", name, error, exc_info=True)
            if raise_errors:
                raise
            return f"工具执行出错: {error}"


__all__ = ["ToolDocument", "ToolMeta", "ToolRegistry"]
