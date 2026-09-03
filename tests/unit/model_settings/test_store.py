from pathlib import Path

import pytest

from model_settings.models import ModelConnection, ModelProfile, ModelRoute
from model_settings.store import ModelSettingsConflict, ModelSettingsStore


def connection(connection_id: str) -> ModelConnection:
    return ModelConnection(
        id=connection_id,
        name=f"连接 {connection_id}",
        provider="deepseek",
        base_url="https://example.com/v1",
        secret_ref=f"connection:{connection_id}",
    )


def test_models_are_isolated_by_connection_and_keys_never_enter_sqlite(tmp_path: Path) -> None:
    store = ModelSettingsStore(tmp_path / "models.db")
    store.save_connection(connection("first"))
    store.save_connection(connection("second"))
    store.save_model(ModelProfile("first", "same-model", "第一个"))
    store.save_model(ModelProfile("second", "same-model", "第二个"))

    assert store.get_model("first", "same-model").display_name == "第一个"
    assert store.get_model("second", "same-model").display_name == "第二个"
    assert b"real-api-key" not in (tmp_path / "models.db").read_bytes()


def test_refresh_marks_missing_model_unavailable_and_preserves_overrides(tmp_path: Path) -> None:
    store = ModelSettingsStore(tmp_path / "models.db")
    store.save_connection(connection("one"))
    old = ModelProfile("one", "old", "旧模型").with_overrides({"context_window": 12345})
    keep = ModelProfile("one", "keep", "保留模型").with_overrides({"display_name": "我的名称"})
    store.save_model(old)
    store.save_model(keep)

    profiles = store.replace_discovered_models(
        "one", [ModelProfile("one", "keep", "目录名称", context_window=999)]
    )

    by_id = {item.model_id: item for item in profiles}
    assert by_id["old"].available is False
    assert by_id["old"].context_window == 12345
    assert by_id["keep"].display_name == "我的名称"
    assert by_id["keep"].context_window == 999


def test_connection_delete_rejects_active_route(tmp_path: Path) -> None:
    store = ModelSettingsStore(tmp_path / "models.db")
    store.save_connection(connection("one"))
    store.save_model(ModelProfile("one", "model", "模型"))
    store.set_route("global", ModelRoute("one", "model"))

    with pytest.raises(ModelSettingsConflict):
        store.delete_connection("one")

    store.delete_route("global")
    assert store.delete_connection("one") is True
