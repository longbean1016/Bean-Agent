"""模型上下文能力解析边界测试。"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from agent.model_capabilities import resolve_context_window
import agent.model_capabilities as capabilities_module


def _set_registry(monkeypatch, entries: dict[str, dict[str, object]]) -> None:
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(model_cost=entries))
    capabilities_module._model_registry.cache_clear()


def test_explicit_context_window_has_priority_over_snapshot(monkeypatch) -> None:
    _set_registry(monkeypatch, {"openai/example": {"max_input_tokens": 1000, "max_output_tokens": 200}})

    result = resolve_context_window(
        provider="openai",
        model="example",
        configured=4096,
    )

    assert result.context_window == 4096
    assert result.source == "explicit"


def test_snapshot_combines_input_and_output_capabilities(monkeypatch) -> None:
    _set_registry(monkeypatch, {"dashscope/qwen-plus": {"max_input_tokens": 128000, "max_output_tokens": 8192}})

    result = resolve_context_window(provider="qwen", model="qwen-plus")

    assert result.context_window == 136192
    assert result.source == "litellm"


def test_unknown_model_keeps_local_gate_disabled(monkeypatch) -> None:
    _set_registry(monkeypatch, {})

    result = resolve_context_window(provider="custom", model="private-model")

    assert result.context_window == 0
    assert result.source == "unknown"
