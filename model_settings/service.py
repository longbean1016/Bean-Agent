"""模型设置应用服务，集中编排领域接口。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from model_settings.catalog import ModelCatalogService
from model_settings.discovery import OpenAIModelDiscovery
from model_settings.models import ADAPTER_IDS, REASONING_EFFORTS, ModelConnection, ModelProfile, ModelRoute
from model_settings.secrets import SecretStore, SecretStoreError
from model_settings.store import ModelSettingsStore


class ModelSettingsValidationError(ValueError):
    pass


class ModelSettingsNotFound(LookupError):
    pass


class ModelSettingsService:
    def __init__(
        self,
        store: ModelSettingsStore,
        secrets: SecretStore,
        discovery: OpenAIModelDiscovery,
        catalog: ModelCatalogService,
    ) -> None:
        self.store = store
        self._secrets = secrets
        self._discovery = discovery
        self._catalog = catalog

    def list_connections(self) -> list[dict[str, Any]]:
        result = []
        for connection in self.store.list_connections():
            try:
                has_key = bool(self._secrets.get(connection.secret_ref))
            except SecretStoreError:
                has_key = False
            result.append({
                **connection.public_dict(has_api_key=has_key),
                "models": [item.public_dict() for item in self.store.list_models(connection.id)],
            })
        return result

    def create_connection(self, values: dict[str, Any]) -> dict[str, Any]:
        api_key = _required_text(values.get("api_key"), "API Key", 4096)
        connection_id = uuid4().hex
        secret_ref = f"connection:{connection_id}"
        connection = ModelConnection(
            id=connection_id,
            name=_required_text(values.get("name"), "连接名称", 80),
            provider=_optional_text(values.get("provider"), 80),
            base_url=normalize_base_url(values.get("base_url")),
            secret_ref=secret_ref,
            enabled=bool(values.get("enabled", True)),
            default_adapter=_adapter(values.get("default_adapter")),
        )
        self._secrets.set(secret_ref, api_key)
        try:
            saved = self.store.save_connection(connection)
        except Exception:
            self._secrets.delete(secret_ref)
            raise
        return saved.public_dict(has_api_key=True)

    def update_connection(self, connection_id: str, values: dict[str, Any]) -> dict[str, Any]:
        current = self._connection(connection_id)
        api_key = str(values.get("api_key") or "").strip()
        updated = replace(
            current,
            name=_required_text(values.get("name", current.name), "连接名称", 80),
            provider=_optional_text(values.get("provider", current.provider), 80),
            base_url=normalize_base_url(values.get("base_url", current.base_url)),
            enabled=bool(values.get("enabled", current.enabled)),
            default_adapter=_adapter(values.get("default_adapter", current.default_adapter)),
        )
        if api_key:
            self._secrets.set(current.secret_ref, api_key)
        saved = self.store.save_connection(updated)
        return saved.public_dict(has_api_key=bool(self._secrets.get(saved.secret_ref)))

    def delete_connection(self, connection_id: str) -> None:
        current = self._connection(connection_id)
        if not self.store.delete_connection(connection_id):
            raise ModelSettingsNotFound("连接不存在")
        self._secrets.delete(current.secret_ref)

    async def discover_models(self, connection_id: str) -> list[dict[str, Any]]:
        connection = self._connection(connection_id)
        api_key = self._secrets.get(connection.secret_ref) or ""
        if not api_key:
            raise ModelSettingsValidationError("连接尚未配置 API Key")
        discovered = await self._discovery.list_models(connection, api_key)
        profiles = [
            self._catalog.enrich(
                ModelProfile(connection.id, item.id, item.name),
                provider=connection.provider,
                default_adapter=connection.default_adapter,
            )
            for item in discovered
        ]
        return [item.public_dict() for item in self.store.replace_discovered_models(connection.id, profiles)]

    async def test_connection(self, connection_id: str) -> dict[str, Any]:
        models = await self.discover_models(connection_id)
        return {"ok": True, "model_count": sum(1 for item in models if item["available"])}

    def save_manual_model(self, connection_id: str, values: dict[str, Any]) -> dict[str, Any]:
        connection = self._connection(connection_id)
        model_id = _required_text(values.get("model_id"), "模型 ID", 300)
        profile = ModelProfile(
            connection_id=connection.id,
            model_id=model_id,
            display_name=_optional_text(values.get("display_name"), 300) or model_id,
        )
        enriched = self._catalog.enrich(
            profile, provider=connection.provider, default_adapter=connection.default_adapter
        )
        overrides = _model_overrides(values)
        saved = self.store.save_model(enriched.with_overrides(overrides) if overrides else enriched)
        return saved.public_dict()

    def update_model(
        self, connection_id: str, model_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        current = self.store.get_model(connection_id, model_id)
        if current is None:
            raise ModelSettingsNotFound("模型不存在")
        overrides = {**current.user_overrides, **_model_overrides(values)}
        return self.store.save_model(current.with_overrides(overrides)).public_dict()

    def get_route(self, session_key: str | None = None) -> ModelRoute | None:
        if session_key:
            route = self.store.get_route(f"session:{session_key}")
            if route:
                return route
        return self.store.get_route("global")

    def set_route(self, route: ModelRoute, session_key: str | None = None) -> ModelRoute:
        connection = self._connection(route.connection_id)
        profile = self.store.get_model(route.connection_id, route.model_id)
        if not connection.enabled or profile is None or not profile.available:
            raise ModelSettingsValidationError("所选连接或模型当前不可用")
        if route.reasoning_effort:
            if not profile.supports_reasoning:
                raise ModelSettingsValidationError("该模型不支持推理等级")
            if route.reasoning_effort not in profile.reasoning_options:
                raise ModelSettingsValidationError("推理等级不在模型支持范围内")
        self.store.set_route(f"session:{session_key}" if session_key else "global", route)
        return route

    async def update_catalog(self) -> dict[str, Any]:
        state = await self._catalog.update()
        for connection in self.store.list_connections():
            for profile in self.store.list_models(connection.id):
                enriched = self._catalog.enrich(
                    profile,
                    provider=connection.provider,
                    default_adapter=connection.default_adapter,
                )
                if profile.user_overrides:
                    enriched = enriched.with_overrides(profile.user_overrides)
                self.store.save_model(enriched)
        self.store.set_catalog_state(updated_at=str(state["updated_at"]))
        return state

    def _connection(self, connection_id: str) -> ModelConnection:
        connection = self.store.get_connection(connection_id)
        if connection is None:
            raise ModelSettingsNotFound("连接不存在")
        return connection


def normalize_base_url(value: Any) -> str:
    text = _required_text(value, "Base URL", 2048)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ModelSettingsValidationError("Base URL 必须是有效的 HTTP/HTTPS 地址")
    if parsed.username or parsed.password:
        raise ModelSettingsValidationError("Base URL 不能包含用户名或密码")
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/responses", "/models"):
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _model_overrides(values: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("display_name", "context_window", "max_output_tokens", "supports_tools", "supports_vision", "supports_reasoning", "reasoning_options", "adapter"):
        if key not in values:
            continue
        value = values[key]
        if key in {"context_window", "max_output_tokens"}:
            value = None if value in {None, ""} else int(value)
            if value is not None and value <= 0:
                raise ModelSettingsValidationError(f"{key} 必须大于 0")
        elif key == "adapter":
            value = _adapter(value)
        elif key == "reasoning_options":
            if not isinstance(value, list) or any(str(item) not in REASONING_EFFORTS for item in value):
                raise ModelSettingsValidationError("推理选项无效")
            value = list(dict.fromkeys(str(item) for item in value))
        elif key == "display_name":
            value = _required_text(value, "显示名称", 300)
        elif value is not None:
            value = bool(value)
        result[key] = value
    return result


def _adapter(value: Any) -> str:
    adapter = str(value or "generic_openai").strip()
    if adapter not in ADAPTER_IDS:
        raise ModelSettingsValidationError("适配器无效")
    return adapter


def _required_text(value: Any, label: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ModelSettingsValidationError(f"{label}不能为空")
    if len(text) > maximum:
        raise ModelSettingsValidationError(f"{label}过长")
    return text


def _optional_text(value: Any, maximum: int) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise ModelSettingsValidationError("字段过长")
    return text


__all__ = [
    "ModelSettingsNotFound",
    "ModelSettingsService",
    "ModelSettingsValidationError",
    "normalize_base_url",
]
