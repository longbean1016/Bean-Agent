"""HyDE 只能扩充原始记忆召回，不能改变已有结果。"""

from __future__ import annotations

import pytest

from memory.hyde_enhancer import HyDEEnhancer
from memory.sufficiency_checker import should_enhance_retrieval


class Provider:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def complete(self, messages, tools=None):
        if self.fail:
            raise RuntimeError("model unavailable")
        return type("Response", (), {"content": "用户使用 Fitbit Charge 6"})()


def test_only_empty_results_need_hyde_enhancement() -> None:
    assert should_enhance_retrieval([]) is True
    assert should_enhance_retrieval([{"id": "known"}]) is False


@pytest.mark.asyncio
async def test_hyde_appends_only_new_hits_and_preserves_raw_items() -> None:
    raw = [{"id": "raw", "score": 0.91}]

    async def retrieve(query: str):
        assert query == "用户使用 Fitbit Charge 6"
        return [{"id": "raw", "score": 0.3}, {"id": "hyde", "score": 0.8}]

    result = await HyDEEnhancer(Provider()).augment(
        query="我的设备是什么",
        context="之前聊过手环",
        raw_items=raw,
        retrieve_fn=retrieve,
    )

    assert result.items == [raw[0], {"id": "hyde", "score": 0.8}]
    assert result.used_hyde is True
    assert result.hypothesis == "用户使用 Fitbit Charge 6"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["generate", "retrieve"])
async def test_hyde_failure_returns_original_items(failure: str) -> None:
    raw = [{"id": "raw", "score": 0.91}]

    async def retrieve(query: str):
        raise RuntimeError("search unavailable")

    result = await HyDEEnhancer(Provider(fail=failure == "generate")).augment(
        query="我的设备是什么",
        context="",
        raw_items=raw,
        retrieve_fn=retrieve,
    )

    assert result.items is raw
    assert result.used_hyde is False
