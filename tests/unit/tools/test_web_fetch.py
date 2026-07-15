"""WebFetchTool 的 URL 安全、内容转换和响应预算测试。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tools.web_fetch import WebFetchTool, _MAX_BYTES, _MAX_TEXT_CHARS


class FakeClient:
    """记录请求参数并返回预置响应，避免单元测试访问真实网络。"""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def make_response(
    content: bytes,
    *,
    status: int = 200,
    content_type: str = "text/plain; charset=utf-8",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    merged_headers = {"content-type": content_type, **(headers or {})}
    request = httpx.Request("GET", "https://example.com/start")
    return httpx.Response(
        status,
        content=content,
        headers=merged_headers,
        request=request,
    )


@pytest.mark.asyncio
async def test_web_fetch_rejects_invalid_and_private_urls_before_request() -> None:
    client = FakeClient(make_response(b"unused"))
    tool = WebFetchTool(client=client)

    invalid = json.loads(await tool.execute(url="ftp://example.com/file"))
    private = json.loads(await tool.execute(url="http://127.0.0.1/private"))

    assert "http:// 或 https://" in invalid["error"]
    assert "内网/本地地址" in private["error"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_web_fetch_converts_html_to_text_and_sends_reference_headers() -> None:
    response = make_response(
        b"<html><style>.x{}</style><body><h1>Title</h1><script>x()</script><p>Hello</p></body></html>",
        content_type="text/html; charset=utf-8",
    )
    client = FakeClient(response)
    tool = WebFetchTool(client=client)

    result = json.loads(
        await tool.execute(url="https://example.com/page", format="text", timeout=40)
    )

    assert result["text"] == "Title Hello"
    assert result["final_url"] == "https://example.com/start"
    assert client.calls[0]["follow_redirects"] is True
    assert client.calls[0]["timeout"] == 40
    assert "text/plain" in client.calls[0]["headers"]["Accept"]


@pytest.mark.asyncio
async def test_web_fetch_rejects_oversized_and_binary_responses() -> None:
    oversized = WebFetchTool(
        client=FakeClient(
            make_response(b"small", headers={"content-length": str(_MAX_BYTES + 1)})
        )
    )
    binary = WebFetchTool(
        client=FakeClient(make_response(b"pdf", content_type="application/pdf"))
    )

    too_large = json.loads(await oversized.execute(url="https://example.com/large"))
    unsupported = json.loads(await binary.execute(url="https://example.com/file.pdf"))

    assert "超过 5MB" in too_large["error"]
    assert "二进制内容" in unsupported["error"]


@pytest.mark.asyncio
async def test_web_fetch_truncates_long_text_with_metadata() -> None:
    tool = WebFetchTool(client=FakeClient(make_response(b"x" * (_MAX_TEXT_CHARS + 10))))

    result = json.loads(await tool.execute(url="https://example.com/long"))

    assert result["length"] == _MAX_TEXT_CHARS
    assert result["truncated"] is True
    assert len(result["text"]) == _MAX_TEXT_CHARS


@pytest.mark.asyncio
async def test_web_fetch_maps_timeout_to_stable_error() -> None:
    request = httpx.Request("GET", "https://example.com/slow")
    tool = WebFetchTool(client=FakeClient(httpx.ReadTimeout("slow", request=request)))

    result = json.loads(await tool.execute(url="https://example.com/slow", timeout=7))

    assert result == {"error": "请求超时（>7s）", "url": "https://example.com/slow"}
