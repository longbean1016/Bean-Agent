"""Consolidation 双 Prompt、字段过滤与近期语境渲染测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from memory.engine import _LLMConsolidationExtractor


class Provider:
    def __init__(self):
        self.prompts = []
        self.prompts_by_function = {}
        self.calls = []

    async def complete(self, messages, tools=None, **kwargs):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        function_name = tools[0]["function"]["name"]
        self.prompts_by_function[function_name] = prompt
        self.calls.append((function_name, kwargs))
        if function_name == "submit_consolidation_events":
            arguments = {
                "history_entries": [{"summary": "[2026-07-16 10:00] 用户完成模块", "emotional_weight": 12}],
                "pending_items": [
                    {"tag": "identity", "content": "用户是开发者"},
                    {"tag": "invalid", "content": "丢弃"},
                ],
            }
        else:
            arguments = {
                "active_topics": ["记忆模块"],
                "user_preferences": [],
                "follow_ups": ["继续测试"],
                "avoidances": [],
                "dormant_threads": ["旧流式方案讨论", "旧语音输入讨论", "旧代码高亮讨论", "旧刷新恢复讨论", "旧提醒确认讨论", "第六条应丢弃"],
                "ongoing_threads": [],
            }
        return SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(name=function_name, arguments=arguments)],
        )


@pytest.mark.asyncio
async def test_consolidation_uses_separate_event_and_recent_context_prompts() -> None:
    provider = Provider()
    draft = await _LLMConsolidationExtractor(provider).extract(
        [{
            "role": "user",
            "content": "我完成了记忆模块",
            "timestamp": "2026-07-16T10:00:00+08:00",
        }],
        "## Compression\n- 旧语境",
    )

    assert len(provider.prompts) == 2
    assert {name for name, _ in provider.calls} == {
        "submit_consolidation_events",
        "submit_recent_context",
    }
    for name, kwargs in provider.calls:
        assert kwargs["tool_choice"] == {
            "type": "function",
            "function": {"name": name},
        }
        assert kwargs["disable_thinking"] is True
    event_prompt = provider.prompts_by_function["submit_consolidation_events"]
    recent_prompt = provider.prompts_by_function["submit_recent_context"]
    assert "transcript" in event_prompt
    assert "agent_context" in event_prompt
    assert "2026-07-16T10:00:00+08:00" in event_prompt
    assert "Memory Extraction Agent" in event_prompt
    assert "旧语境" not in event_prompt
    assert "旧语境" in recent_prompt
    assert "ongoing_threads" in recent_prompt
    assert "dormant_threads" in recent_prompt
    assert draft.history_entries[0]["emotional_weight"] == 10
    assert draft.pending_items == [{"tag": "identity", "content": "用户是开发者"}]
    assert "## Compression" in draft.recent_context
    assert "最近持续关注：记忆模块" in draft.recent_context
    assert "最近待延续话题：继续测试" in draft.recent_context
    assert "## Dormant Threads" in draft.recent_context
    assert "旧流式方案讨论" in draft.recent_context
    assert "旧提醒确认讨论" in draft.recent_context
    assert "第六条应丢弃" not in draft.recent_context


@pytest.mark.asyncio
async def test_consolidation_retries_recent_context_function_once() -> None:
    class RetryProvider(Provider):
        def __init__(self):
            super().__init__()
            self.recent_attempts = 0

        async def complete(self, messages, tools=None, **kwargs):
            function_name = tools[0]["function"]["name"]
            if function_name == "submit_recent_context":
                self.recent_attempts += 1
                if self.recent_attempts == 1:
                    return SimpleNamespace(content="", tool_calls=[])
            return await super().complete(messages, tools=tools, **kwargs)

    provider = RetryProvider()
    draft = await _LLMConsolidationExtractor(provider).extract(
        [{"role": "user", "content": "继续测试", "timestamp": "2026-07-16T10:00:00+08:00"}],
        "",
    )

    assert provider.recent_attempts == 2
    assert "最近持续关注：记忆模块" in draft.recent_context


@pytest.mark.asyncio
async def test_consolidation_starts_event_and_recent_extraction_concurrently() -> None:
    class CoordinatedProvider(Provider):
        def __init__(self):
            super().__init__()
            self.started: set[str] = set()
            self.both_started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, messages, tools=None, **kwargs):
            function_name = tools[0]["function"]["name"]
            self.started.add(function_name)
            if len(self.started) == 2:
                self.both_started.set()
            await self.release.wait()
            return await super().complete(messages, tools=tools, **kwargs)

    provider = CoordinatedProvider()
    extraction = asyncio.create_task(
        _LLMConsolidationExtractor(provider).extract(
            [{"role": "user", "content": "并发测试"}],
            "",
        )
    )
    try:
        await asyncio.wait_for(provider.both_started.wait(), timeout=0.5)
    finally:
        provider.release.set()
        await extraction

    assert provider.started == {
        "submit_consolidation_events",
        "submit_recent_context",
    }
