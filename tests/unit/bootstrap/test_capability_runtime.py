"""模型能力路由在应用启动时组装客户端的定向测试。"""

from pathlib import Path

from agent.config_models import Config, VisionConfig
from bootstrap.app import (
    build_core_runtime,
    _resolve_embedding_config,
    _resolve_vision_config,
)
from model_settings.catalog import ModelCatalogService
from model_settings.models import ModelRoute
from model_settings.secrets import MemorySecretStore, SqliteSecretStore
from model_settings.service import ModelSettingsService
from model_settings.store import ModelSettingsStore


class _Discovery:
    async def list_models(self, connection, api_key):
        return []


def _settings(tmp_path: Path) -> ModelSettingsService:
    return ModelSettingsService(
        ModelSettingsStore(tmp_path / "models.db"),
        MemorySecretStore(),
        _Discovery(),
        ModelCatalogService(tmp_path / "catalog.json"),
    )


def _connection_and_model(settings: ModelSettingsService, *, name: str, model_id: str, key: str):
    connection = settings.create_connection({
        "name": name,
        "provider": "test-provider",
        "base_url": f"https://{name}.example/v1",
        "api_key": key,
    })
    model = settings.save_manual_model(connection["id"], {
        "model_id": model_id,
        "supports_vision": True,
    })
    return connection, model


def test_embedding_independent_route_uses_saved_model_and_secret(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection, model = _connection_and_model(
        settings, name="embedding", model_id="embed-v2", key="embedding-secret"
    )
    settings.set_capability_route(
        "embedding", ModelRoute(connection["id"], model["model_id"])
    )
    config = Config()
    config.memory.embedding.model = "legacy-embedding"
    config.memory.embedding.api_key = "legacy-secret"

    resolved = _resolve_embedding_config(settings, config)

    assert resolved.model == "embed-v2"
    assert resolved.api_key == "embedding-secret"
    assert resolved.base_url == "https://embedding.example/v1"
    settings.store.close()


def test_embedding_follow_reuses_primary_connection_but_keeps_legacy_model(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection, model = _connection_and_model(
        settings, name="primary", model_id="chat-v4", key="primary-secret"
    )
    settings.set_route(ModelRoute(connection["id"], model["model_id"]))
    config = Config()
    config.memory.embedding.model = "text-embedding-v3"
    config.memory.embedding.api_key = ""
    config.memory.embedding.base_url = ""

    resolved = _resolve_embedding_config(settings, config)

    assert resolved.model == "text-embedding-v3"
    assert resolved.api_key == "primary-secret"
    assert resolved.base_url == "https://primary.example/v1"
    settings.store.close()


def test_vision_independent_route_builds_provider_config_without_adapter(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    connection, model = _connection_and_model(
        settings, name="vision", model_id="vision-v1", key="vision-secret"
    )
    settings.set_capability_route(
        "vision", ModelRoute(connection["id"], model["model_id"])
    )
    config = Config()
    config.llm.vl = VisionConfig(model="legacy-vl", api_key="legacy-secret")

    resolved = _resolve_vision_config(settings, config, multimodal=False)

    assert resolved is not None
    assert resolved.model == "vision-v1"
    assert resolved.api_key == "vision-secret"
    assert resolved.base_url == "https://vision.example/v1"
    settings.store.close()


def test_primary_multimodal_stays_controlled_by_runtime_config(tmp_path: Path) -> None:
    # 主模型能力资料只用于设置页提示；不能在启动时覆盖既有 multimodal 开关，
    # 否则会改变主模型图片输入和工具注册路径。
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = ModelSettingsService(
        ModelSettingsStore(workspace / "model-settings.db"),
        SqliteSecretStore(workspace / "model-settings.db"),
        _Discovery(),
        ModelCatalogService(tmp_path / "catalog.json"),
    )
    connection, model = _connection_and_model(
        settings, name="text", model_id="text-v1", key="text-secret"
    )
    # save_manual_model above marks vision support; update the profile to model a
    # provider catalog entry that explicitly says the primary model is text-only.
    settings.update_model(connection["id"], model["model_id"], {"supports_vision": False})
    settings.set_route(ModelRoute(connection["id"], model["model_id"]))
    config = Config()
    config.memory.enabled = False
    config.llm.multimodal = True

    runtime = build_core_runtime(config, workspace, provider=object())
    try:
        assert runtime.pipeline._multimodal is True
    finally:
        settings.store.close()
        runtime.sessions.store.close()
