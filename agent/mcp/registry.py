"""MCP 服务注册、传输生命周期、工具注入和配置持久化。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from agent.mcp.client import HttpMcpClient, McpClient, McpToolInfo
from agent.mcp.tool import McpToolWrapper
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_TRANSPORTS = {"stdio", "http", "sse"}


class McpRevisionConflict(RuntimeError):
    """前端基于旧摘要写入时抛出的并发冲突。"""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"MCP 配置已被更新（期望 revision={expected}，当前 revision={actual}）")
        self.expected = expected
        self.actual = actual


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
    """管理 MCP 服务完整生命周期，并保持配置、连接和工具目录一致。"""

    def __init__(
        self,
        config_path: Path,
        tool_registry: ToolRegistry,
        *,
        client_factory: McpClientFactory = McpClient,
        default_scope: str = "workspace",
        secret_store: Any | None = None,
        tool_namespace: str = "",
        source_id: str = "",
    ) -> None:
        self._config_path = Path(config_path)
        self._tools = tool_registry
        self._client_factory = client_factory
        self._default_scope = default_scope if default_scope in {"user", "workspace"} else "workspace"
        self._secret_store = secret_store
        namespace = str(tool_namespace or "").strip()
        if namespace and not _TOOL_NAME.fullmatch(namespace.rstrip("_")):
            raise ValueError("MCP 工具命名空间只能包含字母、数字、下划线和连字符")
        self._tool_namespace = f"{namespace.rstrip('_')}_" if namespace else ""
        self._source_id = str(source_id or "").strip()
        self._clients: dict[str, McpClientApi] = {}
        self._server_tools: dict[str, list[str]] = {}
        self._configs: dict[str, dict[str, Any]] = {}
        self._statuses: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False

    async def load_and_connect_all(self) -> None:
        """恢复启用服务；单个服务失败不能阻塞其它服务。"""

        async with self._lifecycle_lock:
            self._configs = self._load_raw_configs()
            for name, config in self._configs.items():
                if not self._enabled(config):
                    self._statuses[name] = "disabled"
                    continue
                try:
                    await self._connect_config(name, config)
                except Exception as error:
                    self._mark_error(name, error)
                    logger.error("恢复 MCP server 失败 name=%s error=%s", name, error)

    async def add(
        self,
        name: str,
        command: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> str:
        """兼容 Agent 工具的 stdio 快捷入口。"""

        config = {
            "type": "stdio",
            "command": list(command),
            "env": dict(env or {}),
            "cwd": cwd,
            "enabled": True,
        }
        try:
            names = await self.create(name, config)
        except Exception as error:
            return f"连接 MCP server {name!r} 失败：{error}"
        return (
            f"已连接 MCP server {name!r}，注册了 {len(names)} 个工具：\n"
            + "\n".join(f"- {tool_name}" for tool_name in names)
        )

    async def create(self, name: str, config: dict[str, Any]) -> list[str]:
        """验证、连接、发现工具后原子创建服务。"""

        normalized_name = self._validate_name(name)
        normalized_config = self._normalize_config(config)
        now = _utc_now()
        normalized_config["revision"] = 1
        normalized_config.setdefault("createdAt", now)
        normalized_config["updatedAt"] = now
        async with self._lifecycle_lock:
            self._ensure_open()
            if normalized_name in self._configs or normalized_name in self._clients:
                raise ValueError(f"MCP server {normalized_name!r} 已存在")
            names = (
                await self._connect_config(normalized_name, normalized_config)
                if self._enabled(normalized_config)
                else []
            )
            if not self._enabled(normalized_config):
                self._statuses[normalized_name] = "disabled"
            self._configs[normalized_name] = normalized_config
            try:
                self._save()
            except Exception:
                await self._disconnect_server(normalized_name)
                self._configs.pop(normalized_name, None)
                raise
            return names

    async def update(
        self,
        name: str,
        config: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> list[str]:
        """验证新连接后替换服务；失败时恢复旧连接和配置。"""

        normalized_name = self._validate_name(name)
        normalized_config = self._normalize_config(config)
        async with self._lifecycle_lock:
            self._ensure_open()
            old_config = dict(self._configs.get(normalized_name) or {})
            if not old_config and normalized_name not in self._clients:
                raise KeyError(f"MCP server {normalized_name!r} 不存在")
            actual_revision = _config_revision(old_config)
            if expected_revision is not None and expected_revision != actual_revision:
                raise McpRevisionConflict(expected_revision, actual_revision)
            # 前端只能拿到脱敏后的环境变量/请求头名称；未显式提交这些字段时沿用旧值，
            # 防止编辑普通字段意外清空凭据。
            if "env" not in config and "env" in old_config:
                normalized_config["env"] = dict(old_config.get("env") or {})
            if "headers" not in config and "headers" in old_config:
                normalized_config["headers"] = dict(old_config.get("headers") or {})
            if "oauth" not in config and "oauth" in old_config:
                normalized_config["oauth"] = old_config["oauth"]
            old_was_connected = normalized_name in self._clients
            if old_was_connected:
                await self._disconnect_server(normalized_name)
            try:
                names = (
                    await self._connect_config(normalized_name, normalized_config)
                    if self._enabled(normalized_config)
                    else []
                )
                normalized_config["revision"] = actual_revision + 1
                normalized_config["createdAt"] = old_config.get("createdAt") or _utc_now()
                normalized_config["updatedAt"] = _utc_now()
                self._configs[normalized_name] = normalized_config
                self._save()
                if not self._enabled(normalized_config):
                    self._statuses[normalized_name] = "disabled"
                return names
            except Exception:
                await self._disconnect_server(normalized_name)
                self._configs[normalized_name] = old_config
                if old_was_connected and self._enabled(old_config):
                    try:
                        await self._connect_config(normalized_name, old_config)
                    except Exception as restore_error:
                        self._mark_error(normalized_name, restore_error)
                raise

    async def test(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        """用临时连接测试配置，不改变当前运行中的服务。"""

        normalized_name = self._validate_name(name)
        normalized_config = self._normalize_config(config)
        client = self._build_client(normalized_name, normalized_config)
        try:
            infos = await client.connect()
            return {
                "success": True,
                "transport": normalized_config["type"],
                "tool_count": len(infos),
                "tools": [info.name for info in infos],
            }
        except Exception as error:
            return {
                "success": False,
                "transport": normalized_config["type"],
                "error": str(error),
                "code": self._error_code(error),
            }
        finally:
            await client.disconnect()

    async def enable(self, name: str, *, expected_revision: int | None = None) -> list[str]:
        async with self._lifecycle_lock:
            self._ensure_known(name)
            config = dict(self._configs[name])
            actual_revision = _config_revision(config)
            if expected_revision is not None and expected_revision != actual_revision:
                raise McpRevisionConflict(expected_revision, actual_revision)
            config["enabled"] = True
            if name not in self._clients:
                names = await self._connect_config(name, config)
            else:
                names = list(self._server_tools.get(name, []))
            self._configs[name] = config
            config["revision"] = actual_revision + 1
            config["updatedAt"] = _utc_now()
            self._save()
            return names

    async def disable(self, name: str, *, expected_revision: int | None = None) -> None:
        async with self._lifecycle_lock:
            self._ensure_known(name)
            await self._disconnect_server(name)
            config = dict(self._configs[name])
            actual_revision = _config_revision(config)
            if expected_revision is not None and expected_revision != actual_revision:
                raise McpRevisionConflict(expected_revision, actual_revision)
            config["enabled"] = False
            config["revision"] = actual_revision + 1
            config["updatedAt"] = _utc_now()
            self._configs[name] = config
            self._statuses[name] = "disabled"
            self._save()

    async def refresh(self, name: str) -> dict[str, Any]:
        async with self._lifecycle_lock:
            self._ensure_known(name)
            config = self._configs[name]
            before = list(self._server_tools.get(name, []))
            if not self._enabled(config):
                self._statuses[name] = "disabled"
                return {"status": "disabled", "added": [], "removed": [], "unchanged": before}
            previous_client = self._clients.get(name)
            health_checked = False
            health_ok = True
            ping = getattr(previous_client, "ping", None)
            if previous_client is not None and callable(ping):
                health_checked = True
                try:
                    health_ok = bool(await ping())
                except Exception:
                    health_ok = False
                if not health_ok:
                    self._statuses[name] = "disconnected"
                    self._errors[name] = "MCP 健康检查失败，正在尝试重连"
            await self._disconnect_server(name)
            try:
                await self._connect_config(name, config)
            except Exception as error:
                self._mark_error(name, error)
                raise
            after = list(self._server_tools.get(name, []))
            before_set, after_set = set(before), set(after)
            return {
                "status": "connected",
                "added": sorted(after_set - before_set),
                "removed": sorted(before_set - after_set),
                "unchanged": sorted(before_set & after_set),
                "health_checked": health_checked,
                "reconnected": health_checked and not health_ok,
            }

    async def remove(self, name: str, *, expected_revision: int | None = None) -> str:
        """注销工具、关闭连接并删除配置。"""

        normalized_name = str(name or "").strip()
        async with self._lifecycle_lock:
            if normalized_name not in self._configs and normalized_name not in self._clients:
                return f"MCP server {normalized_name!r} 不存在"
            if expected_revision is not None:
                actual_revision = _config_revision(self._configs.get(normalized_name) or {})
                if expected_revision != actual_revision:
                    raise McpRevisionConflict(expected_revision, actual_revision)
            await self._disconnect_server(normalized_name)
            self._configs.pop(normalized_name, None)
            self._statuses.pop(normalized_name, None)
            self._errors.pop(normalized_name, None)
            self._save()
            return f"已注销 MCP server {normalized_name!r}"

    def list_servers(self) -> str:
        """只展示连接与工具信息，绝不回显环境变量。"""

        if not self._clients:
            return "当前没有已注册的 MCP server"
        return "\n".join(
            f"- {name}（{len(self._server_tools.get(name, []))} 个工具）："
            f"{', '.join(self._server_tools.get(name, [])) or '无'}"
            for name in self._clients
        )

    def connected_server_names(self) -> set[str]:
        return set(self._clients)

    def list_server_records(self, *, scope: str = "workspace") -> list[dict[str, Any]]:
        """返回扩展页安全摘要，不暴露环境变量值和完整命令。"""

        records: list[dict[str, Any]] = []
        names = sorted(set(self._configs) | set(self._clients))
        for name in names:
            config = self._configs.get(name, {})
            client = self._clients.get(name)
            record_scope = str(config.get("scope") or scope)
            transport = str(config.get("type") or ("stdio" if config.get("command") else "http"))
            command = list(config.get("command") or getattr(client, "command", None) or [])
            env = dict(config.get("env") or getattr(client, "env", None) or {})
            url = str(config.get("url") or getattr(client, "url", None) or "")
            enabled = self._enabled(config)
            status = self._statuses.get(name)
            if status == "connected" and client is not None and getattr(client, "connected", True) is False:
                status = "disconnected"
                self._statuses[name] = status
            if status is None:
                status = "connected" if name in self._clients else ("disabled" if not enabled else "disconnected")
            records.append({
                "id": name,
                "name": name,
                "description": str(config.get("description") or "本地 MCP 工具服务"),
                "scope": record_scope,
                "transport": transport,
                "status": status,
                "enabled": enabled,
                "tool_count": len(self._server_tools.get(name, [])),
                "command": " ".join(command[:3]) if command else "",
                "url": url,
                "cwd": str(config.get("cwd") or "") or None,
                "env_names": sorted(str(key) for key in env),
                "header_names": sorted(str(key) for key in dict(config.get("headers") or {})),
                "tools": list(self._server_tools.get(name, [])),
                "error": self._errors.get(name),
                "timeout_ms": config.get("timeoutMs"),
                "revision": _config_revision(config),
                "created_at": config.get("createdAt"),
                "updated_at": config.get("updatedAt"),
                "oauth_configured": isinstance(config.get("oauth"), dict),
                "authorization_required": bool(config.get("oauth", {}).get("authorization_required")) if isinstance(config.get("oauth"), dict) else False,
            })
        return records

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            await asyncio.gather(
                *(self._disconnect_server(name) for name in list(self._clients)),
                return_exceptions=True,
            )

    async def _connect_config(self, name: str, config: dict[str, Any]) -> list[str]:
        self._statuses[name] = "connecting"
        client = self._build_client(name, config)
        try:
            infos = await client.connect()
            wrappers: list[McpToolWrapper] = []
            generated: set[str] = set()
            for info in infos:
                if not _TOOL_NAME.fullmatch(info.name):
                    raise RuntimeError(f"远端工具名称无效: {info.name!r}")
                wrapper = McpToolWrapper(
                    client,
                    info,
                    server_name=name,
                    tool_namespace=self._tool_namespace,
                )
                if wrapper.name in generated or self._tools.has_tool(wrapper.name):
                    raise RuntimeError(f"工具名称冲突: {wrapper.name}")
                generated.add(wrapper.name)
                wrappers.append(wrapper)
            for wrapper in wrappers:
                self._tools.register(
                    wrapper,
                    risk="external-side-effect",
                    always_on=False,
                    source_type="mcp",
                    source_name=f"{self._source_id}:{name}" if self._source_id else name,
                )
            names = [wrapper.name for wrapper in wrappers]
            self._clients[name] = client
            self._server_tools[name] = names
            self._statuses[name] = "connected"
            self._errors.pop(name, None)
            return names
        except BaseException:
            await client.disconnect()
            self._statuses[name] = "error"
            raise

    def _build_client(self, name: str, config: dict[str, Any]) -> McpClientApi:
        transport = str(config.get("type") or "stdio")
        if transport == "stdio":
            factory_kwargs: dict[str, Any] = {
                "name": name,
                "command": list(config.get("command") or []),
                "env": dict(config.get("env") or {}),
                "cwd": str(config.get("cwd") or "") or None,
            }
            if config.get("timeoutMs") is not None:
                factory_kwargs["timeout_ms"] = config["timeoutMs"]
            try:
                return self._client_factory(**factory_kwargs)
            except TypeError:
                # 保持第三方测试/适配器工厂的旧签名兼容；内置客户端会
                # 接收 timeout_ms 并贯穿握手和工具调用。
                factory_kwargs.pop("timeout_ms", None)
                return self._client_factory(**factory_kwargs)
        return cast(
            McpClientApi,
            HttpMcpClient(
                name=name,
                url=str(config.get("url") or ""),
                transport=transport,
                headers=dict(config.get("headers") or config.get("http_headers") or {}),
                timeout_ms=config.get("timeoutMs"),
                oauth=dict(config.get("oauth") or {}) if isinstance(config.get("oauth"), dict) else None,
                secret_store=self._secret_store,
            ),
        )

    async def _disconnect_server(self, name: str) -> None:
        for tool_name in self._server_tools.pop(name, []):
            self._tools.unregister(tool_name)
        client = self._clients.pop(name, None)
        if client is not None:
            await client.disconnect()
        if name in self._configs and not self._enabled(self._configs[name]):
            self._statuses[name] = "disabled"
        elif name in self._configs:
            self._statuses[name] = "disconnected"

    def _load_raw_configs(self) -> dict[str, dict[str, Any]]:
        if not self._config_path.exists():
            return {}
        try:
            payload = json.loads(self._config_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("MCP 配置根节点必须是对象")
            servers: Any = payload.get("servers")
            if not isinstance(servers, dict):
                servers = payload.get("mcpServers")
            if not isinstance(servers, dict) and isinstance(payload.get("mcp"), dict):
                servers = payload["mcp"].get("servers")
            # 早期版本把全局配置放在 common 下；仅作为用户级注册表的
            # 迁移输入读取，后续保存统一写回 servers 结构。
            if not isinstance(servers, dict) and isinstance(payload.get("common"), dict):
                servers = payload["common"]
            if not isinstance(servers, dict):
                raise ValueError("MCP servers 必须是对象")
            return {
                str(name): self._normalize_config(cast(dict[str, Any], config), allow_missing=True)
                for name, config in servers.items()
                if isinstance(config, dict)
            }
        except Exception as error:
            logger.error("读取 MCP 配置失败 path=%s error=%s", self._config_path, type(error).__name__)
            return {}

    def _save(self) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._config_path.with_suffix(self._config_path.suffix + ".tmp")
        content = json.dumps({"servers": self._configs}, ensure_ascii=False, indent=2)
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                # 某些虚拟文件系统不提供 fsync；原子替换仍然有效。
                pass
        temporary.replace(self._config_path)

    def _normalize_config(self, config: dict[str, Any], *, allow_missing: bool = False) -> dict[str, Any]:
        normalized = dict(config)
        normalized["scope"] = normalized.get("scope") if normalized.get("scope") in {"user", "workspace"} else self._default_scope
        transport = str(normalized.get("type") or ("stdio" if normalized.get("command") else "http"))
        if transport not in _TRANSPORTS:
            raise ValueError(f"不支持的 MCP 传输类型: {transport}")
        normalized["type"] = transport
        normalized["enabled"] = bool(normalized.get("enabled", True))
        oauth = normalized.get("oauth")
        if oauth is not None and not isinstance(oauth, dict):
            raise ValueError("MCP OAuth 配置必须是对象")
        normalized["revision"] = _config_revision(normalized)
        normalized["createdAt"] = str(normalized.get("createdAt") or _utc_now())
        normalized["updatedAt"] = str(normalized.get("updatedAt") or normalized["createdAt"])
        if transport == "stdio":
            command = normalized.get("command")
            if isinstance(command, str):
                command = command.split()
            if not allow_missing and (not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command)):
                raise ValueError("MCP 启动命令必须是非空字符串数组")
            normalized["command"] = list(command or [])
            env = normalized.get("env") or {}
            if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
                raise ValueError("MCP 环境变量必须是字符串键值")
            normalized["env"] = dict(env)
            normalized["cwd"] = str(normalized.get("cwd") or "") or None
        else:
            url = str(normalized.get("url") or "").strip()
            if not allow_missing and not url.startswith(("http://", "https://")):
                raise ValueError("MCP URL 必须使用 http 或 https")
            normalized["url"] = url
            headers = normalized.get("headers") or normalized.get("http_headers") or {}
            if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
                raise ValueError("MCP 请求头必须是字符串键值")
            normalized["headers"] = dict(headers)
            normalized.pop("http_headers", None)
        timeout = normalized.get("timeoutMs")
        if timeout is not None:
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
                raise ValueError("MCP timeoutMs 必须是正整数")
            normalized["timeoutMs"] = timeout
        return normalized

    @staticmethod
    def _enabled(config: dict[str, Any]) -> bool:
        return config.get("enabled", True) is not False

    @staticmethod
    def _validate_name(name: str) -> str:
        normalized = str(name or "").strip()
        if not _SERVER_NAME.fullmatch(normalized):
            raise ValueError("MCP server 名称只能包含字母、数字、下划线和连字符")
        return normalized

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("MCP registry 已关闭")

    def _ensure_known(self, name: str) -> None:
        if name not in self._configs:
            raise KeyError(f"MCP server {name!r} 不存在")

    def _mark_error(self, name: str, error: BaseException) -> None:
        self._statuses[name] = "error"
        self._errors[name] = str(error)

    @staticmethod
    def _error_code(error: BaseException) -> str:
        if isinstance(error, TimeoutError):
            return "connection_timeout"
        if isinstance(error, (ValueError, json.JSONDecodeError)):
            return "config_invalid"
        if isinstance(error, ConnectionError):
            return "network_unreachable"
        return "connection_failed"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _config_revision(config: dict[str, Any]) -> int:
    value = config.get("revision", 1)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1


__all__ = ["McpRevisionConflict", "McpServerRegistry"]
