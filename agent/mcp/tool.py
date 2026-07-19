"""把 MCP 远端工具包装成 BeanAgent 标准工具。"""

from __future__ import annotations

from typing import Any

from agent.mcp.client import McpClient, McpToolInfo
from tools.base import Tool


class McpToolWrapper(Tool):
    """通过 Server 前缀隔离远端名称，避免与内置工具冲突。"""

    def __init__(
        self,
        client: McpClient,
        info: McpToolInfo,
        *,
        server_name: str | None = None,
    ) -> None:
        self._client = client
        self._info = info
        self._server_name = server_name or client.name

    @property
    def name(self) -> str:
        return f"mcp_{self._server_name}__{self._info.name}"

    @property
    def description(self) -> str:
        return f"[MCP:{self._server_name}] {self._info.description}"

    @property
    def parameters(self) -> dict[str, Any]:
        return self._info.input_schema

    async def execute(self, **kwargs: Any) -> str:
        return await self._client.call(self._info.name, kwargs)


__all__ = ["McpToolWrapper"]
