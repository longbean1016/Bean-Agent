from pathlib import Path
import json

from fastapi.testclient import TestClient

from agent.config_models import Config
from bootstrap.app import _freeze_web_model_route, _import_legacy_model_settings, build_core_runtime, create_fastapi_app
from model_settings.secrets import MemorySecretStore
from model_settings.models import ModelConnection, ModelProfile, ModelRoute
from model_settings.store import ModelSettingsStore


class Provider:
    async def chat(self, *args, **kwargs):
        raise AssertionError("设置接口不应调用主模型")

    async def complete(self, *args, **kwargs):
        raise AssertionError("设置接口不应调用主模型")

    async def close(self):
        return None


def test_startup_refresh_returns_catalog_capacity_for_existing_connection(tmp_path):
    workspace = tmp_path / "workspace"
    store = ModelSettingsStore(workspace / "model-settings.db")
    store.save_connection(ModelConnection("one", "我的连接", "deep seek", "https://example.com/v1", "one-key"))
    store.save_model(ModelProfile("one", "deepseek-flash", "Flash"))
    store.set_route("global", ModelRoute("one", "deepseek-flash"))
    store.close()
    catalog = workspace / "catalog" / "models-dev-catalog.json"
    catalog.parent.mkdir()
    catalog.write_text(json.dumps({"providers": {
        "deepseek": {"models": {"deepseek-flash": {"limit": {"context": 1_000_000, "output": 393_216}}}},
        "gateway": {"models": {"deepseek-flash": {"limit": {"context": 64000}}}},
    }}), encoding="utf-8")
    secrets = MemorySecretStore()
    secrets.set("one-key", "fixture-key")
    config = Config()
    config.memory.enabled = False
    runtime = build_core_runtime(config, workspace, provider=Provider(), model_secret_store=secrets)
    with TestClient(create_fastapi_app(runtime)) as test_client:
        response = test_client.get("/api/settings").json()
        connection = response["connections"][0]
        profile = connection["models"][0]
        assert connection["name"] == "我的连接"
        assert connection["provider"] == "deep seek"
        assert profile["model_id"] == "deepseek-flash"
        assert profile["context_window"] == 1_000_000
        frozen = runtime.provider_manager.freeze(runtime.model_settings, session_key="web:s", requested=None)
        provider = runtime.provider_manager._providers[frozen.cache_key]
        assert provider.context_window == profile["context_window"]
        assert provider.context_window_source == profile["metadata_source"]
        assert provider.max_tokens == 8192


def test_legacy_import_does_not_treat_missing_capacity_as_manual_null(tmp_path):
    from tests.unit.model_settings.test_service import service

    settings = service(tmp_path)
    config = Config()
    config.llm.provider = "deep seek"
    config.llm.model = "deepseek-flash"
    config.llm.base_url = "https://example.com/v1"
    config.llm.api_key = "fixture-key"
    settings._catalog._catalog = {"deepseek": {"models": {"deepseek-flash": {
        "limit": {"context": 1_000_000, "output": 393_216},
    }}}}
    _import_legacy_model_settings(settings, config)
    profile = settings.list_connections()[0]["models"][0]
    assert "context_window" not in profile["user_overrides"]
    assert profile["context_window"] == 1_000_000
    assert profile["user_overrides"]["max_output_tokens"] == 8192


def client(tmp_path: Path):
    config = Config()
    config.memory.enabled = False
    runtime = build_core_runtime(
        config,
        tmp_path / "workspace",
        provider=Provider(),
        model_secret_store=MemorySecretStore(),
    )
    return TestClient(create_fastapi_app(runtime)), runtime


def test_model_settings_route_returns_spa_index_or_build_hint(tmp_path: Path) -> None:
    test_client, _runtime = client(tmp_path)
    with test_client:
        response = test_client.get("/settings/models")

    assert response.status_code == 200
    assert response.headers["content-type"].split(";", 1)[0] in {
        "text/html", "application/json"
    }


def test_settings_api_manages_connection_manual_model_and_default_route(tmp_path: Path) -> None:
    test_client, runtime = client(tmp_path)
    with test_client:
        created = test_client.post("/api/settings/connections", json={
            "name": "第三方", "provider": "", "base_url": "https://proxy.example/v1",
            "api_key": "super-secret", "default_adapter": "generic_openai",
        })
        assert created.status_code == 201
        connection = created.json()
        assert connection["has_api_key"] is True
        assert connection["api_key_preview"] == "supe...cret"
        assert "api_key" not in connection

        revealed = test_client.get(
            f"/api/settings/connections/{connection['id']}/api-key"
        )
        assert revealed.status_code == 200
        assert revealed.json() == {"api_key": "super-secret"}

        model = test_client.post(
            f"/api/settings/connections/{connection['id']}/models",
            json={"model_id": "custom/model", "context_window": 32000, "supports_tools": True},
        )
        assert model.status_code == 201
        assert model.json()["context_window"] == 32000

        route = test_client.put("/api/settings/routes/default", json={
            "connection_id": connection["id"], "model_id": "custom/model",
        })
        assert route.status_code == 200

        settings = test_client.get("/api/settings").json()
        assert settings["default_route"]["model_id"] == "custom/model"
        assert settings["connections"][0]["models"][0]["model_id"] == "custom/model"
        assert "super-secret" not in str(settings)
        assert b"super-secret" not in runtime.model_store.path.read_bytes()

        conflict = test_client.delete(f"/api/settings/connections/{connection['id']}")
        assert conflict.status_code == 409


def test_settings_api_validates_url_and_adapter(tmp_path: Path) -> None:
    test_client, _runtime = client(tmp_path)
    with test_client:
        invalid_url = test_client.post("/api/settings/connections", json={
            "name": "bad", "base_url": "file:///tmp/model", "api_key": "secret",
        })
        invalid_adapter = test_client.post("/api/settings/connections", json={
            "name": "bad", "base_url": "https://example.com/v1", "api_key": "secret",
            "default_adapter": "arbitrary",
        })
    assert invalid_url.status_code == 400
    assert invalid_adapter.status_code == 400


def test_first_web_turn_persists_and_freezes_requested_session_route(tmp_path: Path) -> None:
    test_client, runtime = client(tmp_path)
    with test_client:
        connection = runtime.model_settings.create_connection({
            "name": "route", "base_url": "https://example.com/v1", "api_key": "secret",
        })
        runtime.model_settings.save_manual_model(connection["id"], {"model_id": "model-a"})
        frozen = _freeze_web_model_route(runtime, "web:new", {
            "connection_id": connection["id"], "model_id": "model-a", "reasoning_effort": None,
        })

        assert runtime.model_settings.get_route("web:new").model_id == "model-a"
        assert frozen["connection_id"] == connection["id"]
        assert "secret" not in str(frozen)


def test_session_route_api_rejects_non_web_scope(tmp_path: Path) -> None:
    test_client, _runtime = client(tmp_path)
    with test_client:
        response = test_client.get("/api/settings/routes/session/global")
    assert response.status_code == 400


def test_capability_route_api_supports_follow_and_independent_modes(tmp_path: Path) -> None:
    test_client, runtime = client(tmp_path)
    with test_client:
        connection = test_client.post("/api/settings/connections", json={
            "name": "能力网关", "base_url": "https://example.com/v1", "api_key": "secret",
        }).json()
        test_client.post(
            f"/api/settings/connections/{connection['id']}/models",
            json={"model_id": "main-model"},
        )
        primary = test_client.put("/api/settings/routes/default", json={
            "connection_id": connection["id"], "model_id": "main-model",
        })
        assert primary.status_code == 200

        initial = test_client.get("/api/settings/capabilities/embedding")
        assert initial.status_code == 200
        assert initial.json()["mode"] == "follow"
        assert initial.json()["route"]["model_id"] == "main-model"

        independent = test_client.put("/api/settings/capabilities/embedding", json={
            "mode": "independent", "connection_id": connection["id"], "model_id": "main-model",
        })
        assert independent.status_code == 200
        assert independent.json()["mode"] == "independent"
        assert independent.json()["override_route"]["model_id"] == "main-model"
        assert independent.json()["requires_restart"] is True
        assert independent.json()["runtime_effective_at"] == "next_start"

        reset = test_client.put("/api/settings/capabilities/embedding", json={"mode": "follow"})
        assert reset.status_code == 200
        assert reset.json()["mode"] == "follow"
        settings = test_client.get("/api/settings").json()
        assert settings["capability_routes"]["vision"]["mode"] == "follow"
        assert "secret" not in str(settings)

        invalid = test_client.put("/api/settings/capabilities/embedding", json={"mode": "unknown"})
        assert invalid.status_code == 400

    runtime.model_store.close()
