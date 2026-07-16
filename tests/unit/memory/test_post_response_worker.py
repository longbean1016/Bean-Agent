"""每轮回复后 invalidation 检测、保护和 supersede 测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from memory.events import TurnIngested
from memory.post_response_worker import PostResponseMemoryWorker


class Memorizer:
    def __init__(self) -> None:
        self.superseded: list[list[str]] = []
        self.saved = 0

    def supersede_batch(self, ids: list[str]):
        self.superseded.append(ids)
        return ids, []

    async def save_item(self, *args, **kwargs):
        self.saved += 1


class Retriever:
    async def retrieve(self, query: str, memory_types=None):
        return [
            {"id": "old-rule", "summary": "旧下载流程", "score": 0.9},
            {"id": "new-rule", "summary": "本轮新规则", "score": 0.95},
        ]


class Provider:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, messages, tools=None):
        self.calls += 1
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        if "受影响的行为主题" in prompt:
            return SimpleNamespace(content='["下载流程"]')
        return SimpleNamespace(content='["old-rule", "new-rule"]')


@pytest.mark.asyncio
async def test_worker_supersedes_confirmed_old_rule_and_protects_explicit_memory() -> None:
    memorizer = Memorizer()
    worker = PostResponseMemoryWorker(memorizer, Retriever(), Provider())
    event = TurnIngested(
        session_key="web:c",
        channel="web",
        chat_id="c",
        user_message="之前的下载流程错了，不要再用了",
        assistant_response="已了解",
        tool_chain=[{"calls": [{"name": "memorize", "arguments": {"summary": "本轮新规则"}, "result": "已记住（item_id=new-rule；status=new）"}]}],
        source_ref="web:c@post_response:turn-1",
    )

    await worker.handle(event)

    assert memorizer.superseded == [["old-rule"]]
    assert memorizer.saved == 0


@pytest.mark.asyncio
async def test_budget_exhaustion_skips_provider_call() -> None:
    provider = Provider()
    worker = PostResponseMemoryWorker(Memorizer(), Retriever(), provider)

    topics, remaining = await worker._extract_invalidation_topics("废弃旧规则", 0)

    assert topics == []
    assert remaining == 0
    assert provider.calls == 0


def test_collect_explicit_memorized_supports_item_id_and_legacy_formats() -> None:
    worker = PostResponseMemoryWorker(Memorizer(), Retriever(), Provider())
    summaries, protected = worker._collect_explicit_memorized([
        {"calls": [
            {"name": "memorize", "arguments": {"summary": "A"}, "result": "new:legacy_1"},
            {"name": "memorize", "arguments": {"summary": "B"}, "result": "已记住（item_id=modern:2；status=new）"},
        ]}
    ])

    assert summaries == ["A", "B"]
    assert protected == {"legacy_1", "modern:2"}


@pytest.mark.asyncio
async def test_invalidation_prompt_rejects_questions_and_uncertain_guesses() -> None:
    provider = Provider()
    worker = PostResponseMemoryWorker(Memorizer(), Retriever(), provider)

    await worker._extract_invalidation_topics("你的流程是什么？", 1000)

    prompt = provider.prompts[0]
    assert "必须同时满足才触发" in prompt
    assert "用户在询问/确认 agent 的流程" in prompt
    assert "用户在描述/回顾自己的操作" in prompt
    assert "也许/可能/猜测" in prompt
    assert "大多数消息应返回 []" in prompt
