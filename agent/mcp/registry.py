"""workspace 级 MCP Server 连接、工具注入和配置持久化。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from agent.mcp.client import McpClient, McpToolInfo
from agent.mcp.tool import McpToolWrapper
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class McpClientApi(Protocol):
    name: str
    command: list[str]
    env: dict[str, str]
    cwd: str | None

    async def connect(self) -> list[McpToolInfo]: ...
    async def call(self, name: str, arguments: dict[str, Any]) -> str: ...
    async def disconnect(self) -> None: ...


McpClientFactory = Callable[..., McpClientApi]


class McpServerRegistry:
    """管理用户 MCP 的完整生命周期，并保持工具目录与连接状态一致。"""

    def __init__(
        self,
        config_path: Path,
        tool_registry: ToolRegistry,
        *,
        client_factory: McpClientFactory = McpClient,
    ) -> None:
        self._config_path = Path(config_path)
        self._tools = tool_registry
        self._client_factory = client_factory
        self._clients: dict[str, McpClientApi] = {}
        self._server_tools: dict[str, list[str]] = {}
        # 添加、删除、恢复和关闭会同时修改连接及工具目录，必须作为一个事务串行。
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False

    async def load_and_connect_all(self) -> None:
        """从配置恢复服务；单个失败只记录日志，不阻止应用启动。"""

        configurations = self._load_raw_configs()
        for name, config in configurations.items():
            try:
                async with self._lifecycle_lock:
                    if self._closed:
                        return
                    await self._connect(
                        name,
                        list(config.get("command") or []),
                        dict(config.get("env") or {}),
                        str(config.get("cwd") or "") or None,
                    )
            except Exception as error:
                logger.error("恢复 MCP server 失败 name=%s error=%s", name, error)

    async def add(
        self,
        name: str,
        command: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> str:
        """连接并原子注入一个服务的全部工具，成功后再保存配置。"""

        normalized_name = str(name or "").strip()
        async with self._lifecycle_lock:
            if self._closed:
                return "MCP registry 已关闭"
            if not _SERVER_NAME.fullmatch(normalized_name):
                return "MCP server 名称只能包含字母、数字、下划线和连字符"
            if normalized_name in self._clients:
                return f"MCP server {normalized_name!r} 已存在，请先移除后再添加"
            if not command or not all(isinstance(item, str) and item for item in command):
                return "MCP 启动命令必须是非空字符串数组"
            if env is not None and not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in env.items()
            ):
                return "MCP 环境变量必须是字符串键值"
            try:
                names = await self._connect(normalized_name, command, env, cwd)
            except Exception as error:
                return f"连接 MCP server {normalized_name!r} 失败：{error}"
            try:
                self._save()
            except Exception as error:
                # 配置未落盘时不能保留仅当前进程可见的服务，否则重启后的行为不同。
                await self._disconnect_server(normalized_name)
                return f"保存 MCP server {normalized_name!r} 配置失败：{error}"
            return (
                f"已连接 MCP server {normalized_name!r}，注册了 {len(names)} 个工具：\n"
                + "\n".join(f"- {tool_name}" for tool_name in names)
            )

    async def remove(self, name: str) -> str:
        """注销工具、关闭 Client 并更新持久化配置。"""

        normalized_name = str(name or "").strip()
        async with self._lifecycle_lock:
            if normalized_name not in self._clients:
                return f"MCP server {normalized_name!r} 不存在"
            await self._disconnect_server(normalized_name)
            self._save()
            return f"已注销 MCP server {normalized_name!r}"

    def list_servers(self) -> str:
        """只展示连接与工具信息，绝不回显命令环境变量。"""

        if not self._clients:
            return "当前没有已注册的 MCP server"
        return "\n".join(
            f"- {name}（{len(self._server_tools.get(name, []))} 个工具）："
            f"{', '.join(self._server_tools.get(name, [])) or '无'}"
            for name in self._clients
        )

    def connected_server_names(self) -> set[str]:
        return set(self._clients)

    async def shutdown(self) -> None:
        """幂等释放全部工具与子进程，配置保留供下次启动恢复。"""

        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            names = list(self._clients)
            await asyncio.gather(
                *(self._disconnect_server(name) for name in names),
                return_exceptions=True,
            )

    async def _connect(
        self,
        name: str,
        command: list[str],
        env: dict[str, str] | None,
        cwd: str | None,
    ) -> list[str]:
        client = self._client_factory(
            name=name,
            command=list(command),
            env=dict(env or {}),
            cwd=cwd,
        )
        try:
            infos = await client.connect()
            wrappers: list[McpToolWrapper] = []
            generated: set[str] = set()
            for info in infos:
                if not _TOOL_NAME.fullmatch(info.name):
                    raise RuntimeError(f"远端工具名称无效: {info.name!r}")
                wrapper = McpToolWrapper(
                    cast(McpClient, client),
                    info,
                    server_name=name,
                )
                if wrapper.name in generated or self._tools.has_tool(wrapper.name):
                    raise RuntimeError(f"工具名称冲突: {wrapper.name}")
                generated.add(wrapper.name)
                wrappers.append(wrapper)

            # 预检完成后才统一写入 Registry，确保一个 Server 不会只注册部分工具。
            for wrapper in wrappers:
                self._tools.register(
                    wrapper,
                    risk="external-side-effect",
                    always_on=False,
                    source_type="mcp",
                    source_name=name,
                )
            names = [wrapper.name for wrapper in wrappers]
            self._clients[name] = client
            self._server_tools[name] = names
            return names
        except BaseException:
            await client.disconnect()
            raise

    async def _disconnect_server(self, name: str) -> None:
        for tool_name in self._server_tools.pop(name, []):
            self._tools.unregister(tool_name)
        client = self._clients.pop(name, None)
        if client is not None:
            await client.disconnect()

    def _load_raw_configs(self) -> dict[str, dict[str, Any]]:
        if not self._config_path.exists():
            return {}
        try:
            payload = json.loads(self._config_path.read_text(encoding="utf-8"))
            servers = payload.get("servers", {}) if isinstance(payload, dict) else {}
            if not isinstance(servers, dict):
                raise ValueError("servers 必须是对象")
            return {
                str(name): cast(dict[str, Any], config)
                for name, config in servers.items()
                if isinstance(config, dict)
            }
        except Exception as error:
            logger.error("读取 MCP 配置失败 path=%s error=%s", self._config_path, error)
            return {}

    def _save(self) -> None:
        servers = {
            name: {
                "command": list(client.command),
                "env": dict(client.env),
                "cwd": client.cwd,
            }
            for name, client in self._clients.items()
        }
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._config_path.with_suffix(self._config_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"servers": servers}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._config_path)


__all__ = ["McpServerRegistry"]
