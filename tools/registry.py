"""内置工具的注册、查询、上下文注入与执行。"""

from __future__ import annotations

import logging
from typing import Any

from tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)


class ToolRegistry:
    """管理第一阶段全部内置工具。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册工具；同名工具覆盖旧实例且保留原注册位置。"""

        self._tools[tool.name] = tool
        logger.debug("注册工具: %s", tool.name)

    def unregister(self, name: str) -> None:
        """注销工具；名称不存在时保持幂等。"""

        self._tools.pop(name, None)
        logger.debug("注销工具: %s", name)

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def get_tool(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def get_registered_names(self) -> set[str]:
        """返回当前已注册工具名集合。"""

        return set(self._tools.keys())

    def get_schemas(self) -> list[dict[str, Any]]:
        """按注册顺序返回 OpenAI function calling Schema。"""

        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: dict[str, str] | None = None,
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


__all__ = ["ToolRegistry"]
