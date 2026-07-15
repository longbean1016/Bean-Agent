"""向量与关键词双路记忆检索、RRF 融合及局部失败隔离。"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Protocol

from memory.store import MemoryStore2

logger = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]+")
_ASCII_RE = re.compile(r"[a-zA-Z0-9_\-.]{2,}")
_STOPWORDS = {"这个", "那个", "我们", "你们", "他们", "什么", "怎么"}


class EmbeddingApi(Protocol):
    async def embed(self, text: str) -> list[float]: ...
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class Retriever:
    """统一承载主动 recall 与 Turn 预检索，避免两条路径语义漂移。"""

    def __init__(self, store: MemoryStore2, embedder: EmbeddingApi, *, top_k: int = 8, score_threshold: float = 0.0, rrf_k: int = 60, keyword_rrf_weight: float = 0.5, hotness_alpha: float = 0.20, hotness_half_life_days: float = 14.0, embed_timeout_s: float = 8.0, business_timeout_s: float = 10.0) -> None:
        self._store = store
        self._embedder = embedder
        self._top_k = max(1, int(top_k))
        self._score_threshold = float(score_threshold)
        self._rrf_k = max(1, int(rrf_k))
        self._keyword_weight = max(0.0, float(keyword_rrf_weight))
        self._hotness_alpha = max(0.0, min(float(hotness_alpha), 1.0))
        self._half_life = max(0.1, float(hotness_half_life_days))
        self._embed_timeout = max(0.001, float(embed_timeout_s))
        self._business_timeout = max(0.001, float(business_timeout_s))

    async def retrieve(self, query: str, memory_types: list[str] | None = None, top_k: int | None = None, scope_channel: str | None = None, scope_chat_id: str | None = None, require_scope_match: bool = False, aux_queries: list[str] | None = None, score_threshold: float | None = None, time_start: datetime | None = None, time_end: datetime | None = None, keyword_enabled: bool = True) -> list[dict[str, object]]:
        actual_top_k = self._top_k if top_k is None else max(1, int(top_k))
        threshold = self._score_threshold if score_threshold is None else float(score_threshold)
        texts = _dedupe([query, *(aux_queries or [])])
        vector_task = asyncio.create_task(self._vector_lane(
            texts, actual_top_k, memory_types, threshold, scope_channel,
            scope_chat_id, require_scope_match, time_start, time_end,
        ))

        keyword_items: list[dict[str, object]] = []
        if keyword_enabled:
            try:
                # 同步 SQLite 查询移到线程，避免在 AgentLoop 事件循环中阻塞其它 Turn。
                keyword_items = await asyncio.to_thread(
                    self._store.keyword_search_summary,
                    extract_query_terms(query),
                    memory_types=memory_types,
                    limit=max(30, actual_top_k * 2),
                    time_start=time_start,
                    time_end=time_end,
                    scope_channel=scope_channel,
                    scope_chat_id=scope_chat_id,
                    require_scope_match=require_scope_match,
                )
            except Exception as error:
                logger.warning("记忆关键词检索失败，保留向量 lane: %s", error)

        try:
            vector_items = await asyncio.wait_for(vector_task, self._business_timeout)
        except TimeoutError:
            # 总预算耗尽只取消向量 lane；已经完成的关键词结果仍可作为降级答案。
            vector_task.cancel()
            vector_items = []
            logger.warning("记忆向量检索超过业务超时 %.3fs", self._business_timeout)
        return rrf_merge(
            vector_items,
            keyword_items,
            top_n=actual_top_k,
            k=self._rrf_k,
            keyword_weight=self._keyword_weight,
        )

    async def _vector_lane(self, texts: list[str], top_k: int, memory_types: list[str] | None, threshold: float, channel: str | None, chat_id: str | None, require_scope: bool, time_start: datetime | None, time_end: datetime | None) -> list[dict[str, object]]:
        vectors = await self._embed_with_rescue(texts)
        if not vectors:
            return []
        kwargs = dict(top_k=top_k, memory_types=memory_types, score_threshold=threshold, scope_channel=channel, scope_chat_id=chat_id, require_scope_match=require_scope, hotness_alpha=self._hotness_alpha, hotness_half_life_days=self._half_life, time_start=time_start, time_end=time_end)
        groups: list[list[dict[str, object]]] = []
        try:
            groups = await asyncio.to_thread(self._store.vector_search_batch, vectors, **kwargs)
        except Exception as error:
            logger.warning("批量向量检索失败，改为逐条补救: %s", error)
        if not groups:
            for vector in vectors:
                try:
                    groups.append(await asyncio.to_thread(self._store.vector_search, vector, **kwargs))
                except Exception as error:
                    # 辅助 query 的局部失败不能丢弃其它 query 已召回的结果。
                    logger.warning("单条向量检索失败，跳过该 lane: %s", error)
        seen: dict[str, dict[str, object]] = {}
        for group in groups:
            for item in group:
                item_id = str(item.get("id", ""))
                previous = seen.get(item_id)
                if item_id and (previous is None or float(item.get("score", 0)) > float(previous.get("score", 0))):
                    seen[item_id] = item
        return list(seen.values())

    async def _embed_with_rescue(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            return await asyncio.wait_for(self._embedder.embed_batch(texts), self._embed_timeout)
        except Exception as error:
            logger.warning("批量 embedding 失败，改为逐条补救: %s", error)
        results = await asyncio.gather(
            *(asyncio.wait_for(self._embedder.embed(text), self._embed_timeout) for text in texts),
            return_exceptions=True,
        )
        return [result for result in results if isinstance(result, list)]


def extract_query_terms(query: str) -> list[str]:
    """提取 CJK bigram 与 ASCII token，并按首次出现顺序去重。"""

    values: list[str] = []
    for match in _CJK_RE.finditer(str(query)):
        sequence = match.group()
        values.extend(sequence[index:index + 2] for index in range(len(sequence) - 1))
    values.extend(_ASCII_RE.findall(str(query)))
    return [value for value in _dedupe(values) if value not in _STOPWORDS]


def rrf_merge(vector_hits: list[dict[str, object]], keyword_hits: list[dict[str, object]], *, top_n: int = 8, k: int = 60, keyword_weight: float = 0.5) -> list[dict[str, object]]:
    vector_ranks = {str(item["id"]): index + 1 for index, item in enumerate(vector_hits)}
    keyword_ranks = {str(item["id"]): index + 1 for index, item in enumerate(keyword_hits)}
    by_id = {str(item["id"]): item for item in [*vector_hits, *keyword_hits]}
    result: list[dict[str, object]] = []
    for item_id in vector_ranks.keys() | keyword_ranks.keys():
        score = 0.0
        if item_id in vector_ranks:
            score += 1.0 / (k + vector_ranks[item_id])
        if item_id in keyword_ranks:
            score += keyword_weight / (k + keyword_ranks[item_id])
        result.append({**by_id[item_id], "score": score})
    result.sort(key=lambda item: (-float(item["score"]), str(item["id"])))
    return result[: max(1, int(top_n))]


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


__all__ = ["Retriever", "extract_query_terms", "rrf_merge"]
