import httpx
import pytest

from model_settings.discovery import ModelAuthenticationError, OpenAIModelDiscovery
from model_settings.models import ModelConnection


def connection() -> ModelConnection:
    return ModelConnection("one", "测试", "openai", "https://models.example/v1", "secret")


@pytest.mark.asyncio
async def test_discovery_normalizes_and_deduplicates_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://models.example/v1/models"
        assert request.headers["authorization"] == "Bearer key-value"
        return httpx.Response(200, json={"data": [
            {"id": "gpt-x", "name": "GPT X"},
            {"id": "gpt-x"},
            {"name": "manual-name"},
            {"id": ""},
        ]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    models = await OpenAIModelDiscovery(client).list_models(connection(), "key-value")
    await client.aclose()

    assert [(item.id, item.name) for item in models] == [
        ("gpt-x", "GPT X"), ("manual-name", "manual-name")
    ]


@pytest.mark.asyncio
async def test_discovery_distinguishes_authentication_failure() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(401, text="secret body"))
    )
    with pytest.raises(ModelAuthenticationError, match="API Key"):
        await OpenAIModelDiscovery(client).list_models(connection(), "bad-key")
    await client.aclose()
