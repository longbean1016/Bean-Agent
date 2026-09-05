"""模型设置领域对象，不依赖 Web、数据库或厂商 SDK。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any


ADAPTER_IDS = frozenset({
    "generic_openai",
    "deepseek",
    "qwen_dashscope",
    "openai_reasoning",
})
REASONING_EFFORT_ORDER = (
    "none", "enabled", "minimal", "low", "medium", "high", "xhigh", "max",
)
REASONING_EFFORTS = frozenset(REASONING_EFFORT_ORDER)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ModelConnection:
    id: str
    name: str
    provider: str
    base_url: str
    secret_ref: str
    enabled: bool = True
    default_adapter: str = "generic_openai"
    revision: int = 1
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def public_dict(
        self, *, has_api_key: bool, api_key_preview: str | None = None
    ) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "base_url": self.base_url,
            "has_api_key": has_api_key,
            "api_key_preview": api_key_preview,
            "enabled": self.enabled,
            "default_adapter": self.default_adapter,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class ModelProfile:
    connection_id: str
    model_id: str
    display_name: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    supports_reasoning: bool | None = None
    reasoning_options: tuple[str, ...] = ()
    adapter: str = "generic_openai"
    metadata_source: str = "unknown"
    metadata_updated_at: str | None = None
    user_overrides: dict[str, Any] = field(default_factory=dict)
    available: bool = True
    revision: int = 1
    discovered_at: str = field(default_factory=utc_now)

    @property
    def route_key(self) -> str:
        return f"{self.connection_id}:{self.model_id}"

    def with_overrides(self, overrides: dict[str, Any]) -> ModelProfile:
        allowed = {
            "display_name", "context_window", "max_output_tokens",
            "supports_tools", "supports_vision", "supports_reasoning",
            "reasoning_options", "adapter",
        }
        values = {key: value for key, value in overrides.items() if key in allowed}
        if "reasoning_options" in values:
            values["reasoning_options"] = tuple(values["reasoning_options"] or ())
        return replace(self, **values, user_overrides=dict(overrides))

    def public_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "model_id": self.model_id,
            "display_name": self.display_name,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "supports_reasoning": self.supports_reasoning,
            "reasoning_options": list(self.reasoning_options),
            "adapter": self.adapter,
            "metadata_source": self.metadata_source,
            "metadata_updated_at": self.metadata_updated_at,
            "user_overrides": dict(self.user_overrides),
            "available": self.available,
            "revision": self.revision,
            "discovered_at": self.discovered_at,
        }


@dataclass(frozen=True, slots=True)
class ModelRoute:
    connection_id: str
    model_id: str
    reasoning_effort: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "model_id": self.model_id,
            "reasoning_effort": self.reasoning_effort,
        }


__all__ = [
    "ADAPTER_IDS",
    "REASONING_EFFORT_ORDER",
    "REASONING_EFFORTS",
    "ModelConnection",
    "ModelProfile",
    "ModelRoute",
    "utc_now",
]
