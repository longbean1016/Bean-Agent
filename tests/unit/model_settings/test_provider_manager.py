from pathlib import Path

import pytest

from agent.config_models import LLMConfig
from model_settings.adapters import AdapterRegistry
from model_settings.catalog import ModelCatalogService
from model_settings.models import ModelConnection, ModelProfile, ModelRoute
from model_settings.provider_manager import ProviderManager
from model_settings.secrets import MemorySecretStore
from model_settings.service import ModelSettingsService
from model_settings.store import ModelSettingsStore


class Discovery:
    async def list_models(self, connection, api_key):
        return []


def setup(tmp_path: Path):
    store = ModelSettingsStore(tmp_path / "models.db")
    secrets = MemorySecretStore()
    connection = store.save_connection(ModelConnection(
        "one", "连接", "deepseek", "https://example.com/v1", "connection:one"
    ))
    profile = store.save_model(ModelProfile(
        "one", "reasoner", "Reasoner", context_window=64000, max_output_tokens=8000,
        supports_reasoning=True, reasoning_options=("low", "high"), adapter="deepseek",
        metadata_source="models.dev:deepseek",
    ))
    secrets.set(connection.secret_ref, "secret-value")
    settings = ModelSettingsService(
        store, secrets, Discovery(), ModelCatalogService(tmp_path / "catalog.json")
    )
    manager = ProviderManager(store, secrets, AdapterRegistry(), LLMConfig(api_key="legacy"))
    return store, settings, manager, connection, profile


@pytest.mark.asyncio
async def test_frozen_route_keeps_original_provider_after_connection_update(tmp_path: Path) -> None:
    store, settings, manager, connection, _profile = setup(tmp_path)
    first = manager.freeze(
        settings, session_key="web:s", requested=ModelRoute("one", "reasoner", "high")
    )
    store.save_connection(ModelConnection(
        connection.id, connection.name, connection.provider,
        "https://changed.example/v1", connection.secret_ref,
    ))
    second = manager.freeze(
        settings, session_key="web:s", requested=ModelRoute("one", "reasoner", "high")
    )

    first_lease = await manager.acquire(first.metadata())
    second_lease = await manager.acquire(second.metadata())
    assert first.cache_key != second.cache_key
    assert first_lease.provider is not second_lease.provider
    assert first_lease.provider._base_url == "https://example.com/v1"
    assert second_lease.provider._base_url == "https://changed.example/v1"
    assert first_lease.provider.runtime_id == first.runtime_id
    assert second_lease.provider.runtime_id == second.runtime_id
    assert "secret-value" not in str(first.metadata())
    await manager.release(first_lease)
    await manager.release(second_lease)
    await manager.close()


@pytest.mark.asyncio
async def test_same_frozen_route_reuses_client_and_reference_counts(tmp_path: Path) -> None:
    _store, settings, manager, _connection, _profile = setup(tmp_path)
    frozen = manager.freeze(settings, session_key="web:s", requested=ModelRoute("one", "reasoner", "low"))
    first = await manager.acquire(frozen.metadata())
    second = await manager.acquire(frozen.metadata())
    assert first.provider is second.provider
    assert manager._active[frozen.cache_key] == 2
    await manager.release(first)
    await manager.release(second)
    assert frozen.cache_key not in manager._active
    await manager.close()
