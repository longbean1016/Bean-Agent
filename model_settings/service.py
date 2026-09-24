"""模型设置应用服务，集中编排领域接口。"""

from __future__ import annotations

import base64
from dataclasses import replace
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx

from model_settings.catalog import CatalogUpdateError, ModelCatalogService
from model_settings.discovery import OpenAIModelDiscovery
from model_settings.models import (
    ADAPTER_IDS,
    INDEPENDENT_MODEL_CAPABILITIES,
    MODEL_CAPABILITIES,
    REASONING_EFFORTS,
    DiscoveryRun,
    ModelConnection,
    ModelProfile,
    ModelRoute,
)
from model_settings.secrets import SecretStore, SecretStoreError
from model_settings.store import ModelSettingsConflict, ModelSettingsStore


class ModelSettingsValidationError(ValueError):
    pass


class ModelSettingsNotFound(LookupError):
    pass


class ModelCapabilityTestError(RuntimeError):
    """能力接口探测失败；错误码可安全返回设置页，不包含密钥。"""

    def __init__(self, message: str, *, code: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ModelSettingsService:
    def __init__(
        self,
        store: ModelSettingsStore,
        secrets: SecretStore,
        discovery: OpenAIModelDiscovery,
        catalog: ModelCatalogService,
        *,
        embedding_dimensions: int = 1024,
        capability_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.store = store
        self._secrets = secrets
        self._discovery = discovery
        self._catalog = catalog
        self._embedding_dimensions = _dimensions(embedding_dimensions)
        self._capability_client = capability_client

    def list_connections(self) -> list[dict[str, Any]]:
        result = []
        for connection in self.store.list_connections():
            try:
                api_key = self._secrets.get(connection.secret_ref)
            except SecretStoreError:
                api_key = None
            has_key = bool(api_key)
            result.append({
                **connection.public_dict(
                    has_api_key=has_key,
                    api_key_preview=_api_key_preview(api_key),
                ),
                "models": [item.public_dict() for item in self.store.list_models(connection.id)],
            })
        return result

    def get_connection_api_key(self, connection_id: str) -> str:
        connection = self._connection(connection_id)
        api_key = self._secrets.get(connection.secret_ref)
        if not api_key:
            raise ModelSettingsValidationError("连接尚未配置 API Key")
        return api_key

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
        return saved.public_dict(
            has_api_key=True, api_key_preview=_api_key_preview(api_key)
        )

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
        previous_key = self._secrets.get(current.secret_ref)
        if api_key:
            self._secrets.set(current.secret_ref, api_key)
        try:
            saved = self.store.save_connection(updated)
        except Exception:
            if api_key:
                if previous_key:
                    self._secrets.set(current.secret_ref, previous_key)
                else:
                    self._secrets.delete(current.secret_ref)
            raise
        saved_key = self._secrets.get(saved.secret_ref)
        if (saved.provider, saved.default_adapter) != (current.provider, current.default_adapter):
            self._refresh_connection_profiles(saved)
        return saved.public_dict(
            has_api_key=bool(saved_key), api_key_preview=_api_key_preview(saved_key)
        )

    def delete_connection(self, connection_id: str) -> None:
        current = self._connection(connection_id)
        if self.store.connection_is_referenced(connection_id):
            raise ModelSettingsConflict("连接仍被默认路由或会话引用")
        previous_key = self._secrets.get(current.secret_ref)
        self._secrets.delete(current.secret_ref)
        try:
            if not self.store.delete_connection(connection_id):
                raise ModelSettingsNotFound("连接不存在")
        except Exception:
            if previous_key:
                self._secrets.set(current.secret_ref, previous_key)
            raise

    async def discover_models(self, connection_id: str) -> list[dict[str, Any]]:
        connection = self._connection(connection_id)
        api_key = self._secrets.get(connection.secret_ref) or ""
        if not api_key:
            raise ModelSettingsValidationError("连接尚未配置 API Key")
        requested_url = f"{connection.base_url.rstrip('/')}/models"
        try:
            discovered = await self._discovery.list_models(connection, api_key)
        except Exception as error:
            self.store.record_discovery_run(DiscoveryRun(
                connection_id=connection.id,
                requested_url=requested_url,
                status="failed",
                error_message=str(error)[:500],
            ))
            raise
        profiles = [
            self._catalog.enrich(
                ModelProfile(connection.id, item.id, item.name),
                provider=connection.provider,
                default_adapter=connection.default_adapter,
            )
            for item in discovered
        ]
        saved = self.store.replace_discovered_models(connection.id, profiles)
        self.store.record_discovery_run(DiscoveryRun(
            connection_id=connection.id,
            requested_url=requested_url,
            status="success",
            model_count=len(discovered),
            response_hash=getattr(self._discovery, "last_response_hash", None),
        ))
        return [item.public_dict() for item in saved]

    async def refresh_models(self, connection_id: str) -> dict[str, Any]:
        catalog_warning = ""
        if not self._catalog.has_data():
            try:
                state = await self._catalog.update()
                self.store.set_catalog_state(updated_at=str(state["updated_at"]))
            except CatalogUpdateError as error:
                # 公共目录不可用不应阻断私有网关发现，未知容量仍可手工覆盖。
                catalog_warning = str(error)
        return {
            "items": await self.discover_models(connection_id),
            "catalog_warning": catalog_warning or None,
        }

    async def test_model_list(self, connection_id: str) -> dict[str, Any]:
        connection = self._connection(connection_id)
        api_key = self._api_key(connection)
        requested_url = f"{connection.base_url.rstrip('/')}/models"
        try:
            models = await self._discovery.list_models(connection, api_key)
        except Exception as error:
            self.store.record_discovery_run(DiscoveryRun(
                connection_id=connection.id,
                requested_url=requested_url,
                status="failed",
                error_message=str(error)[:500],
            ))
            raise
        self.store.record_discovery_run(DiscoveryRun(
            connection_id=connection.id,
            requested_url=requested_url,
            status="tested",
            model_count=len(models),
            response_hash=getattr(self._discovery, "last_response_hash", None),
        ))
        return {
            "ok": True,
            "connection_id": connection.id,
            "connection_name": connection.name,
            "model_count": len(models),
        }

    def discovery_runs(self, connection_id: str) -> list[dict[str, Any]]:
        self._connection(connection_id)
        return [item.public_dict() for item in self.store.list_discovery_runs(connection_id)]

    def capability_probes(
        self, connection_id: str, model_id: str | None = None
    ) -> list[dict[str, Any]]:
        self._connection(connection_id)
        return [
            item.public_dict()
            for item in self.store.list_capability_probes(connection_id, model_id)
        ]

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

    def capability_settings(self, session_key: str | None = None) -> dict[str, dict[str, Any]]:
        """返回三个能力的路由状态，未配置独立路由时显式标记为跟随主模型。"""

        return {
            capability: self.capability_state(capability, session_key=session_key)
            for capability in MODEL_CAPABILITIES
        }

    def capability_state(
        self, capability: str, *, session_key: str | None = None
    ) -> dict[str, Any]:
        normalized = _capability(capability)
        if normalized == "primary":
            # 主模型本身就是基准路由，不把它重复标记成 override。
            override = None
            effective = self.get_route(session_key)
            mode = "primary"
            follows_primary = False
            effective_source = "primary"
        else:
            override = self.get_capability_route(
                normalized, session_key=session_key, resolve_fallback=False
            )
            effective = override or self.get_route(session_key)
            if override is None:
                mode = "follow"
                follows_primary = True
                effective_source = "primary"
            else:
                mode = "independent"
                follows_primary = False
                effective_source = "independent"
        state = {
            "capability": normalized,
            "mode": mode,
            "follows_primary": follows_primary,
            "effective_source": effective_source,
            "route": effective.public_dict() if effective else None,
            "override_route": override.public_dict() if override else None,
        }
        if normalized == "embedding":
            # 记忆库向量列维度固定；设置页只展示探测基准，不允许在此处静默改维度。
            state["expected_dimension"] = self._embedding_dimensions
        return state

    def get_capability_route(
        self,
        capability: str,
        *,
        session_key: str | None = None,
        resolve_fallback: bool = True,
    ) -> ModelRoute | None:
        """读取能力路由；默认将无独立覆盖的能力回退到主模型。"""

        normalized = _capability(capability)
        if normalized == "primary":
            return self.get_route(session_key)
        scopes = _capability_scopes(normalized, session_key)
        for scope in scopes:
            route = self.store.get_route(scope)
            if route:
                return route
        return self.get_route(session_key) if resolve_fallback else None

    async def test_capability(
        self,
        capability: str,
        *,
        route: ModelRoute | None = None,
        dimensions: int | None = None,
    ) -> dict[str, Any]:
        """调用能力专用接口，验证连接与所选模型，而不修改已保存路由。"""

        normalized = _capability(capability)
        if normalized == "primary":
            raise ModelCapabilityTestError(
                "主模型请使用模型测试接口", code="capability_not_supported", status_code=400
            )
        selected = route or self.get_capability_route(normalized)
        if selected is None:
            raise ModelCapabilityTestError(
                "尚未配置该能力的模型路由", code="capability_route_missing", status_code=400
            )
        self._validate_route(selected)
        connection = self._connection(selected.connection_id)
        api_key = self._api_key(connection)
        profile = self.store.get_model(selected.connection_id, selected.model_id)
        started = perf_counter()
        client = self._capability_client or httpx.AsyncClient()
        owns_client = self._capability_client is None
        try:
            if normalized == "embedding":
                expected = self._embedding_dimensions if dimensions is None else _dimensions(dimensions)
                result = await self._test_embedding(
                    client, connection, selected.model_id, api_key, expected
                )
            else:
                result = await self._test_vision(
                    client, connection, selected.model_id, api_key
                )
        finally:
            if owns_client:
                await client.aclose()
        result.update({
            "ok": True,
            "capability": normalized,
            "connection_id": connection.id,
            "connection_name": connection.name,
            "model_id": selected.model_id,
            "model_display_name": profile.display_name if profile else selected.model_id,
            "duration_ms": max(0, round((perf_counter() - started) * 1000)),
        })
        return result

    async def _test_embedding(
        self,
        client: httpx.AsyncClient,
        connection: ModelConnection,
        model_id: str,
        api_key: str,
        expected_dimensions: int,
    ) -> dict[str, Any]:
        try:
            response = await client.post(
                f"{connection.base_url.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model_id,
                    "input": ["BeanAgent capability probe"],
                    "dimensions": expected_dimensions,
                },
                timeout=30.0,
            )
        except httpx.TimeoutException as error:
            raise ModelCapabilityTestError("Embedding 调用超时", code="embedding_timeout", status_code=504) from error
        except httpx.HTTPError as error:
            raise ModelCapabilityTestError("无法连接 Embedding 服务", code="embedding_connection_failed") from error
        self._raise_capability_response_error(response, "Embedding")
        try:
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            vector = data[0].get("embedding") if isinstance(data, list) and data else None
            actual = len(vector) if isinstance(vector, list) else 0
        except (ValueError, TypeError, IndexError, AttributeError) as error:
            raise ModelCapabilityTestError("Embedding 响应格式无效", code="embedding_response_invalid") from error
        if actual != expected_dimensions:
            raise ModelCapabilityTestError(
                f"Embedding 向量维度不一致：期望 {expected_dimensions}，实际 {actual}",
                code="embedding_dimension_mismatch",
                status_code=422,
            )
        return {"dimensions": actual, "expected_dimension": expected_dimensions}

    async def _test_vision(
        self,
        client: httpx.AsyncClient,
        connection: ModelConnection,
        model_id: str,
        api_key: str,
    ) -> dict[str, Any]:
        # 1x1 PNG 只用于能力探测，不落盘也不进入会话历史。
        image_data = base64.b64encode(
            bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360606000000004000000ffff03000006000557bfadba0000000049454e44ae426082")
        ).decode("ascii")
        try:
            response = await client.post(
                f"{connection.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model_id,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Reply OK after receiving this image."},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}},
                        ],
                    }],
                    "max_tokens": 8,
                },
                timeout=30.0,
            )
        except httpx.TimeoutException as error:
            raise ModelCapabilityTestError("视觉模型调用超时", code="vision_timeout", status_code=504) from error
        except httpx.HTTPError as error:
            raise ModelCapabilityTestError("无法连接视觉模型服务", code="vision_connection_failed") from error
        self._raise_capability_response_error(response, "视觉模型")
        try:
            payload = response.json()
            choices = payload.get("choices") if isinstance(payload, dict) else None
            received = isinstance(choices, list) and bool(choices)
        except (ValueError, TypeError, AttributeError) as error:
            raise ModelCapabilityTestError("视觉模型响应格式无效", code="vision_response_invalid") from error
        if not received:
            raise ModelCapabilityTestError("视觉模型未返回有效响应", code="vision_response_invalid")
        return {"vision_received": True}

    @staticmethod
    def _raise_capability_response_error(response: httpx.Response, label: str) -> None:
        if response.is_success:
            return
        if response.status_code in {401, 403}:
            raise ModelCapabilityTestError(
                f"{label}认证失败（HTTP {response.status_code}），请检查 API Key",
                code="capability_authentication_failed", status_code=401,
            )
        if response.status_code == 404:
            raise ModelCapabilityTestError(
                f"{label}接口不存在（HTTP 404）", code="capability_not_found", status_code=404
            )
        if response.status_code in {400, 422}:
            raise ModelCapabilityTestError(
                f"{label}请求协议或参数不兼容（HTTP {response.status_code}）",
                code="capability_request_invalid",
                status_code=422,
            )
        raise ModelCapabilityTestError(
            f"{label}调用失败（HTTP {response.status_code}）",
            code="capability_invocation_failed", status_code=502,
        )

    def set_capability_route(
        self,
        capability: str,
        route: ModelRoute | None = None,
        *,
        session_key: str | None = None,
        follow_primary: bool = False,
    ) -> ModelRoute | None:
        """保存独立能力路由，或删除覆盖使其重新跟随主模型。"""

        normalized = _capability(capability)
        if normalized == "primary":
            if route is None:
                raise ModelSettingsValidationError("主模型路由不能为空")
            return self.set_route(route, session_key=session_key)
        scope = _capability_scope(normalized, session_key)
        if follow_primary:
            self.store.delete_route(scope)
            return self.get_capability_route(normalized, session_key=session_key)
        if route is None:
            raise ModelSettingsValidationError("独立能力路由不能为空")
        self._validate_route(route)
        self.store.set_route(scope, route)
        return route

    def set_route(self, route: ModelRoute, session_key: str | None = None) -> ModelRoute:
        self._validate_route(route)
        self.store.set_route(f"session:{session_key}" if session_key else "global", route)
        return route

    def _validate_route(self, route: ModelRoute) -> None:
        connection = self._connection(route.connection_id)
        profile = self.store.get_model(route.connection_id, route.model_id)
        if not connection.enabled:
            raise ModelSettingsValidationError("连接已停用，请先启用并保存连接")
        if profile is None or not profile.available:
            raise ModelSettingsValidationError("所选模型当前不可用，请重新获取或选择模型")
        if route.reasoning_effort:
            if not profile.supports_reasoning:
                raise ModelSettingsValidationError("该模型不支持推理等级")
            if route.reasoning_effort not in profile.reasoning_options:
                raise ModelSettingsValidationError("推理等级不在模型支持范围内")

    async def update_catalog(self) -> dict[str, Any]:
        state = await self._catalog.update()
        self.refresh_cached_profiles()
        self.store.set_catalog_state(updated_at=str(state["updated_at"]))
        return state

    def refresh_cached_profiles(self) -> None:
        """启动及目录更新时补全已有资料；不联网，不要求重新发现模型。"""

        if not self._catalog.has_data():
            return
        for connection in self.store.list_connections():
            self._refresh_connection_profiles(connection)

    def _refresh_connection_profiles(self, connection: ModelConnection) -> None:
        for profile in self.store.list_models(connection.id):
            enriched = self._catalog.enrich(
                profile, provider=connection.provider, default_adapter=connection.default_adapter,
            )
            if profile.user_overrides:
                enriched = enriched.with_overrides(profile.user_overrides)
            # 无实际能力变化时不推进 revision，避免每次启动仅因时间戳淘汰客户端。
            if replace(enriched, metadata_updated_at=profile.metadata_updated_at) != profile:
                self.store.save_model(enriched)

    def _connection(self, connection_id: str) -> ModelConnection:
        connection = self.store.get_connection(connection_id)
        if connection is None:
            raise ModelSettingsNotFound("连接不存在")
        return connection

    def _api_key(self, connection: ModelConnection) -> str:
        api_key = self._secrets.get(connection.secret_ref) or ""
        if not api_key:
            raise ModelSettingsValidationError("连接尚未配置 API Key")
        return api_key


def normalize_base_url(value: Any) -> str:
    text = _required_text(value, "Base URL", 2048)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ModelSettingsValidationError("Base URL 必须是有效的 HTTP/HTTPS 地址")
    if parsed.username or parsed.password:
        raise ModelSettingsValidationError("Base URL 不能包含用户名或密码")
    path = parsed.path.rstrip("/")
    for suffix in (
        "/chat/completions", "/completions", "/responses", "/models", "/embeddings",
    ):
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


def _capability(value: Any) -> str:
    capability = str(value or "").strip().lower()
    if capability not in MODEL_CAPABILITIES:
        raise ModelSettingsValidationError("模型能力无效")
    return capability


def _capability_scope(capability: str, session_key: str | None) -> str:
    """把能力覆盖编码进现有 scope 字符串，避免新增关系型字段。"""

    prefix = f"session:{session_key}:" if session_key else "global:"
    return f"{prefix}{capability}"


def _capability_scopes(capability: str, session_key: str | None) -> tuple[str, ...]:
    if session_key:
        return (_capability_scope(capability, session_key), _capability_scope(capability, None))
    return (_capability_scope(capability, None),)


def _required_text(value: Any, label: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ModelSettingsValidationError(f"{label}不能为空")
    if len(text) > maximum:
        raise ModelSettingsValidationError(f"{label}过长")
    return text


def _api_key_preview(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        visible = max(1, len(value) // 4)
        return f"{value[:visible]}...{value[-visible:]}"
    return f"{value[:4]}...{value[-4:]}"


def _optional_text(value: Any, maximum: int) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise ModelSettingsValidationError("字段过长")
    return text


def _dimensions(value: Any) -> int:
    try:
        dimensions = int(value)
    except (TypeError, ValueError) as error:
        raise ModelSettingsValidationError("Embedding 维度必须是正整数") from error
    if dimensions <= 0 or dimensions > 65536:
        raise ModelSettingsValidationError("Embedding 维度必须在 1 到 65536 之间")
    return dimensions


__all__ = [
    "INDEPENDENT_MODEL_CAPABILITIES",
    "MODEL_CAPABILITIES",
    "ModelCapabilityTestError",
    "ModelSettingsNotFound",
    "ModelSettingsService",
    "ModelSettingsValidationError",
    "normalize_base_url",
]
