"""OpenAI 兼容的文本向量生成客户端。"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from openai import AsyncOpenAI

from agent.config_models import EmbeddingConfig


class _EmbeddingsResource(Protocol):
    async def create(self, **kwargs: Any) -> object: ...


class _EmbeddingClient(Protocol):
    embeddings: _EmbeddingsResource

    async def close(self) -> None: ...


class Embedder:
    """为记忆模块提供单条和批量文本向量。"""

    MAX_BATCH = 10
    MAX_TEXT_LENGTH = 2000
    BATCH_DELAY_SECONDS = 0.2
    REQUEST_TIMEOUT_SECONDS = 30.0
    MAX_RETRIES = 2

    def __init__(
        self,
        config: EmbeddingConfig,
        *,
        client: _EmbeddingClient | None = None,
    ) -> None:
        if config.dimensions <= 0:
            raise ValueError("Embedding dimensions 必须大于 0")

        self._model = config.model
        self._dimensions = config.dimensions
        self._client: _EmbeddingClient = client or AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url or None,
            # 复用官方 SDK 的退避重试，同时限制单次请求时间，避免记忆链路
            # 在外部向量服务异常时无限占用 Agent Turn。
            timeout=self.REQUEST_TIMEOUT_SECONDS,
            max_retries=self.MAX_RETRIES,
        )
        self._closed = False

    async def embed(self, text: str) -> list[float]:
        """生成单条文本的向量。"""

        vectors = await self.embed_batch([text])
        return vectors[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """按服务端批量限制生成向量，并保持与输入完全相同的顺序。"""

        if self._closed:
            raise RuntimeError("Embedder 已关闭")
        if not texts:
            return []
        if any(not isinstance(text, str) for text in texts):
            raise TypeError("Embedding 输入必须全部是字符串")

        # DashScope 兼容接口限制单批数量和单条长度。截断发生在请求边界，
        # 保证调用方和后续记忆存储拿到的向量数量、位置始终不变。
        normalized = [text[: self.MAX_TEXT_LENGTH] for text in texts]
        vectors: list[list[float]] = []
        for start in range(0, len(normalized), self.MAX_BATCH):
            batch = normalized[start : start + self.MAX_BATCH]
            response = await self._client.embeddings.create(
                model=self._model,
                input=batch,
                dimensions=self._dimensions,
            )
            vectors.extend(self._parse_batch(response, expected_count=len(batch)))

            if start + self.MAX_BATCH < len(normalized):
                await asyncio.sleep(self.BATCH_DELAY_SECONDS)
        return vectors

    async def close(self) -> None:
        """幂等关闭底层 HTTP 客户端。"""

        if self._closed:
            return
        # 只有底层资源成功释放后才标记关闭；若 close 抛错，shutdown
        # 编排仍可再次尝试，不会把未释放资源误判为已关闭。
        await self._client.close()
        self._closed = True

    def _parse_batch(
        self,
        response: object,
        *,
        expected_count: int,
    ) -> list[list[float]]:
        data = getattr(response, "data", None)
        if not isinstance(data, list) or len(data) != expected_count:
            actual_count = len(data) if isinstance(data, list) else 0
            raise RuntimeError(
                f"Embedding 响应数量不一致: 期望 {expected_count}，实际 {actual_count}"
            )

        # OpenAI 兼容服务允许响应顺序与输入不同，必须按 index 还原，
        # 否则向量会被写到错误的记忆条目上。
        ordered = sorted(data, key=lambda item: int(getattr(item, "index", -1)))
        vectors: list[list[float]] = []
        for expected_index, item in enumerate(ordered):
            actual_index = int(getattr(item, "index", -1))
            if actual_index != expected_index:
                raise RuntimeError("Embedding 响应 index 不连续")
            embedding = getattr(item, "embedding", None)
            if not isinstance(embedding, list):
                raise RuntimeError("Embedding 响应缺少向量数据")
            if len(embedding) != self._dimensions:
                raise ValueError(
                    "Embedding 向量维度不一致: "
                    f"期望 {self._dimensions}，实际 {len(embedding)}"
                )
            vectors.append([float(value) for value in embedding])
        return vectors


__all__ = ["Embedder"]
