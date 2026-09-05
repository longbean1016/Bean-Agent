from types import SimpleNamespace

import pytest

from agent.config_models import LLMConfig
from agent.provider import LLMProvider
from model_settings.adapters import AdapterRegistry


class Completions:
    def __init__(self):
        self.request = None

    async def create(self, **kwargs):
        self.request = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[]))],
            usage=None,
        )


@pytest.mark.asyncio
async def test_openai_reasoning_adapter_maps_effort_and_output_limit() -> None:
    provider = LLMProvider(
        LLMConfig(model="o-model", api_key="key", max_tokens=900, extra_body={"reasoning_effort": "high"}),
        strategy=AdapterRegistry().create("openai_reasoning"),
    )
    completions = Completions()
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    await provider.chat([{"role": "user", "content": "hello"}])

    assert completions.request["reasoning_effort"] == "high"
    assert completions.request["max_completion_tokens"] == 900
    assert "max_tokens" not in completions.request
    assert "extra_body" not in completions.request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter", "effort", "expected_extra_body", "expected_effort"),
    [
        ("deepseek", "none", {"thinking": {"type": "disabled"}}, None),
        ("deepseek", "enabled", {"thinking": {"type": "enabled"}}, None),
        ("deepseek", "high", None, "high"),
        ("qwen_dashscope", "none", {"enable_thinking": False}, None),
        ("qwen_dashscope", "enabled", {"enable_thinking": True}, None),
        ("qwen_dashscope", "high", {"enable_thinking": True}, "high"),
    ],
)
async def test_adapter_maps_unified_reasoning_effort(
    adapter: str,
    effort: str,
    expected_extra_body: dict[str, object] | None,
    expected_effort: str | None,
) -> None:
    provider = LLMProvider(
        LLMConfig(model="reasoner", api_key="key", extra_body={"reasoning_effort": effort}),
        strategy=AdapterRegistry().create(adapter),
    )
    completions = Completions()
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    await provider.chat([{"role": "user", "content": "hello"}])

    if expected_extra_body is None:
        assert "extra_body" not in completions.request
    else:
        assert completions.request["extra_body"] == expected_extra_body
    if expected_effort is None:
        assert "reasoning_effort" not in completions.request
    else:
        assert completions.request["reasoning_effort"] == expected_effort


@pytest.mark.asyncio
async def test_openai_toggle_uses_provider_default_reasoning_effort() -> None:
    provider = LLMProvider(
        LLMConfig(model="o-model", api_key="key", extra_body={"reasoning_effort": "enabled"}),
        strategy=AdapterRegistry().create("openai_reasoning"),
    )
    completions = Completions()
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    await provider.chat([{"role": "user", "content": "hello"}])

    assert "reasoning_effort" not in completions.request
    assert "extra_body" not in completions.request


def test_registry_exposes_only_supported_explicit_adapters() -> None:
    assert AdapterRegistry().ids() == (
        "generic_openai", "deepseek", "qwen_dashscope", "openai_reasoning"
    )
