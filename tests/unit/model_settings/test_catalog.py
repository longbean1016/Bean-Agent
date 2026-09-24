import json
from pathlib import Path

import pytest

from model_settings.catalog import ModelCatalogService
from model_settings.models import ModelProfile


@pytest.mark.parametrize("provider", ["deepseek", " deep seek ", "DEEP SEEK"])
def test_known_provider_alias_wins_over_ambiguous_global_matches(tmp_path, provider):
    catalog = ModelCatalogService(tmp_path / "catalog.json")
    catalog._catalog = {"providers": {
        "deepseek": {"models": {"deepseek-flash": {"limit": {"context": 1_000_000, "output": 393_216}}}},
        "gateway": {"models": {"deepseek-flash": {"limit": {"context": 64_000}}}},
    }}
    profile = catalog.enrich(ModelProfile("one", "deepseek-flash", "Flash"),
                             provider=provider, default_adapter="deepseek")
    assert profile.context_window == 1_000_000
    assert profile.max_output_tokens == 393_216
    assert profile.model_id == "deepseek-flash"
    assert profile.metadata_source == "models.dev:deepseek"
    unknown = catalog.enrich(profile, provider="deep-seek-private", default_adapter="generic_openai")
    assert unknown.context_window is None
    assert unknown.max_output_tokens is None
    assert unknown.metadata_source == "unknown"


@pytest.mark.parametrize("invalid", [True, False, -10, 0, 1.5, "1.5", None, float("inf")])
def test_invalid_catalog_capacity_is_unknown(tmp_path, invalid):
    catalog = ModelCatalogService(tmp_path / "catalog.json")
    catalog._catalog = {"one": {"models": {"model": {"limit": {"context": invalid, "output": invalid}}}}}
    result = catalog.enrich(ModelProfile("one", "model", "Model"), provider="one", default_adapter="generic_openai")
    assert result.context_window is None
    assert result.max_output_tokens is None


def test_catalog_matches_provider_and_exact_model_id(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "openai": {"models": {"same": {
            "name": "OpenAI Same", "reasoning": True, "tool_call": True,
            "modalities": {"input": ["text", "image"]},
            "reasoning_options": [{"type": "effort", "values": ["low", "high"]}],
            "limit": {"context": 128000, "output": 16000},
        }}},
        "other": {"models": {"same": {"name": "Other Same", "limit": {"context": 8000}}}},
    }), encoding="utf-8")

    result = ModelCatalogService(path).enrich(
        ModelProfile("c", "same", "same"), provider="openai", default_adapter="generic_openai"
    )

    assert result.display_name == "OpenAI Same"
    assert result.context_window == 128000
    assert result.max_output_tokens == 16000
    assert result.supports_vision is True
    assert result.reasoning_options == ("low", "high")
    assert result.adapter == "openai_reasoning"


def test_catalog_does_not_guess_ambiguous_provider_independent_model(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "one": {"models": {"same": {"name": "One"}}},
        "two": {"models": {"same": {"name": "Two"}}},
    }), encoding="utf-8")

    result = ModelCatalogService(path).enrich(
        ModelProfile("c", "same", "same"), provider="", default_adapter="generic_openai"
    )

    assert result.metadata_source == "unknown"
    assert result.context_window is None


def test_catalog_accepts_wrapped_models_dev_catalog(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "models": {"deepseek/deepseek-v4-flash": {"limit": {"context": 1000000}}},
        "providers": {
            "deepseek": {"models": {"deepseek-v4-flash": {
                "name": "DeepSeek V4 Flash",
                "reasoning": True,
                "reasoning_options": [
                    {"type": "toggle"},
                    {"type": "effort", "values": ["low", "high", "max"]},
                ],
                "tool_call": True,
                "limit": {"context": 1000000, "output": 384000},
            }}},
        },
    }), encoding="utf-8")

    result = ModelCatalogService(path).enrich(
        ModelProfile("c", "deepseek-v4-flash", "deepseek-v4-flash"),
        provider="deepseek",
        default_adapter="generic_openai",
    )

    assert result.display_name == "DeepSeek V4 Flash"
    assert result.context_window == 1000000
    assert result.max_output_tokens == 384000
    assert result.reasoning_options == ("none", "low", "high", "max")
    assert result.adapter == "deepseek"
    assert result.metadata_source == "models.dev:deepseek"


def test_catalog_maps_toggle_only_reasoning_to_off_and_on(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "providers": {"deepseek": {"models": {"toggle-model": {
            "reasoning": True,
            "reasoning_options": {"type": "toggle"},
        }}}},
    }), encoding="utf-8")

    result = ModelCatalogService(path).enrich(
        ModelProfile("c", "toggle-model", "Toggle Model"),
        provider="deepseek",
        default_adapter="generic_openai",
    )

    assert result.reasoning_options == ("none", "enabled")


def test_catalog_matches_unique_canonical_base_model_for_gateway_id(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "models": {
            "deepseek/deepseek-v4-pro": {
                "id": "deepseek/deepseek-v4-pro",
                "name": "DeepSeek V4 Pro",
                "reasoning": True,
                "tool_call": True,
                "limit": {"context": 1_000_000, "output": 384_000},
            },
        },
        "providers": {"deepseek": {"models": {"deepseek-v4-pro": {
            "reasoning_options": [
                {"type": "toggle"},
                {"type": "effort", "values": ["high", "max"]},
            ],
        }}}},
    }), encoding="utf-8")

    result = ModelCatalogService(path).enrich(
        ModelProfile("c", "deepseek-ai/DeepSeek-V4-Pro", "remote name"),
        provider="",
        default_adapter="generic_openai",
    )

    assert result.context_window == 1_000_000
    assert result.max_output_tokens == 384_000
    assert result.reasoning_options == ("none", "high", "max")
    assert result.adapter == "deepseek"
    assert result.metadata_source == "models.dev:deepseek"
