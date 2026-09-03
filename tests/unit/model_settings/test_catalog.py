import json
from pathlib import Path

from model_settings.catalog import ModelCatalogService
from model_settings.models import ModelProfile


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
