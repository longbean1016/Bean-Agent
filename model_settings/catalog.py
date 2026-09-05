"""公共模型目录缓存与精确能力匹配。"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from model_settings.models import REASONING_EFFORT_ORDER, REASONING_EFFORTS, ModelProfile, utc_now


CATALOG_URL = "https://models.dev/catalog.json"


class CatalogUpdateError(RuntimeError):
    pass


class ModelCatalogService:
    """只输出 Bean 领域对象，不把公共目录原始结构泄漏给上层。"""

    def __init__(
        self,
        cache_path: str | Path,
        client: httpx.AsyncClient | None = None,
        *,
        max_response_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.cache_path = Path(cache_path).expanduser().resolve()
        self._client = client
        self._max_response_bytes = max_response_bytes
        self._catalog: dict[str, Any] | None = None

    def enrich(
        self,
        profile: ModelProfile,
        *,
        provider: str,
        default_adapter: str,
    ) -> ModelProfile:
        match = _find_model(self._load_cache(), provider.strip().lower(), profile.model_id)
        if match is None:
            return replace(profile, adapter=default_adapter, metadata_source="unknown")
        catalog_provider, model = match
        limit = model.get("limit") if isinstance(model.get("limit"), dict) else {}
        modalities = model.get("modalities") if isinstance(model.get("modalities"), dict) else {}
        input_modes = modalities.get("input") if isinstance(modalities.get("input"), list) else []
        reasoning_options = _reasoning_values(model.get("reasoning_options"))
        supports_reasoning = bool(model.get("reasoning"))
        adapter = _suggest_adapter(catalog_provider, profile.model_id, supports_reasoning, default_adapter)
        return replace(
            profile,
            display_name=str(model.get("name") or profile.display_name),
            context_window=_positive_int(limit.get("context")),
            max_output_tokens=_positive_int(limit.get("output")),
            supports_tools=_optional_bool(model.get("tool_call")),
            supports_vision=("image" in input_modes),
            supports_reasoning=supports_reasoning,
            reasoning_options=reasoning_options,
            adapter=adapter,
            metadata_source=f"models.dev:{catalog_provider}",
            metadata_updated_at=str(model.get("last_updated") or utc_now()),
        )

    def has_data(self) -> bool:
        """返回本地缓存是否已经包含可用于匹配的模型资料。"""

        payload = self._load_cache()
        return bool(_provider_catalog(payload) or _canonical_models(payload))

    async def update(self) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient()
        owns_client = self._client is None
        try:
            response = await client.get(CATALOG_URL, timeout=25.0)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise CatalogUpdateError("更新公共模型目录失败") from error
        finally:
            if owns_client:
                await client.aclose()
        if len(response.content) > self._max_response_bytes:
            raise CatalogUpdateError("公共模型目录响应过大")
        try:
            payload = response.json()
        except ValueError as error:
            raise CatalogUpdateError("公共模型目录不是有效 JSON") from error
        _validate_catalog(payload)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.cache_path)
        self._catalog = payload
        catalog = _provider_catalog(payload)
        providers = len(catalog)
        models = sum(
            len(item.get("models", {}))
            for item in catalog.values()
            if isinstance(item, dict) and isinstance(item.get("models"), dict)
        )
        return {"updated_at": utc_now(), "providers": providers, "models": models}

    def _load_cache(self) -> dict[str, Any]:
        if self._catalog is not None:
            return self._catalog
        if not self.cache_path.is_file():
            self._catalog = {}
            return self._catalog
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            _validate_catalog(payload)
        except (OSError, ValueError, CatalogUpdateError):
            payload = {}
        self._catalog = payload
        return payload


def _find_model(
    payload: dict[str, Any], provider: str, model_id: str
) -> tuple[str, dict[str, Any]] | None:
    catalog = _provider_catalog(payload)
    if provider:
        item = catalog.get(provider)
        models = item.get("models") if isinstance(item, dict) else None
        if isinstance(models, dict) and isinstance(models.get(model_id), dict):
            return provider, models[model_id]

    canonical = _find_canonical_model(_canonical_models(payload), model_id)
    if canonical is not None:
        canonical_provider, canonical_model = canonical
        # 统一索引适合匹配中转站别名，但可能省略推理档位等 provider 侧字段。
        # 命中后用同一厂商的原始模型记录补齐，仍保留统一索引中的基础能力。
        item = catalog.get(canonical_provider)
        provider_models = item.get("models") if isinstance(item, dict) else None
        canonical_id = str(canonical_model.get("id") or "")
        native_id = canonical_id.split("/", 1)[-1]
        provider_model = (
            provider_models.get(native_id)
            if isinstance(provider_models, dict)
            else None
        )
        if isinstance(provider_model, dict):
            return canonical_provider, {**canonical_model, **provider_model}
        return canonical

    exact: list[tuple[str, dict[str, Any]]] = []
    for provider_id, item in catalog.items():
        models = item.get("models") if isinstance(item, dict) else None
        if not isinstance(models, dict):
            continue
        model = models.get(model_id)
        if isinstance(model, dict):
            exact.append((str(provider_id), model))
    # 厂商无关匹配只接受唯一结果，避免相同 ID 在不同网关能力不一致时误配。
    return exact[0] if len(exact) == 1 else None


def _find_canonical_model(
    models: dict[str, Any], model_id: str
) -> tuple[str, dict[str, Any]] | None:
    """按统一模型 ID 或完整 base model ID 匹配，不裁剪版本后缀。"""

    requested = model_id.casefold()
    exact = [
        (str(key), value)
        for key, value in models.items()
        if isinstance(value, dict)
        and (str(key).casefold() == requested or str(value.get("id") or "").casefold() == requested)
    ]
    if len(exact) == 1:
        key, model = exact[0]
        return key.split("/", 1)[0], model

    base_model_id = requested.rsplit("/", 1)[-1]
    base_matches = [
        (str(key), value)
        for key, value in models.items()
        if isinstance(value, dict) and str(key).casefold().rsplit("/", 1)[-1] == base_model_id
    ]
    if len(base_matches) != 1:
        return None
    key, model = base_matches[0]
    return key.split("/", 1)[0], model


def _reasoning_values(value: Any) -> tuple[str, ...]:
    options = value if isinstance(value, list) else [value]
    if not all(isinstance(option, dict) for option in options):
        return ()
    values: set[str] = set()
    has_toggle = False
    for option in options:
        has_toggle = has_toggle or option.get("type") == "toggle"
        raw_values = option.get("values")
        if isinstance(raw_values, list):
            values.update(
                str(item) for item in raw_values if str(item) in REASONING_EFFORTS
            )
    if has_toggle:
        # 有明确 effort 时开启由各档位表达；只有 toggle 时才提供独立“开启”。
        values.add("none")
        if not values.difference({"none"}):
            values.add("enabled")
    return tuple(item for item in REASONING_EFFORT_ORDER if item in values)


def _suggest_adapter(
    provider: str, model_id: str, reasoning: bool, fallback: str
) -> str:
    text = f"{provider} {model_id}".lower()
    if "deepseek" in text:
        return "deepseek"
    if "qwen" in text or "dashscope" in text or "alibaba" in text:
        return "qwen_dashscope"
    if provider == "openai" and reasoning:
        return "openai_reasoning"
    return fallback


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _provider_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    """兼容公共目录的新包装结构和历史缓存的扁平结构。"""

    providers = payload.get("providers")
    return providers if isinstance(providers, dict) else payload


def _canonical_models(payload: dict[str, Any]) -> dict[str, Any]:
    models = payload.get("models")
    return models if isinstance(models, dict) else {}


def _validate_catalog(payload: Any) -> None:
    if not isinstance(payload, dict) or not payload:
        raise CatalogUpdateError("公共模型目录根节点无效")
    catalog = _provider_catalog(payload)
    if not any(
        isinstance(item, dict) and isinstance(item.get("models"), dict)
        for item in catalog.values()
    ):
        raise CatalogUpdateError("公共模型目录不包含模型")


__all__ = ["CATALOG_URL", "CatalogUpdateError", "ModelCatalogService"]
