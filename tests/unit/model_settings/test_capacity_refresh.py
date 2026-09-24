"""目录补全、手工覆盖、冻结路由与输出预算的一致性回归。"""

from dataclasses import replace

import pytest

from model_settings.models import ModelProfile, ModelRoute
from model_settings.service import ModelSettingsValidationError
from tests.unit.model_settings.test_provider_manager import setup


def catalog_payload():
    return {"providers": {
        "deepseek": {"models": {"deepseek-flash": {
            "name": "Flash", "limit": {"context": 1_000_000, "output": 393_216},
        }}},
        "gateway": {"models": {"deepseek-flash": {
            "limit": {"context": 64_000, "output": 4_000},
        }}},
    }}


@pytest.mark.asyncio
async def test_existing_profile_refresh_keeps_identity_route_and_frozen_request(tmp_path):
    store, settings, manager, connection, _ = setup(tmp_path)
    store.save_connection(replace(connection, provider="deep seek"))
    store.save_model(ModelProfile("one", "deepseek-flash", "Flash"))
    route = settings.set_route(ModelRoute("one", "deepseek-flash"))
    old = manager.freeze(settings, session_key="web:s", requested=route)
    old_lease = await manager.acquire(old.metadata())
    old_capacity = old_lease.provider.context_window
    settings._catalog._catalog = catalog_payload()
    settings.refresh_cached_profiles()
    updated = store.get_model("one", "deepseek-flash")
    assert updated.context_window == 1_000_000
    returned = next(item for item in settings.list_connections()[0]["models"]
                    if item["model_id"] == "deepseek-flash")
    assert returned["context_window"] == updated.context_window
    assert returned["metadata_source"] == updated.metadata_source
    assert settings.get_route() == route
    assert store.get_connection("one").provider == "deep seek"
    fresh = manager.freeze(settings, session_key="web:s", requested=route)
    lease = await manager.acquire(fresh.metadata())
    assert fresh.cache_key != old.cache_key
    assert lease.provider.context_window == 1_000_000
    assert lease.provider.context_window_source == "models.dev:deepseek"
    assert lease.provider.max_tokens == 8192
    assert old_lease.provider.context_window == old_capacity
    settings.refresh_cached_profiles()
    assert store.get_model("one", "deepseek-flash").revision == updated.revision
    await manager.release(old_lease)
    await manager.release(lease)
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [None, 32_000])
async def test_manual_capacity_and_output_survive_refresh(tmp_path, capacity):
    store, settings, manager, _, _ = setup(tmp_path)
    settings._catalog._catalog = catalog_payload()
    settings.save_manual_model("one", {
        "model_id": "deepseek-flash", "context_window": capacity, "max_output_tokens": 12_000,
    })
    settings.refresh_cached_profiles()
    model = store.get_model("one", "deepseek-flash")
    assert model.context_window == capacity
    assert "context_window" in model.user_overrides
    assert model.user_overrides["context_window"] == capacity
    frozen = manager.freeze(settings, session_key="web:s", requested=ModelRoute("one", "deepseek-flash"))
    lease = await manager.acquire(frozen.metadata())
    assert lease.provider.context_window == (capacity or 0)
    assert lease.provider.context_window_source == "user_override"
    assert lease.provider.max_tokens == 12_000
    await manager.release(lease)
    await manager.close()


def test_provider_edit_reenriches_and_clears_unmatched_old_capacity(tmp_path):
    store, settings, _, _, _ = setup(tmp_path)
    settings._catalog._catalog = catalog_payload()
    settings.save_manual_model("one", {"model_id": "deepseek-flash"})
    settings.update_connection("one", {"provider": "gateway"})
    assert store.get_model("one", "deepseek-flash").context_window == 64_000
    settings.update_connection("one", {"provider": "private-unknown"})
    profile = store.get_model("one", "deepseek-flash")
    assert profile.context_window is None
    assert profile.max_output_tokens is None


@pytest.mark.asyncio
async def test_catalog_update_refreshes_existing_models(tmp_path, monkeypatch):
    store, settings, _, _, _ = setup(tmp_path)
    store.save_model(ModelProfile("one", "deepseek-flash", "Flash"))
    async def update():
        settings._catalog._catalog = catalog_payload()
        return {"updated_at": "2026-01-01"}
    monkeypatch.setattr(settings._catalog, "update", update)
    await settings.update_catalog()
    assert store.get_model("one", "deepseek-flash").context_window == 1_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize("limit, expected", [(4000, 4000), (393216, 8192)])
async def test_catalog_output_only_caps_default_request(tmp_path, limit, expected):
    store, settings, manager, _, profile = setup(tmp_path)
    store.save_model(replace(profile, max_output_tokens=limit))
    frozen = manager.freeze(settings, session_key="web:s", requested=ModelRoute("one", "reasoner"))
    lease = await manager.acquire(frozen.metadata())
    assert lease.provider.max_tokens == expected
    await manager.release(lease)
    await manager.close()


def test_invalid_manual_output_is_rejected_not_silently_rewritten(tmp_path):
    _, settings, manager, _, _ = setup(tmp_path)
    settings.update_model("one", "reasoner", {"context_window": 4000, "max_output_tokens": 8000})
    with pytest.raises(ModelSettingsValidationError, match="输出预算"):
        manager.freeze(settings, session_key="web:s", requested=ModelRoute("one", "reasoner"))


@pytest.mark.asyncio
async def test_discovery_reenriches_existing_profile_and_keeps_null_override(tmp_path):
    from model_settings.discovery import DiscoveredModel

    store, settings, _, _, _ = setup(tmp_path)
    settings.save_manual_model("one", {"model_id": "deepseek-flash", "context_window": None})
    settings._catalog._catalog = catalog_payload()

    class Discovery:
        async def list_models(self, connection, api_key):
            return [DiscoveredModel("deepseek-flash", "Flash")]

    settings._discovery = Discovery()
    await settings.discover_models("one")
    model = store.get_model("one", "deepseek-flash")
    assert model.context_window is None
    assert model.user_overrides == {"context_window": None}
    assert model.max_output_tokens == 393_216
