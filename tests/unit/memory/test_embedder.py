"""Embedding 客户端的离线行为测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent.config_models import EmbeddingConfig
from memory.embedder import Embedder


class _Embeddings:
    def __init__(self, responses: list[object]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return next(self._responses)


class _Client:
    def __init__(self, responses: list[object]) -> None:
        self.embeddings = _Embeddings(responses)
        self.close = AsyncMock()


def _response(*items: tuple[int, list[float]]) -> object:
    return SimpleNamespace(
        data=[
            SimpleNamespace(index=index, embedding=embedding)
            for index, embedding in items
        ]
    )


def _config(*, dimensions: int = 3) -> EmbeddingConfig:
    return EmbeddingConfig(
        model="embedding-test",
        api_key="test-key",
        base_url="https://embedding.example/v1",
        dimensions=dimensions,
    )


def test_client_uses_bounded_timeout_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def create_client(**kwargs: Any) -> _Client:
        captured.update(kwargs)
        return _Client([])

    monkeypatch.setattr("memory.embedder.AsyncOpenAI", create_client)

    Embedder(_config())

    assert captured == {
        "api_key": "test-key",
        "base_url": "https://embedding.example/v1",
        "timeout": 30.0,
        "max_retries": 2,
    }


@pytest.mark.asyncio
async def test_embed_sends_model_input_and_dimensions() -> None:
    client = _Client([_response((0, [0.1, 0.2, 0.3]))])
    embedder = Embedder(_config(), client=client)

    result = await embedder.embed("你好")

    assert result == [0.1, 0.2, 0.3]
    assert client.embeddings.calls == [
        {
            "model": "embedding-test",
            "input": ["你好"],
            "dimensions": 3,
        }
    ]


@pytest.mark.asyncio
async def test_embed_batch_splits_requests_and_restores_response_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _response(
        *[(index, [float(index), 0.0]) for index in reversed(range(10))]
    )
    second = _response((1, [11.0, 0.0]), (0, [10.0, 0.0]))
    client = _Client([first, second])
    embedder = Embedder(_config(dimensions=2), client=client)
    sleep = AsyncMock()
    monkeypatch.setattr("memory.embedder.asyncio.sleep", sleep)
    texts = [f"text-{index}" for index in range(12)]

    result = await embedder.embed_batch(texts)

    assert [vector[0] for vector in result] == [float(index) for index in range(12)]
    assert client.embeddings.calls[0]["input"] == texts[:10]
    assert client.embeddings.calls[1]["input"] == texts[10:]
    sleep.assert_awaited_once_with(0.2)


@pytest.mark.asyncio
async def test_embed_batch_truncates_text_and_empty_input_skips_request() -> None:
    client = _Client([_response((0, [1.0]))])
    embedder = Embedder(_config(dimensions=1), client=client)

    empty_result = await embedder.embed_batch([])
    result = await embedder.embed_batch(["x" * 2001])

    assert empty_result == []
    assert result == [[1.0]]
    assert client.embeddings.calls[0]["input"] == ["x" * 2000]


@pytest.mark.asyncio
async def test_embed_batch_rejects_wrong_vector_dimension() -> None:
    client = _Client([_response((0, [0.1, 0.2]))])
    embedder = Embedder(_config(dimensions=3), client=client)

    with pytest.raises(ValueError, match="向量维度"):
        await embedder.embed_batch(["维度错误"])


@pytest.mark.asyncio
async def test_embed_batch_rejects_incomplete_response() -> None:
    client = _Client([_response((0, [0.1, 0.2, 0.3]))])
    embedder = Embedder(_config(), client=client)

    with pytest.raises(RuntimeError, match="数量不一致"):
        await embedder.embed_batch(["第一条", "第二条"])


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    client = _Client([])
    embedder = Embedder(_config(), client=client)

    await embedder.close()
    await embedder.close()

    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_can_retry_after_client_close_failure() -> None:
    client = _Client([])
    client.close.side_effect = [RuntimeError("关闭失败"), None]
    embedder = Embedder(_config(), client=client)

    with pytest.raises(RuntimeError, match="关闭失败"):
        await embedder.close()
    await embedder.close()

    assert client.close.await_count == 2
