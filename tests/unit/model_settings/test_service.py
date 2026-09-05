from pathlib import Path

import pytest

from model_settings.catalog import ModelCatalogService
from model_settings.discovery import DiscoveredModel
from model_settings.models import ModelRoute
from model_settings.secrets import MemorySecretStore
from model_settings.service import ModelSettingsService, ModelSettingsValidationError
from model_settings.store import ModelSettingsStore


class Discovery:
    async def list_models(self, connection, api_key):
        assert api_key == "top-secret"
        return [DiscoveredModel("model-a", "Model A")]


def service(tmp_path: Path) -> ModelSettingsService:
    return ModelSettingsService(
        ModelSettingsStore(tmp_path / "models.db"),
        MemorySecretStore(),
        Discovery(),
        ModelCatalogService(tmp_path / "catalog.json"),
    )


@pytest.mark.asyncio
async def test_connection_discovery_and_route_round_trip_without_key_exposure(tmp_path: Path) -> None:
    settings = service(tmp_path)
    connection = settings.create_connection({
        "name": "私有网关", "provider": "", "base_url": "https://example.com/v1/chat/completions",
        "api_key": "top-secret", "default_adapter": "generic_openai",
    })
    models = await settings.discover_models(connection["id"])
    route = settings.set_route(ModelRoute(connection["id"], models[0]["model_id"]))

    payload = settings.list_connections()[0]
    assert payload["base_url"] == "https://example.com/v1"
    assert payload["has_api_key"] is True
    assert payload["api_key_preview"] == "top-...cret"
    assert "api_key" not in payload
    assert settings.get_connection_api_key(connection["id"]) == "top-secret"
    assert settings.get_route() == route
    assert b"top-secret" not in (tmp_path / "models.db").read_bytes()


@pytest.mark.asyncio
async def test_model_list_test_does_not_replace_saved_models(tmp_path: Path) -> None:
    settings = service(tmp_path)
    connection = settings.create_connection({
        "name": "私有网关", "base_url": "https://example.com/v1", "api_key": "top-secret",
    })

    result = await settings.test_model_list(connection["id"])

    assert result == {
        "ok": True,
        "connection_id": connection["id"],
        "connection_name": "私有网关",
        "model_count": 1,
    }
    assert settings.store.list_models(connection["id"]) == []


def test_route_rejects_unsupported_reasoning_effort(tmp_path: Path) -> None:
    settings = service(tmp_path)
    connection = settings.create_connection({
        "name": "连接", "base_url": "https://example.com/v1", "api_key": "top-secret",
    })
    settings.save_manual_model(connection["id"], {"model_id": "plain"})
    with pytest.raises(ModelSettingsValidationError, match="不支持"):
        settings.set_route(ModelRoute(connection["id"], "plain", "high"))


def test_route_explains_that_disabled_connection_must_be_enabled(tmp_path: Path) -> None:
    settings = service(tmp_path)
    connection = settings.create_connection({
        "name": "连接", "base_url": "https://example.com/v1", "api_key": "top-secret",
        "enabled": False,
    })
    settings.save_manual_model(connection["id"], {"model_id": "plain"})

    with pytest.raises(ModelSettingsValidationError, match="连接已停用"):
        settings.set_route(ModelRoute(connection["id"], "plain"))


def test_manual_reasoning_toggle_can_be_saved_and_routed(tmp_path: Path) -> None:
    settings = service(tmp_path)
    connection = settings.create_connection({
        "name": "连接", "base_url": "https://example.com/v1", "api_key": "top-secret",
    })
    settings.save_manual_model(connection["id"], {"model_id": "toggle-model"})

    updated = settings.update_model(connection["id"], "toggle-model", {
        "supports_reasoning": True,
        "reasoning_options": ["none", "enabled"],
    })
    route = settings.set_route(ModelRoute(connection["id"], "toggle-model", "enabled"))

    assert updated["reasoning_options"] == ["none", "enabled"]
    assert route.reasoning_effort == "enabled"


def test_manual_reasoning_options_reject_unknown_value(tmp_path: Path) -> None:
    settings = service(tmp_path)
    connection = settings.create_connection({
        "name": "连接", "base_url": "https://example.com/v1", "api_key": "top-secret",
    })
    settings.save_manual_model(connection["id"], {"model_id": "reasoner"})

    with pytest.raises(ModelSettingsValidationError, match="推理选项无效"):
        settings.update_model(connection["id"], "reasoner", {
            "supports_reasoning": True,
            "reasoning_options": ["turbo"],
        })
