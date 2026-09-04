"""按冻结模型路由管理 Provider 生命周期。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Any

from agent.config_models import LLMConfig
from agent.provider import LLMProvider
from model_settings.adapters import AdapterRegistry
from model_settings.models import ModelConnection, ModelProfile, ModelRoute
from model_settings.secrets import SecretStore
from model_settings.service import ModelSettingsService, ModelSettingsValidationError
from model_settings.store import ModelSettingsStore


@dataclass(frozen=True, slots=True)
class FrozenModelRoute:
    connection_id: str
    connection_revision: int
    model_id: str
    model_revision: int
    adapter: str
    reasoning_effort: str | None
    runtime_id: str
    cache_key: str
    connection_name: str = ""
    model_display_name: str = ""

    def metadata(self) -> dict[str, Any]:
        """只返回可进入消息 metadata 的公开路由事实。"""

        metadata = {
            "connection_id": self.connection_id,
            "connection_revision": self.connection_revision,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "adapter": self.adapter,
            "reasoning_effort": self.reasoning_effort,
            "model_runtime_id": self.runtime_id,
            "lease_key": self.cache_key,
        }
        if self.connection_name:
            metadata["connection_name"] = self.connection_name
        if self.model_display_name:
            metadata["model_display_name"] = self.model_display_name
        return metadata


@dataclass(frozen=True, slots=True)
class ProviderLease:
    provider: Any
    route: FrozenModelRoute


class ModelInvocationTestError(RuntimeError):
    def __init__(self, message: str, *, code: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ProviderManager:
    """Provider 客户端的唯一所有者；密钥不离开本模块。"""

    def __init__(
        self,
        store: ModelSettingsStore,
        secrets: SecretStore,
        adapters: AdapterRegistry,
        legacy_config: LLMConfig,
    ) -> None:
        self._store = store
        self._secrets = secrets
        self._adapters = adapters
        self._legacy_config = legacy_config
        self._providers: dict[str, Any] = {}
        self._routes: dict[str, FrozenModelRoute] = {}
        self._active: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    def freeze(
        self,
        settings: ModelSettingsService,
        *,
        session_key: str,
        requested: ModelRoute | None,
    ) -> FrozenModelRoute:
        route = requested or settings.get_route(session_key)
        if route is None:
            raise ModelSettingsValidationError("尚未配置默认模型")
        # 冻结前统一走 Service 校验，但消息发送不改变已保存的默认路由。
        connection, profile = self._validated(route)
        key = ":".join((
            connection.id, str(connection.revision), profile.model_id,
            str(profile.revision), route.reasoning_effort or "",
        ))
        runtime_id = f"{connection.id}:{connection.revision}:{profile.model_id}:{profile.revision}"
        frozen = FrozenModelRoute(
            connection_id=connection.id,
            connection_revision=connection.revision,
            model_id=profile.model_id,
            model_revision=profile.revision,
            adapter=profile.adapter,
            reasoning_effort=route.reasoning_effort,
            runtime_id=runtime_id,
            cache_key=key,
            connection_name=connection.name,
            model_display_name=profile.display_name,
        )
        if key not in self._providers:
            self._providers[key] = self._create_provider(connection, profile, route, runtime_id)
        self._routes[key] = frozen
        return frozen

    async def acquire(self, metadata: dict[str, Any]) -> ProviderLease:
        key = str(metadata.get("lease_key") or "")
        async with self._lock:
            if self._closed:
                raise RuntimeError("ProviderManager 已关闭")
            provider = self._providers.get(key)
            route = self._routes.get(key)
            if provider is None or route is None:
                raise ModelSettingsValidationError("模型路由快照已失效")
            self._active[key] = self._active.get(key, 0) + 1
        return ProviderLease(provider, route)

    async def release(self, lease: ProviderLease) -> None:
        key = lease.route.cache_key
        async with self._lock:
            count = self._active.get(key, 0)
            if count <= 1:
                self._active.pop(key, None)
            else:
                self._active[key] = count - 1

    async def test_model(
        self, settings: ModelSettingsService, route: ModelRoute
    ) -> dict[str, Any]:
        """使用实际 adapter 发起无工具的最小生成，验证模型级权限与协议。"""

        frozen = self.freeze(settings, session_key="settings:model-test", requested=route)
        lease = await self.acquire(frozen.metadata())
        started = perf_counter()
        try:
            await lease.provider.chat(
                [{"role": "user", "content": "Reply OK."}],
                tools=None,
                max_tokens=8,
                disable_thinking=True,
            )
        except Exception as error:
            raise _model_test_error(error, frozen) from error
        finally:
            await self.release(lease)
        return {
            "ok": True,
            "connection_id": frozen.connection_id,
            "connection_name": frozen.connection_name,
            "model_id": frozen.model_id,
            "model_display_name": frozen.model_display_name,
            "adapter": frozen.adapter,
            "duration_ms": max(0, round((perf_counter() - started) * 1000)),
        }

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            unique = []
            seen: set[int] = set()
            for provider in self._providers.values():
                if id(provider) not in seen:
                    seen.add(id(provider))
                    unique.append(provider)
            self._providers.clear()
            self._routes.clear()
        await asyncio.gather(*(provider.close() for provider in unique), return_exceptions=False)

    def _validated(self, route: ModelRoute) -> tuple[ModelConnection, ModelProfile]:
        connection = self._store.get_connection(route.connection_id)
        profile = self._store.get_model(route.connection_id, route.model_id)
        if connection is None or profile is None or not connection.enabled or not profile.available:
            raise ModelSettingsValidationError("所选连接或模型当前不可用")
        if route.reasoning_effort and (
            not profile.supports_reasoning
            or route.reasoning_effort not in profile.reasoning_options
        ):
            raise ModelSettingsValidationError("推理等级不在模型支持范围内")
        return connection, profile

    def _create_provider(
        self, connection: ModelConnection, profile: ModelProfile, route: ModelRoute, runtime_id: str
    ) -> LLMProvider:
        api_key = self._secrets.get(connection.secret_ref) or ""
        if not api_key:
            raise ModelSettingsValidationError("连接尚未配置 API Key")
        extra_body = dict(self._legacy_config.extra_body)
        # 页面路由是主模型推理设置的唯一事实源，不能让旧 TOML 中的推理字段
        # 在用户选择“默认”时继续生效；其他兼容扩展参数仍可沿用。
        for key in ("enable_thinking", "thinking", "reasoning_effort"):
            extra_body.pop(key, None)
        if route.reasoning_effort:
            extra_body["reasoning_effort"] = route.reasoning_effort
        config = replace(
            self._legacy_config,
            provider=connection.provider,
            model=profile.model_id,
            api_key=api_key,
            base_url=connection.base_url,
            max_tokens=profile.max_output_tokens or self._legacy_config.max_tokens,
            context_window=profile.context_window or 0,
            context_window_source=profile.metadata_source,
            multimodal=bool(profile.supports_vision),
            extra_body=extra_body,
        )
        return LLMProvider(
            config,
            strategy=self._adapters.create(profile.adapter),
            runtime_id=runtime_id,
        )


def _model_test_error(error: Exception, route: FrozenModelRoute) -> ModelInvocationTestError:
    status = getattr(error, "status_code", None)
    label = f"连接“{route.connection_name}” / 模型“{route.model_display_name or route.model_id}”"
    if status == 401:
        return ModelInvocationTestError(f"{label}认证失败（HTTP 401），请检查 API Key", code="model_authentication_failed", status_code=401)
    if status == 403:
        return ModelInvocationTestError(f"{label}调用被拒绝（HTTP 403），请检查中转站分组、模型权限或渠道", code="model_forbidden", status_code=403)
    if status == 404:
        return ModelInvocationTestError(f"{label}不存在或调用路径未开放（HTTP 404）", code="model_not_found", status_code=404)
    if status in {400, 422}:
        return ModelInvocationTestError(f"{label}请求协议或参数不兼容（HTTP {status}）", code="model_request_invalid", status_code=422)
    if isinstance(status, int):
        return ModelInvocationTestError(f"{label}调用失败（HTTP {status}）", code="model_invocation_failed", status_code=502)
    return ModelInvocationTestError(f"{label}无法连接模型服务", code="model_connection_failed", status_code=502)


__all__ = ["FrozenModelRoute", "ModelInvocationTestError", "ProviderLease", "ProviderManager"]
