"""显式模型适配器注册表。"""

from __future__ import annotations

from collections.abc import Callable

from agent.provider import (
    DashScopeStrategy,
    DeepSeekStrategy,
    OpenAIReasoningStrategy,
    ProviderStrategy,
)


class AdapterRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], ProviderStrategy]] = {
            "generic_openai": ProviderStrategy,
            "deepseek": DeepSeekStrategy,
            "qwen_dashscope": DashScopeStrategy,
            "openai_reasoning": OpenAIReasoningStrategy,
        }

    def create(self, adapter_id: str) -> ProviderStrategy:
        try:
            return self._factories[adapter_id]()
        except KeyError as error:
            raise ValueError(f"未知模型适配器: {adapter_id}") from error

    def ids(self) -> tuple[str, ...]:
        return tuple(self._factories)


__all__ = ["AdapterRegistry"]
