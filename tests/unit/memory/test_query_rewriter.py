"""查询 Gate、并行改写、解析和 fail-open 降级测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from memory.query_rewriter import QueryRewriter


class LLM:
    def __init__(self, *, fail: bool = False, delay: float = 0) -> None:
        self.fail = fail
        self.delay = delay

    async def complete(self, messages, tools=None):
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("llm unavailable")
        prompt = messages[0]["content"]
        if "记忆检索决策器" in prompt:
            return SimpleNamespace(content="<decision>RETRIEVE</decision><history_query>用户使用的设备型号</history_query>")
        return SimpleNamespace(content="用户询问记忆内容时 agent 应如何查找依据")


@pytest.mark.asyncio
async def test_decide_parses_history_and_procedure_queries() -> None:
    decision = await QueryRewriter(LLM()).decide("你记得我的设备吗", "之前讨论手环")

    assert decision.needs_episodic is True
    assert decision.episodic_query == "用户使用的设备型号"
    assert "如何查找依据" in decision.procedure_query


@pytest.mark.asyncio
async def test_failure_fails_open_to_original_query() -> None:
    decision = await QueryRewriter(LLM(fail=True)).decide("原始问题", "")

    assert decision.needs_episodic is True
    assert decision.episodic_query == "原始问题"
    assert decision.procedure_query == ""


@pytest.mark.asyncio
async def test_timeout_fails_open_without_waiting_for_slow_llm() -> None:
    decision = await QueryRewriter(LLM(delay=0.05), timeout_ms=10).decide("超时问题", "")

    assert decision.needs_episodic is True
    assert decision.episodic_query == "超时问题"
