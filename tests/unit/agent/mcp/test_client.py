"""stdio MCP 客户端协议与生命周期测试。"""

from __future__ import annotations

import json
import asyncio
import sys
from pathlib import Path

import pytest
import httpx

from agent.mcp.client import HttpMcpClient, McpClient


@pytest.mark.asyncio
async def test_client_handshake_lists_tools_calls_and_disconnects(tmp_path: Path) -> None:
    log_path = tmp_path / "requests.jsonl"
    server = Path(__file__).parents[3] / "fixtures" / "stdio_mcp_server.py"
    client = McpClient(
        name="demo",
        command=[sys.executable, "-u", str(server)],
        env={"BEANAGENT_MCP_TEST_LOG": str(log_path)},
    )

    tools = await client.connect()
    result = await client.call("echo", {"text": "hello"})
    await client.disconnect()
    await client.disconnect()

    requests = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [request["method"] for request in requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert requests[0]["params"]["protocolVersion"] == "2024-11-05"
    assert [(tool.name, tool.description) for tool in tools] == [("echo", "回显文本")]
    assert result == "echo:hello"
    assert client.connected is False


@pytest.mark.asyncio
async def test_client_connect_failure_cleans_up_process() -> None:
    client = McpClient(
        name="broken",
        command=[sys.executable, "-u", "-c", "raise SystemExit(2)"],
    )

    with pytest.raises(Exception):
        await client.connect()

    assert client.connected is False


@pytest.mark.asyncio
async def test_http_client_discovers_and_calls_tools_without_leaking_headers() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read()
        body = json.loads(payload.decode("utf-8"))
        requests.append(body)
        method = body.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05"}
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "description": "回显", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}]}
        elif method == "ping":
            result = {}
        else:
            return httpx.Response(204)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": result})

    client = HttpMcpClient(
        "remote",
        "https://example.test/mcp",
        headers={"Authorization": "Bearer secret"},
        client_factory=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs),
    )
    tools = await client.connect()
    assert [tool.name for tool in tools] == ["echo"]
    assert await client.call("echo", {"text": "hello"}) == "ok"
    assert await client.ping() is True
    await client.disconnect()
    assert [item["method"] for item in requests] == ["initialize", "notifications/initialized", "tools/list", "tools/call", "ping"]


@pytest.mark.asyncio
async def test_sse_client_uses_endpoint_and_event_stream() -> None:
    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"event: endpoint\ndata: /messages\n\n"
            while True:
                payload = await queue.get()
                yield f"event: message\ndata: {json.dumps(payload)}\n\n".encode()

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
        body = json.loads(request.content.decode("utf-8"))
        method = body.get("method")
        result: dict[str, object]
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05"}
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "description": "回显", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}]}
        elif method == "ping":
            result = {}
        else:
            return httpx.Response(202)
        queue.put_nowait({"jsonrpc": "2.0", "id": body.get("id"), "result": result})
        return httpx.Response(202)

    client = HttpMcpClient(
        "sse-remote",
        "https://example.test/sse",
        transport="sse",
        client_factory=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs),
    )
    tools = await client.connect()
    assert [tool.name for tool in tools] == ["echo"]
    assert await client.call("echo", {"text": "hello"}) == "ok"
    assert await client.ping() is True
    await client.disconnect()


@pytest.mark.asyncio
async def test_http_client_client_credentials_uses_secret_store_without_persisting_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "temporary-token"})
        body = json.loads(request.content.decode("utf-8"))
        if body.get("method") == "initialize":
            result = {"protocolVersion": "2024-11-05"}
        elif body.get("method") == "tools/list":
            result = {"tools": []}
        else:
            result = {}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": result})

    class Secrets:
        def get(self, ref: str) -> str | None:
            assert ref == "mcp/demo/client-secret"
            return "secret-value"

    client = HttpMcpClient(
        "oauth-remote",
        "https://example.test/mcp",
        headers={"X-Test": "yes"},
        oauth={"mode": "client_credentials", "token_url": "https://example.test/oauth/token", "client_id": "beanagent", "client_secret_ref": "mcp/demo/client-secret"},
        secret_store=Secrets(),
        client_factory=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs),
    )
    await client.connect()
    assert requests[0].url.path == "/oauth/token"
    assert requests[-1].headers.get("authorization") == "Bearer temporary-token"
    assert "access_token" not in client._oauth
    await client.disconnect()
