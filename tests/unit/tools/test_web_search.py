"""WebSearchTool 的 Exa MCP 请求与 SSE 降级测试。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tools.web_search import WebSearchTool


class FakeClient:
    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def make_response(text: str, status: int = 200) -> httpx.Response:
    request = httpx.Request("POST", "https://mcp.exa.ai/mcp")
    return httpx.Response(status, text=text, request=request)


@pytest.mark.asyncio
async def test_web_search_posts_exa_json_rpc_and_parses_sse() -> None:
    event = {
        "result": {"content": [{"type": "text", "text": "1. Example https://example.com"}]}
    }
    client = FakeClient(make_response(f"event: message\ndata: {json.dumps(event)}\n\n"))
    tool = WebSearchTool(client=client)

    result = json.loads(
        await tool.execute(
            query="BeanAgent", num_results=30, livecrawl="preferred", type="deep"
        )
    )

    payload = client.calls[0]["json"]
    assert payload["method"] == "tools/call"
    assert payload["params"]["name"] == "web_search_exa"
    assert payload["params"]["arguments"] == {
        "query": "BeanAgent",
        "numResults": 20,
        "livecrawl": "preferred",
        "type": "deep",
    }
    assert result == {"query": "BeanAgent", "result": "1. Example https://example.com"}


@pytest.mark.asyncio
async def test_web_search_returns_empty_result_for_invalid_sse() -> None:
    tool = WebSearchTool(client=FakeClient(make_response("data: not-json\n")))

    result = json.loads(await tool.execute(query="missing"))

    assert result == {"query": "missing", "results": [], "count": 0}


@pytest.mark.asyncio
async def test_web_search_maps_request_failure_to_json_error() -> None:
    request = httpx.Request("POST", "https://mcp.exa.ai/mcp")
    tool = WebSearchTool(client=FakeClient(httpx.ConnectError("offline", request=request)))

    result = json.loads(await tool.execute(query="BeanAgent"))

    assert result["query"] == "BeanAgent"
    assert "搜索失败" in result["error"]
