"""项目级 MCP 注册表池与旧默认工作区配置迁移。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from agent.mcp.registry import McpClient, McpClientFactory, McpServerRegistry
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _server_map(payload: object) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("MCP 配置根节点必须是对象")
    servers: object = payload.get("servers")
    if not isinstance(servers, dict):
        servers = payload.get("mcpServers")
    if not isinstance(servers, dict) and isinstance(payload.get("mcp"), dict):
        servers = payload["mcp"].get("servers")
    if not isinstance(servers, dict):
        return {}
    return {
        str(name): dict(config)
        for name, config in servers.items()
        if isinstance(config, dict)
    }


def migrate_legacy_workspace_mcp(
    legacy_path: Path,
    user_path: Path,
    marker_path: Path,
) -> dict[str, Any]:
    """把旧默认工作区配置只迁移一次；同名用户配置始终优先。"""

    if marker_path.is_file() or not legacy_path.is_file():
        return {"migrated": [], "skipped": [], "status": "not_needed"}
    try:
        legacy_bytes = legacy_path.read_bytes()
        legacy = _server_map(json.loads(legacy_bytes.decode("utf-8")))
        user = _server_map(json.loads(user_path.read_text(encoding="utf-8"))) if user_path.is_file() else {}
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        logger.warning("旧 MCP 配置迁移暂缓: error=%s", type(error).__name__)
        return {"migrated": [], "skipped": [], "status": "invalid_source"}

    migrated: list[str] = []
    skipped: list[str] = []
    for name, config in legacy.items():
        if name in user:
            skipped.append(name)
            continue
        next_config = dict(config)
        next_config["scope"] = "user"
        user[name] = next_config
        migrated.append(name)

    try:
        if migrated:
            user_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = user_path.with_suffix(user_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps({"servers": user}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(user_path)
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_temporary = marker_path.with_suffix(marker_path.suffix + ".tmp")
        marker_temporary.write_text(
            json.dumps({
                "schema": 1,
                "legacy_hash": hashlib.sha256(legacy_bytes).hexdigest(),
                "migrated": migrated,
                "skipped": skipped,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        marker_temporary.replace(marker_path)
    except OSError as error:
        logger.warning("旧 MCP 配置迁移写入失败: error=%s", type(error).__name__)
        return {"migrated": [], "skipped": skipped, "status": "write_failed"}
    return {"migrated": migrated, "skipped": skipped, "status": "completed"}


class ProjectMcpRegistryPool:
    """按规范化项目路径缓存注册表，并用命名空间隔离跨项目工具。"""

    def __init__(
        self,
        tools: ToolRegistry,
        *,
        secret_store: Any | None = None,
        client_factory: McpClientFactory = McpClient,
    ) -> None:
        self._tools = tools
        self._secret_store = secret_store
        self._client_factory = client_factory
        self._registries: dict[Path, McpServerRegistry] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def config_path(project_path: str | Path) -> Path:
        return Path(project_path).expanduser().resolve() / ".beanagent" / "mcp_servers.json"

    async def get(self, project_path: str | Path) -> McpServerRegistry:
        project = Path(project_path).expanduser().resolve()
        async with self._lock:
            existing = self._registries.get(project)
            if existing is not None:
                return existing
            project_key = hashlib.sha256(str(project).casefold().encode("utf-8")).hexdigest()[:10]
            registry = McpServerRegistry(
                self.config_path(project),
                self._tools,
                client_factory=self._client_factory,
                default_scope="workspace",
                secret_store=self._secret_store,
                tool_namespace=f"project_{project_key}",
                source_id=f"project:{project_key}",
            )
            await registry.load_and_connect_all()
            self._registries[project] = registry
            return registry

    async def tool_names(self, project_path: str | Path) -> set[str]:
        registry = await self.get(project_path)
        return {
            str(tool_name)
            for record in registry.list_server_records(scope="workspace")
            for tool_name in record.get("tools", [])
        }

    async def discard(self, project_path: str | Path) -> None:
        project = Path(project_path).expanduser().resolve()
        async with self._lock:
            registry = self._registries.pop(project, None)
        if registry is not None:
            await registry.shutdown()

    async def shutdown(self) -> None:
        async with self._lock:
            registries = list(self._registries.values())
            self._registries.clear()
        await asyncio.gather(*(registry.shutdown() for registry in registries), return_exceptions=True)


__all__ = ["ProjectMcpRegistryPool", "migrate_legacy_workspace_mcp"]
