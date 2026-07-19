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
    ) -> None:
        self.name = str(name)
        self.command = list(command)
        self.env = dict(env or {})
        self.cwd = cwd or _infer_cwd(self.command)
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
            return await asyncio.wait_for(self._connect_impl(), _CONNECT_TIMEOUT)
        except BaseException:
            await self.disconnect()
            raise

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
                timeout=timeout or _CALL_TIMEOUT,
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


__all__ = ["McpClient", "McpToolInfo"]
