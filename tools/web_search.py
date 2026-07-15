"""基于 Exa 公共 MCP 端点的互联网搜索工具。"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

from tools.base import Tool

_MCP_URL = "https://mcp.exa.ai/mcp"
_DEFAULT_NUM_RESULTS = 8


class AsyncHttpClient(Protocol):
    async def post(self, url: str, **kwargs: Any) -> httpx.Response: ...


class WebSearchTool(Tool):
    """通过 Exa MCP 搜索互联网并解析 SSE 响应。"""

    name = "web_search"
    description = (
        "用关键词搜索互联网，返回最新的搜索结果（标题 + 摘要 + URL）。"
        "适合查询时效性信息：新闻、产品发布、价格、人物动态等。"
        "拿到 URL 后可用 web_fetch 获取完整内容。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "num_results": {
                "type": "integer",
                "description": f"返回结果数量，默认 {_DEFAULT_NUM_RESULTS}，最大 20",
                "minimum": 1,
                "maximum": 20,
            },
            "livecrawl": {
                "type": "string",
                "enum": ["fallback", "preferred"],
                "description": "实时抓取模式：fallback（缓存优先）或 preferred（优先实时），默认 fallback",
            },
            "type": {
                "type": "string",
                "enum": ["auto", "fast", "deep"],
                "description": "搜索类型：auto（均衡）、fast（快速）、deep（深度），默认 auto",
            },
        },
        "required": ["query"],
    }

    def __init__(self, client: AsyncHttpClient | None = None) -> None:
        self._client = client

    async def execute(self, **kwargs: Any) -> str:
        query = str(kwargs["query"])
        num_results = min(int(kwargs.get("num_results", _DEFAULT_NUM_RESULTS)), 20)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "web_search_exa",
                "arguments": {
                    "query": query,
                    "numResults": num_results,
                    "livecrawl": kwargs.get("livecrawl", "fallback"),
                    "type": kwargs.get("type", "auto"),
                },
            },
        }

        try:
            response = await self._post(payload)
            response.raise_for_status()
        except Exception as error:
            # 搜索属于辅助能力，网络不可用时返回可被模型理解的结构化错误。
            return json.dumps(
                {"error": f"搜索失败：{error}", "query": query},
                ensure_ascii=False,
            )

        # MCP 端点以 SSE 返回 JSON-RPC 结果；忽略心跳和损坏事件，继续寻找有效 data 行。
        for line in response.text.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            content = data.get("result", {}).get("content", [])
            if content:
                return json.dumps(
                    {"query": query, "result": content[0].get("text", "")},
                    ensure_ascii=False,
                )
        return json.dumps(
            {"query": query, "results": [], "count": 0},
            ensure_ascii=False,
        )

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        request_kwargs = {
            "json": payload,
            "headers": {
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
        }
        if self._client is not None:
            return await self._client.post(_MCP_URL, **request_kwargs)
        async with httpx.AsyncClient(timeout=25.0) as client:
            return await client.post(_MCP_URL, **request_kwargs)


__all__ = ["WebSearchTool"]
