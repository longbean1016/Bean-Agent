"""把 MCP 远端工具包装成 BeanAgent 标准工具。"""

from __future__ import annotations

from typing import Any

from agent.mcp.client import McpToolInfo
from tools.base import Tool


class McpToolWrapper(Tool):
    """通过 Server 前缀隔离远端名称，避免与内置工具冲突。"""

    def __init__(
        self,
        client: Any,
        info: McpToolInfo,
        *,
        server_name: str | None = None,
        tool_namespace: str = "",
    ) -> None:
        self._client = client
        self._info = info
        self._server_name = server_name or client.name
        self._tool_namespace = str(tool_namespace or "")

    @property
    def name(self) -> str:
        return f"mcp_{self._tool_namespace}{self._server_name}__{self._info.name}"

    @property
    def description(self) -> str:
        return f"[MCP:{self._server_name}] {self._info.description}"

    @property
    def parameters(self) -> dict[str, Any]:
        return self._info.input_schema

    async def execute(self, **kwargs: Any) -> str:
        try:
            return await self._client.call(self._info.name, kwargs)
        except Exception:
            # 远端连接可能在工具调用前断开；只允许一次受控重连，避免模型
            # 调用触发无限重试。若客户端不支持重连则沿用原始异常。
            reconnect = getattr(self._client, "reconnect", None)
            if not callable(reconnect):
                raise
            await reconnect()
            return await self._client.call(self._info.name, kwargs)


__all__ = ["McpToolWrapper"]
