"""Consolidation 双 Prompt、字段过滤与近期语境渲染测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from memory.engine import _LLMConsolidationExtractor


class Provider:
    def __init__(self): self.prompts = []
    async def complete(self, messages, tools=None, **kwargs):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        if "history_entries" in prompt:
            return SimpleNamespace(content='{"history_entries":[{"summary":"[2026-07-16 10:00] 用户完成模块","emotional_weight":12}],"pending_items":[{"tag":"identity","content":"用户是开发者"},{"tag":"invalid","content":"丢弃"}]}')
        return SimpleNamespace(content='{"active_topics":["记忆模块"],"user_preferences":[],"follow_ups":["继续测试"],"avoidances":[],"ongoing_threads":[]}')


@pytest.mark.asyncio
async def test_consolidation_uses_separate_event_and_recent_context_prompts() -> None:
    provider = Provider()
    draft = await _LLMConsolidationExtractor(provider).extract(
        [{"role": "user", "content": "我完成了记忆模块"}],
        "## Compression\n- 旧语境",
    )

    assert len(provider.prompts) == 2
    assert "transcript" in provider.prompts[0]
    assert "agent_context" in provider.prompts[0]
    assert "ongoing_threads" in provider.prompts[1]
    assert draft.history_entries[0]["emotional_weight"] == 10
    assert draft.pending_items == [{"tag": "identity", "content": "用户是开发者"}]
    assert "## Compression" in draft.recent_context
    assert "最近持续关注：记忆模块" in draft.recent_context
    assert "最近待延续话题：继续测试" in draft.recent_context
