from pathlib import Path

import pytest

from model_settings.models import (
    CapabilityProbe,
    DiscoveryRun,
    ModelConnection,
    ModelProfile,
    ModelRoute,
)
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


def test_model_capabilities_and_protocol_round_trip(tmp_path: Path) -> None:
    store = ModelSettingsStore(tmp_path / "settings.db")
    store.save_connection(connection("one"))
    profile = ModelProfile(
        "one",
        "deepseek-v4-flash",
        "DeepSeek V4 Flash",
        protocol="chat_completions",
        capability_source="probe",
        capability_confidence="high",
        capabilities_json={
            "reasoning": {
                "mode": "effort",
                "native": ["none", "low", "high", "max"],
                "aliases": {"medium": "high", "xhigh": "max"},
                "response_field": "reasoning_content",
            }
        },
    )
    store.save_model(profile)
    actual = store.get_model("one", "deepseek-v4-flash")
    assert actual is not None
    assert actual.protocol == "chat_completions"
    assert actual.capability_source == "probe"
    assert actual.capability_confidence == "high"
    assert actual.capabilities_json["reasoning"]["native"] == ["none", "low", "high", "max"]


def test_discovery_runs_and_capability_probes_are_persisted(tmp_path: Path) -> None:
    store = ModelSettingsStore(tmp_path / "settings.db")
    store.save_connection(connection("one"))
    store.save_model(ModelProfile("one", "deepseek-v4-flash", "DeepSeek V4 Flash"))
    run = store.record_discovery_run(
        DiscoveryRun(
            connection_id="one",
            requested_url="https://example.test/v1/models",
            status="success",
            model_count=2,
            response_hash="abc123",
        )
    )
    probe = store.record_capability_probe(
        CapabilityProbe(
            connection_id="one",
            model_id="deepseek-v4-flash",
            probe_type="reasoning",
            requested_effort="medium",
            effective_effort="high",
            protocol="chat_completions",
            response_reasoning_field="reasoning_content",
            status="verified",
        )
    )
    assert store.list_discovery_runs("one")[0] == run
    assert store.list_capability_probes("one", "deepseek-v4-flash")[0] == probe
