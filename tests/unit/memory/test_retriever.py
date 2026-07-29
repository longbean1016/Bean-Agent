"""双路检索的融合、降级、补救与业务超时测试。"""

from __future__ import annotations

import asyncio

import pytest

from memory.retriever import Retriever, extract_query_terms


class Embedder:
    def __init__(self, *, fail: bool = False, delay: float = 0) -> None:
        self.fail = fail
        self.delay = delay

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return [[1.0, float(index)] for index, _ in enumerate(texts)]

    async def embed(self, text: str) -> list[float]:
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return [1.0, 0.0]


class Store:
    def __init__(self, *, batch_fail: bool = False, vector_fail: bool = False) -> None:
        self.batch_fail = batch_fail
        self.vector_fail = vector_fail
        self.single_calls = 0

    def vector_search_batch(self, vectors, **kwargs):
        if self.batch_fail:
            raise RuntimeError("batch failed")
        return [[{"id": "both", "summary": "vector", "score": 0.9}]]

    def vector_search(self, vector, **kwargs):
        self.single_calls += 1
        if self.vector_fail:
            raise RuntimeError("single failed")
        return [{"id": "vector", "summary": "rescued", "score": 0.8}]

    def keyword_search_summary(self, terms, **kwargs):
        return [{"id": "both", "summary": "keyword", "keyword_score": 1.0}, {"id": "keyword", "summary": "literal", "keyword_score": 0.5}]


@pytest.mark.asyncio
async def test_retriever_fuses_vector_and_keyword_with_rrf() -> None:
    items = await Retriever(Store(), Embedder()).retrieve("上海 Python", top_k=2)

    assert [item["id"] for item in items] == ["both", "keyword"]
    assert items[0]["score"] == 0.9
    assert items[0]["rrf_score"] > items[1]["rrf_score"]


@pytest.mark.asyncio
async def test_embedding_failure_degrades_to_keyword_lane() -> None:
    items = await Retriever(Store(), Embedder(fail=True)).retrieve("上海计划")

    assert {item["id"] for item in items} == {"both", "keyword"}


@pytest.mark.asyncio
async def test_batch_search_failure_retries_vectors_individually() -> None:
    store = Store(batch_fail=True)

    items = await Retriever(store, Embedder()).retrieve("query", aux_queries=["aux"])

    assert store.single_calls == 2
    assert any(item["id"] == "vector" for item in items)


@pytest.mark.asyncio
async def test_business_timeout_returns_available_keyword_results() -> None:
    retriever = Retriever(Store(), Embedder(delay=0.05), business_timeout_s=0.01)

    items = await retriever.retrieve("timeout keyword")

    assert {item["id"] for item in items} == {"both", "keyword"}


def test_extract_query_terms_matches_default_memory_tokenization() -> None:
    assert extract_query_terms(
        "准备面试 长期记忆检索 Python Charge-6 用户"
    ) == [
        "Python",
        "Charge-6",
        "准备面试",
        "长期",
        "期记",
        "记忆",
        "忆检",
        "检索",
    ]


def test_extract_query_terms_deduplicates_and_limits_results() -> None:
    query = " ".join(["token0", "token0", *(f"token{index}" for index in range(1, 25))])

    terms = extract_query_terms(query)

    assert terms == [f"token{index}" for index in range(20)]
