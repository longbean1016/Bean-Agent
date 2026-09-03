"""OpenAI-compatible ``/models`` 模型发现。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from model_settings.models import ModelConnection


class ModelDiscoveryError(RuntimeError):
    code = "discovery_failed"


class ModelAuthenticationError(ModelDiscoveryError):
    code = "authentication_failed"


class ModelDiscoveryUnsupported(ModelDiscoveryError):
    code = "discovery_unsupported"


class ModelDiscoveryTimeout(ModelDiscoveryError):
    code = "discovery_timeout"


@dataclass(frozen=True, slots=True)
class DiscoveredModel:
    id: str
    name: str


class OpenAIModelDiscovery:
    """只负责远端发现；不写数据库、不读取 SecretStore。"""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        timeout_seconds: float = 12.0,
        max_response_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self._client = client
        self._timeout = max(1.0, timeout_seconds)
        self._max_response_bytes = max_response_bytes

    async def list_models(
        self, connection: ModelConnection, api_key: str
    ) -> list[DiscoveredModel]:
        client = self._client or httpx.AsyncClient()
        owns_client = self._client is None
        try:
            response = await client.get(
                f"{connection.base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as error:
            raise ModelDiscoveryTimeout("获取模型超时") from error
        except httpx.HTTPError as error:
            raise ModelDiscoveryError("无法连接模型服务") from error
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code in {401, 403}:
            raise ModelAuthenticationError("API Key 无效或没有模型列表权限")
        if response.status_code in {404, 405, 501}:
            raise ModelDiscoveryUnsupported("该连接不支持自动获取模型")
        if not response.is_success:
            raise ModelDiscoveryError(f"模型服务返回 HTTP {response.status_code}")
        if len(response.content) > self._max_response_bytes:
            raise ModelDiscoveryError("模型列表响应过大")
        try:
            payload = response.json()
        except ValueError as error:
            raise ModelDiscoveryUnsupported("模型列表不是有效 JSON") from error
        return _parse_models(payload)


def _parse_models(payload: Any) -> list[DiscoveredModel]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ModelDiscoveryUnsupported("模型列表缺少 data 数组")
    found: dict[str, DiscoveredModel] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or item.get("name") or "").strip()
        if not model_id or len(model_id) > 300:
            continue
        display_name = str(item.get("name") or model_id).strip()[:300] or model_id
        found.setdefault(model_id, DiscoveredModel(model_id, display_name))
    return sorted(found.values(), key=lambda item: (item.name.casefold(), item.id))


__all__ = [
    "DiscoveredModel",
    "ModelAuthenticationError",
    "ModelDiscoveryError",
    "ModelDiscoveryTimeout",
    "ModelDiscoveryUnsupported",
    "OpenAIModelDiscovery",
]
