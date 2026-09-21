from pathlib import Path
import json

import httpx
import pytest

from model_settings.catalog import ModelCatalogService
from model_settings.models import ModelRoute
from model_settings.secrets import MemorySecretStore
from model_settings.service import (
    ModelCapabilityTestError,
    ModelSettingsService,
    ModelSettingsValidationError,
    normalize_base_url,
)
from model_settings.store import ModelSettingsStore


def make_service(
    tmp_path: Path,
    *,
    client: httpx.AsyncClient | None = None,
    dimensions: int = 4,
) -> ModelSettingsService:
    return ModelSettingsService(
        ModelSettingsStore(tmp_path / "models.db"),
        MemorySecretStore(),
        _Discovery(),
        ModelCatalogService(tmp_path / "catalog.json"),
        embedding_dimensions=dimensions,
        capability_client=client,
    )


class _Discovery:
    async def list_models(self, connection, api_key):
        return []


def configure(service: ModelSettingsService) -> tuple[dict, dict]:
    connection = service.create_connection({
        "name": "网关", "base_url": "https://example.test/v1", "api_key": "secret",
    })
    model = service.save_manual_model(connection["id"], {"model_id": "model-a"})
    return connection, model


def test_capability_route_falls_back_without_new_route_scope(tmp_path: Path) -> None:
    settings = make_service(tmp_path)
    connection, model = configure(settings)
    primary = settings.set_route(ModelRoute(connection["id"], model["model_id"]))

    state = settings.capability_state("embedding")
    assert state["mode"] == "follow"
    assert state["follows_primary"] is True
    assert state["route"] == primary.public_dict()
    assert state["override_route"] is None

    independent = ModelRoute(connection["id"], model["model_id"])
    settings.set_capability_route("embedding", independent)
    assert settings.capability_state("embedding")["mode"] == "independent"
    assert settings.store.get_route("global:embedding") == independent

    settings.set_capability_route("embedding", follow_primary=True)
    assert settings.store.get_route("global:embedding") is None
    assert settings.get_capability_route("embedding") == primary


def test_capability_route_rejects_unknown_capability(tmp_path: Path) -> None:
    settings = make_service(tmp_path)
    with pytest.raises(ModelSettingsValidationError, match="能力无效"):
        settings.capability_state("audio")


def test_base_url_normalization_accepts_embedding_endpoint_suffix() -> None:
    assert normalize_base_url("https://example.test/v1/embeddings") == "https://example.test/v1"


@pytest.mark.asyncio
async def test_embedding_capability_probe_validates_expected_dimension(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = make_service(tmp_path, client=client, dimensions=4)
    connection, model = configure(settings)
    result = await settings.test_capability(
        "embedding", route=ModelRoute(connection["id"], model["model_id"])
    )

    assert result["dimensions"] == 4
    assert result["expected_dimension"] == 4
    assert requests[0].url.path == "/v1/embeddings"
    assert requests[0].headers["authorization"] == "Bearer secret"
    request_payload = json.loads(requests[0].content)
    assert request_payload["model"] == "model-a"
    assert request_payload["input"] == ["BeanAgent capability probe"]
    assert request_payload["dimensions"] == 4
    await client.aclose()


@pytest.mark.asyncio
async def test_embedding_probe_reports_dimension_mismatch(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = make_service(tmp_path, client=client, dimensions=4)
    connection, model = configure(settings)
    with pytest.raises(ModelCapabilityTestError, match="维度不一致"):
        await settings.test_capability(
            "embedding", route=ModelRoute(connection["id"], model["model_id"])
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_vision_capability_probe_sends_image_content(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = make_service(tmp_path, client=client)
    connection, model = configure(settings)
    result = await settings.test_capability(
        "vision", route=ModelRoute(connection["id"], model["model_id"])
    )

    payload = json.loads(requests[0].content)
    content = payload["messages"][0]["content"]
    assert result["vision_received"] is True
    assert requests[0].url.path == "/v1/chat/completions"
    assert any(item["type"] == "image_url" for item in content)
    await client.aclose()
