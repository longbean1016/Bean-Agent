"""单个 stdio MCP Server 的子进程与 JSON-RPC 通信。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 8.0
_CALL_TIMEOUT = 30.0
_DISCONNECT_TIMEOUT = 5.0
_STREAM_LIMIT = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class McpToolInfo:
    """远端工具在本地注册所需的最小描述。"""

    name: str
    description: str
    input_schema: dict[str, Any]


def _infer_cwd(command: list[str]) -> str | None:
    """从绝对脚本路径推断工作目录，避免子进程依赖应用启动位置。"""

    for argument in command:
        path = Path(argument)
        if path.is_absolute() and path.is_file():
            return str(path.parent)
    return None


class McpClient:
    """拥有一个 MCP 子进程，并串行配对请求与响应。"""

    def __init__(
        self,
        name: str,
        command: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_ms: int | None = None,
    ) -> None:
        self.name = str(name)
        self.command = list(command)
        self.env = dict(env or {})
        self.cwd = cwd or _infer_cwd(self.command)
        self._connect_timeout = max(1.0, float(timeout_ms or _CONNECT_TIMEOUT * 1000) / 1000)
        self._call_timeout = max(1.0, float(timeout_ms or _CALL_TIMEOUT * 1000) / 1000)
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_id = 1
        # stdio 是单一响应流。串行调用可保证通知与响应跳过后，期望 ID 仍有
        # 唯一读取者，不需要额外常驻分发任务。
        self._call_lock = asyncio.Lock()
        self._tool_infos: list[McpToolInfo] = []
        self._recent_stdout: deque[str] = deque(maxlen=8)
        self._recent_stderr: deque[str] = deque(maxlen=8)

    @property
    def connected(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def tool_infos(self) -> list[McpToolInfo]:
        return list(self._tool_infos)

    async def connect(self) -> list[McpToolInfo]:
        """启动子进程并在限定时间内完成握手与工具发现。"""

        if not self.command:
            raise ValueError("MCP 启动命令不能为空")
        try:
            return await asyncio.wait_for(self._connect_impl(), self._connect_timeout)
        except BaseException:
            await self.disconnect()
            raise

    async def reconnect(self) -> list[McpToolInfo]:
        """在一次受控重试中重建 stdio 进程和工具目录。"""

        await self.disconnect()
        return await self.connect()

    async def _connect_impl(self) -> list[McpToolInfo]:
        process_env = {**os.environ, **self.env}
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=process_env,
            cwd=self.cwd,
            limit=_STREAM_LIMIT,
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(),
            name=f"mcp-stderr:{self.name}",
        )

        initialize_id = self._new_id()
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": initialize_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "beanagent", "version": "1.0"},
                },
            }
        )
        self._response_result(
            await self._recv(initialize_id, "initialize"),
            "initialize",
        )
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        list_id = self._new_id()
        await self._send(
            {"jsonrpc": "2.0", "id": list_id, "method": "tools/list", "params": {}}
        )
        result = self._response_result(
            await self._recv(list_id, "tools/list"),
            "tools/list",
        )
        raw_tools = result.get("tools", [])
        if not isinstance(raw_tools, list):
            raise RuntimeError(f"MCP server {self.name!r} 返回的 tools 不是列表")
        parsed: list[McpToolInfo] = []
        for raw_tool in raw_tools:
            if not isinstance(raw_tool, dict):
                raise RuntimeError(f"MCP server {self.name!r} 返回了无效工具")
            tool = cast(dict[str, Any], raw_tool)
            name = tool.get("name")
            schema = tool.get("inputSchema", {"type": "object", "properties": {}})
            if not isinstance(name, str) or not name or not isinstance(schema, dict):
                raise RuntimeError(f"MCP server {self.name!r} 返回了无效工具")
            parsed.append(
                McpToolInfo(
                    name=name,
                    description=str(tool.get("description") or ""),
                    input_schema=cast(dict[str, Any], schema),
                )
            )
        self._tool_infos = parsed
        return list(parsed)

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> str:
        """调用远端工具并把文本 content 块规范成单个字符串。"""

        async with self._call_lock:
            call_id = self._new_id()
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": call_id,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                }
            )
            response = await self._recv(
                call_id,
                f"tools/call:{tool_name}",
                timeout=timeout or self._call_timeout,
            )
        if "error" in response:
            error = response["error"]
            message = error.get("message", error) if isinstance(error, dict) else error
            return f"MCP error ({self.name}/{tool_name}): {message}"
        result = response.get("result", {})
        content = result.get("content", []) if isinstance(result, dict) else []
        if isinstance(content, list):
            return "\n".join(
                str(block.get("text", block)) if isinstance(block, dict) else str(block)
                for block in content
            )
        return str(result)

    async def disconnect(self) -> None:
        """幂等终止子进程，并等待 stderr 读取任务退出。"""

        process = self._process
        self._process = None
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), _DISCONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._stderr_task is not None:
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

    def _new_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    async def _send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise ConnectionError(f"MCP server {self.name!r} 未连接")
        process.stdin.write(
            (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        )
        await process.stdin.drain()

    async def _recv(
        self,
        expected_id: int,
        stage: str,
        *,
        timeout: float = _CALL_TIMEOUT,
    ) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                self._recv_impl(expected_id, stage),
                timeout,
            )
        except asyncio.TimeoutError as error:
            diagnostics = " | ".join([*self._recent_stdout, *self._recent_stderr])
            raise TimeoutError(
                f"MCP server {self.name!r} 在 {stage} 等待响应超时"
                + (f": {diagnostics}" if diagnostics else "")
            ) from error

    async def _recv_impl(self, expected_id: int, stage: str) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise ConnectionError(f"MCP server {self.name!r} 未连接")
        while True:
            raw = await process.stdout.readline()
            if not raw:
                raise ConnectionError(
                    f"MCP server {self.name!r} 在 {stage} 意外关闭 stdout"
                )
            text = raw.decode("utf-8", errors="replace").strip()
            self._recent_stdout.append(text[:400])
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                raise RuntimeError(f"MCP server {self.name!r} 返回了非对象响应")
            if "id" not in payload:
                continue
            if payload.get("id") != expected_id:
                logger.debug(
                    "忽略 MCP 非预期响应 server=%s id=%r expected=%r",
                    self.name,
                    payload.get("id"),
                    expected_id,
                )
                continue
            return cast(dict[str, Any], payload)

    @staticmethod
    def _response_result(response: dict[str, Any], stage: str) -> dict[str, Any]:
        if "error" in response:
            raise RuntimeError(f"MCP {stage} 失败: {response['error']}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"MCP {stage} 返回了无效 result")
        return cast(dict[str, Any], result)

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            raw = await process.stderr.readline()
            if not raw:
                return
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                self._recent_stderr.append(text[:400])
                logger.debug("MCP stderr server=%s message=%s", self.name, text[:400])


class HttpMcpClient:
    """通过 MCP HTTP 传输连接 Streamable HTTP 或兼容的 SSE 服务。"""

    def __init__(
        self,
        name: str,
        url: str,
        *,
        transport: str = "http",
        headers: dict[str, str] | None = None,
        timeout_ms: int | None = None,
        client_factory: Any | None = None,
        oauth: dict[str, Any] | None = None,
        secret_store: Any | None = None,
    ) -> None:
        self.name = str(name)
        self.url = str(url)
        self.transport = transport
        self.headers = dict(headers or {})
        self.command: list[str] = []
        self.env: dict[str, str] = {}
        self.cwd: str | None = None
        self.timeout = max(1.0, float(timeout_ms or _CALL_TIMEOUT * 1000) / 1000)
        self._client_factory = client_factory or httpx.AsyncClient
        self._oauth = dict(oauth or {})
        self._secret_store = secret_store
        self._http: httpx.AsyncClient | None = None
        self._next_id = 1
        self._tool_infos: list[McpToolInfo] = []
        self._sse_response: httpx.Response | None = None
        self._sse_task: asyncio.Task[None] | None = None
        self._sse_endpoint: str | None = None
        self._sse_endpoint_ready = asyncio.Event()
        self._sse_error: BaseException | None = None
        self._sse_pending: dict[int, asyncio.Future[dict[str, Any]]] = {}

    @property
    def connected(self) -> bool:
        return self._http is not None

    @property
    def tool_infos(self) -> list[McpToolInfo]:
        return list(self._tool_infos)

    async def connect(self) -> list[McpToolInfo]:
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("MCP URL 必须使用 http 或 https")
        self._http = self._client_factory(
            timeout=self.timeout,
            headers={"accept": "application/json, text/event-stream", **self.headers},
        )
        try:
            await self._prepare_oauth()
            if self.transport == "sse":
                await self._start_sse_stream()
            await self._request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "beanagent", "version": "1.0"},
                },
            )
            await self._notify("notifications/initialized")
            result = await self._request("tools/list", {})
            raw_tools = result.get("tools", [])
            if not isinstance(raw_tools, list):
                raise RuntimeError("MCP HTTP 返回的 tools 不是列表")
            parsed: list[McpToolInfo] = []
            for raw_tool in raw_tools:
                if not isinstance(raw_tool, dict):
                    raise RuntimeError("MCP HTTP 返回了无效工具")
                name = raw_tool.get("name")
                schema = raw_tool.get("inputSchema", {"type": "object", "properties": {}})
                if not isinstance(name, str) or not name or not isinstance(schema, dict):
                    raise RuntimeError("MCP HTTP 返回了无效工具")
                parsed.append(
                    McpToolInfo(
                        name=name,
                        description=str(raw_tool.get("description") or ""),
                        input_schema=cast(dict[str, Any], schema),
                    )
                )
            self._tool_infos = parsed
            return list(parsed)
        except BaseException:
            await self.disconnect()
            raise

    async def _prepare_oauth(self) -> None:
        if not self._oauth or self._oauth.get("mode") != "client_credentials":
            return
        token_url = str(self._oauth.get("token_url") or "").strip()
        client_id = str(self._oauth.get("client_id") or "").strip()
        secret_ref = str(self._oauth.get("client_secret_ref") or "").strip()
        if not token_url.startswith(("http://", "https://")) or not client_id or not secret_ref:
            raise ValueError("MCP OAuth client credentials 配置不完整")
        if self._secret_store is None:
            raise RuntimeError("MCP OAuth 凭据存储不可用")
        client_secret = self._secret_store.get(secret_ref)
        if not client_secret:
            raise RuntimeError("MCP OAuth 尚未配置 client secret")
        token_client = self._client_factory(timeout=self.timeout)
        try:
            response = await token_client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    **({"scope": str(self._oauth["scope"])} if self._oauth.get("scope") else {}),
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"MCP OAuth token 请求失败: HTTP {response.status_code}")
            payload = response.json()
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                raise RuntimeError("MCP OAuth token 响应缺少 access_token")
        finally:
            await token_client.aclose()
        self.headers["Authorization"] = f"Bearer {token}"
        mcp_client = self._http
        if mcp_client is not None:
            mcp_client.headers["Authorization"] = f"Bearer {token}"

    async def reconnect(self) -> list[McpToolInfo]:
        """在一次受控重试中重建 HTTP 会话，避免无限重连。"""

        await self.disconnect()
        return await self.connect()

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> str:
        result = await self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=timeout,
        )
        content = result.get("content", [])
        if isinstance(content, list):
            return "\n".join(
                str(block.get("text", block)) if isinstance(block, dict) else str(block)
                for block in content
            )
        return str(result)

    async def ping(self) -> bool:
        try:
            await self._request("ping", {}, timeout=min(self.timeout, 5.0))
            return True
        except Exception:
            return False

    async def disconnect(self) -> None:
        if self._sse_task is not None:
            self._sse_task.cancel()
            await asyncio.gather(self._sse_task, return_exceptions=True)
            self._sse_task = None
        if self._sse_response is not None:
            await self._sse_response.aclose()
            self._sse_response = None
        for future in self._sse_pending.values():
            if not future.done():
                future.set_exception(ConnectionError(f"MCP server {self.name!r} 已断开"))
        self._sse_pending.clear()
        client = self._http
        self._http = None
        if client is not None:
            await client.aclose()

    async def _notify(self, method: str) -> None:
        client = self._http
        if client is None:
            raise ConnectionError(f"MCP server {self.name!r} 未连接")
        target = await self._sse_target() if self.transport == "sse" else self.url
        response = await client.post(target, json={"jsonrpc": "2.0", "method": method})
        if response.status_code >= 400:
            raise RuntimeError(f"MCP {method} 失败: HTTP {response.status_code}")

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        client = self._http
        if client is None:
            raise ConnectionError(f"MCP server {self.name!r} 未连接")
        request_id = self._next_id
        self._next_id += 1
        if self.transport == "sse":
            target = await self._sse_target()
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._sse_pending[request_id] = future
            try:
                response = await client.post(
                    target,
                    json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"MCP {method} 失败: HTTP {response.status_code}")
                # 部分兼容 SSE 服务会直接在 POST 响应返回 JSON；优先消费它，
                # 标准 legacy SSE 则由常驻事件流投递到 future。
                if response.content:
                    payload = response.json()
                    if isinstance(payload, dict) and not future.done():
                        future.set_result(cast(dict[str, Any], payload))
                response_payload = await asyncio.wait_for(future, timeout=timeout or self.timeout)
            finally:
                self._sse_pending.pop(request_id, None)
            if "error" in response_payload:
                raise RuntimeError(f"MCP {method} 失败: {response_payload['error']}")
            result = response_payload.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"MCP {method} 返回了无效 result")
            return cast(dict[str, Any], result)
        response = await client.post(
            self.url,
            json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            timeout=timeout or self.timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"MCP {method} 失败: HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"MCP {method} 返回了无效响应")
        if "error" in payload:
            raise RuntimeError(f"MCP {method} 失败: {payload['error']}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"MCP {method} 返回了无效 result")
        return cast(dict[str, Any], result)

    async def _start_sse_stream(self) -> None:
        client = self._http
        if client is None:
            raise ConnectionError(f"MCP server {self.name!r} 未连接")
        self._sse_endpoint = None
        self._sse_error = None
        self._sse_endpoint_ready.clear()
        request = client.build_request("GET", self.url, headers={"accept": "text/event-stream"})
        response = await client.send(request, stream=True)
        if response.status_code >= 400:
            await response.aclose()
            raise RuntimeError(f"MCP SSE 连接失败: HTTP {response.status_code}")
        self._sse_response = response
        self._sse_task = asyncio.create_task(self._consume_sse(), name=f"mcp-sse:{self.name}")
        try:
            await asyncio.wait_for(self._sse_endpoint_ready.wait(), self.timeout)
        except asyncio.TimeoutError as error:
            raise TimeoutError(f"MCP server {self.name!r} 未收到 SSE endpoint") from error
        if self._sse_error is not None:
            raise ConnectionError(f"MCP SSE 事件流已断开: {self._sse_error}")

    async def _sse_target(self) -> str:
        if self._sse_endpoint is None:
            try:
                await asyncio.wait_for(self._sse_endpoint_ready.wait(), self.timeout)
            except asyncio.TimeoutError as error:
                raise TimeoutError(f"MCP server {self.name!r} 未收到 SSE endpoint") from error
        if self._sse_error is not None:
            raise ConnectionError(f"MCP SSE 事件流已断开: {self._sse_error}")
        if self._sse_endpoint is None:
            raise ConnectionError(f"MCP server {self.name!r} SSE endpoint 不可用")
        return self._sse_endpoint

    async def _consume_sse(self) -> None:
        response = self._sse_response
        if response is None:
            return
        event_name = "message"
        data_lines: list[str] = []
        try:
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event_name = line[6:].strip() or "message"
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line.strip():
                    data = "\n".join(data_lines).strip()
                    data_lines = []
                    if data:
                        if event_name == "endpoint":
                            self._sse_endpoint = urljoin(self.url, data)
                            self._sse_endpoint_ready.set()
                        else:
                            try:
                                payload = json.loads(data)
                            except json.JSONDecodeError:
                                payload = None
                            if isinstance(payload, dict) and isinstance(payload.get("id"), int):
                                future = self._sse_pending.get(payload["id"])
                                if future is not None and not future.done():
                                    future.set_result(cast(dict[str, Any], payload))
                    event_name = "message"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._sse_error = error
            self._sse_endpoint_ready.set()
            for future in self._sse_pending.values():
                if not future.done():
                    future.set_exception(ConnectionError(f"MCP SSE 事件流已断开: {error}"))


__all__ = ["HttpMcpClient", "McpClient", "McpToolInfo"]
