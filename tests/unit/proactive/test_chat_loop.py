"""主动聊天循环的关键防打扰边界测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agent.provider import LLMResponse, ToolCall
from memory.contracts import MemoryQueryResult, MemoryRecord
from proactive.agent_tools import ProactiveToolFactory
from proactive.chat_loop import ProactiveChatLoop
from proactive.models import SessionProactiveSettings
from proactive.store import ProactiveStore
from tools.registry import ToolRegistry


class _SessionStore:
    def get_session_meta(self, session_key: str):
        return {"key": session_key, "updated_at": "2026-07-20T00:00:00+00:00"}

    def list_chat_messages(self, session_key: str, *, limit: int, offset: int):
        return ([{"role": "user", "content": "请继续处理这个普通请求"}], 1)


class _NeverProvider:
    async def complete(self, *args, **kwargs):
        raise AssertionError("普通回复未结束时不应调用主动判定模型")


class _NeverDelivery:
    async def deliver(self, **kwargs):
        raise AssertionError("普通回复未结束时不应主动投递")


class _NeverTools:
    def create(self, session_key: str):
        raise AssertionError("普通回复未结束时不应创建主动工具会话")


class _AlwaysAdmit:
    def random(self) -> float:
        return 0.0

    def randint(self, start: int, _end: int) -> int:
        return start

    def uniform(self, _start: float, _end: float) -> float:
        return 0.0


@pytest.mark.asyncio
async def test_pending_passive_turn_is_never_repackaged_as_proactive(tmp_path) -> None:
    store = ProactiveStore(tmp_path / "proactive.db")
    store.upsert_settings(SessionProactiveSettings(
        session_key="web:a",
        conversation_enabled=True,
        activity_level="active",
        min_conversation_interval_hours=1,
    ))
    loop = ProactiveChatLoop(
        store,
        _SessionStore(),
        _NeverProvider(),
        _NeverDelivery(),
        _NeverTools(),  # type: ignore[arg-type]
        is_session_busy=lambda _key: False,
        now_fn=lambda: datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
        rng=_AlwaysAdmit(),  # type: ignore[arg-type]
    )

    await loop.run_once()

    assert store.get_state("web:a").last_skip_reason == "unfinished_passive_turn"
    await loop.close()
    store.close()


@pytest.mark.asyncio
async def test_unanswered_proactive_message_is_blocked_before_judging(tmp_path) -> None:
    store = ProactiveStore(tmp_path / "proactive.db")
    store.upsert_settings(SessionProactiveSettings(
        session_key="web:a",
        conversation_enabled=True,
        activity_level="active",
        min_conversation_interval_hours=1,
    ))
    sessions = _CompletedSessionStore()
    sessions.rows = [
        {"role": "user", "content": "继续复习", "timestamp": "1"},
        {"role": "assistant", "content": "我先给你三道题", "timestamp": "2"},
        {
            "role": "assistant",
            "content": "主动跟进", "timestamp": "3", "proactive": True,
        },
    ]
    loop = ProactiveChatLoop(
        store,
        sessions,
        _NeverProvider(),
        _NeverDelivery(),
        _NeverTools(),  # type: ignore[arg-type]
        is_session_busy=lambda _key: False,
        now_fn=lambda: datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
        rng=_AlwaysAdmit(),  # type: ignore[arg-type]
    )

    await loop.run_once()

    assert store.get_state("web:a").last_skip_reason == "waiting_for_reply"
    assert store.get_state("web:a").daily_count == 0
    await loop.close()
    store.close()


class _CompletedSessionStore:
    rows = [
        {"role": "user", "content": "最近想去徒步", "timestamp": "1"},
        {"role": "assistant", "content": "可以看看周边路线", "timestamp": "2"},
    ]

    def get_session_meta(self, session_key: str):
        return {"key": session_key, "updated_at": "2026-07-20T00:00:00+00:00"}

    def get_last_chat_message_timestamp(self, session_key: str):
        return "2026-07-20T09:00:00+00:00"

    def list_chat_messages(self, session_key: str, *, limit: int, offset: int):
        return self.rows[offset:offset + limit], len(self.rows)


class _Memory:
    async def query(self, request):
        return MemoryQueryResult(
            records=[MemoryRecord("m1", "preference", "用户喜欢徒步", 0.9)],
            trace={"read_only": True},
        )


class _ToolProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.received_messages: list[list[dict]] = []

    async def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        self.received_messages.append(list(messages))
        if self.calls == 1:
            calls = [
                ToolCall("c1", "finish_turn", {
                    "decision": "reply",
                    "message": "周末想不想去走走？",
                    "topic": "徒步",
                    "reason": "用户近期表达了兴趣",
                })
            ]
        elif self.calls == 2:
            calls = [ToolCall("c2", "recall_memory", {"query": "徒步兴趣", "limit": 2})]
        else:
            calls = [
                ToolCall("c3", "finish_turn", {
                    "decision": "reply",
                    "message": "周末想不想去走走？",
                    "topic": "徒步",
                    "reason": "用户近期表达了兴趣",
                }),
            ]
        return LLMResponse(None, tool_calls=calls)


class _Delivery:
    def __init__(self) -> None:
        self.items: list[dict] = []

    async def deliver(self, **kwargs):
        self.items.append(kwargs)
        return object()


@pytest.mark.asyncio
async def test_proactive_agent_uses_read_only_tools_and_explicit_reply_finish(
    tmp_path,
) -> None:
    store = ProactiveStore(tmp_path / "proactive.db")
    store.upsert_settings(SessionProactiveSettings(
        session_key="web:a",
        conversation_enabled=True,
        activity_level="active",
        min_conversation_interval_hours=1,
    ))
    sessions = _CompletedSessionStore()
    provider = _ToolProvider()
    delivery = _Delivery()
    tools = ProactiveToolFactory(sessions, _Memory(), ToolRegistry())
    loop = ProactiveChatLoop(
        store,
        sessions,
        provider,
        delivery,
        tools,
        is_session_busy=lambda _key: False,
        now_fn=lambda: datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
        rng=_AlwaysAdmit(),  # type: ignore[arg-type]
    )

    await loop.run_once()

    assert provider.calls == 1
    assert delivery.items[0]["content"] == "周末想不想去走走？"
    assert delivery.items[0]["source"] == "proactive_conversation"
    assert delivery.items[0]["tool_chain"] == []
    assert delivery.items[0]["tools_used"] == []
    assert "get_recent_chat" not in str(provider.received_messages[0])
    assert "message_push" not in str(provider.received_messages[0])
    assert "最近想去徒步" in str(provider.received_messages[0])
    assert store.get_state("web:a").last_skip_reason == ""
    await loop.close()
    store.close()


class _NoToolProvider:
    async def complete(self, messages, tools=None, **kwargs):
        return LLMResponse("直接输出一段文本")


@pytest.mark.asyncio
async def test_proactive_agent_without_finish_tool_defaults_to_skip(tmp_path) -> None:
    store = ProactiveStore(tmp_path / "proactive.db")
    store.upsert_settings(SessionProactiveSettings(
        session_key="web:a",
        conversation_enabled=True,
        activity_level="active",
        min_conversation_interval_hours=1,
    ))
    sessions = _CompletedSessionStore()
    delivery = _Delivery()
    loop = ProactiveChatLoop(
        store,
        sessions,
        _NoToolProvider(),
        delivery,
        ProactiveToolFactory(sessions, _Memory(), ToolRegistry()),
        is_session_busy=lambda _key: False,
        now_fn=lambda: datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
        rng=_AlwaysAdmit(),  # type: ignore[arg-type]
    )

    await loop.run_once()

    assert delivery.items == []
    assert store.get_state("web:a").last_skip_reason == "missing_terminal_tool"
    await loop.close()
    store.close()


class _InvalidArgumentsProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        raise json.JSONDecodeError("missing comma", '{"message":"broken"', 12)


class _CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        call = ToolCall(
            f"c{self.calls}",
            "recall_memory",
            {"query": f"topic-{self.calls}", "limit": 1},
        )
        return LLMResponse(None, tool_calls=[call])


class _ToolThenFinishProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.received_messages: list[list[dict]] = []

    async def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        self.received_messages.append(list(messages))
        if self.calls == 1:
            calls = [ToolCall("c1", "recall_memory", {"query": "面试计划", "limit": 1})]
        else:
            calls = [ToolCall("c2", "finish_turn", {
                "decision": "reply",
                "message": "今天要不要把面试复盘里最薄弱的一项先拆出来？",
                "topic": "面试复盘",
                "reason": "用户近期持续推进面试准备",
            })]
        return LLMResponse(None, tool_calls=calls)


class _RepeatedCallProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        return LLMResponse(None, tool_calls=[
            ToolCall(f"c{self.calls}", "recall_memory", {"query": "same", "limit": 1}),
        ])


def _judge_loop(tmp_path, provider, *, max_iterations: int = 16) -> tuple[ProactiveChatLoop, ProactiveStore]:
    store = ProactiveStore(tmp_path / "proactive.db")
    sessions = _CompletedSessionStore()
    return ProactiveChatLoop(
        store,
        sessions,
        provider,
        _Delivery(),
        ProactiveToolFactory(sessions, _Memory(), ToolRegistry()),
        is_session_busy=lambda _key: False,
        max_iterations=max_iterations,
    ), store


@pytest.mark.asyncio
async def test_invalid_tool_arguments_retry_once_without_traceback(tmp_path, caplog) -> None:
    provider = _InvalidArgumentsProvider()
    loop, store = _judge_loop(tmp_path, provider)

    verdict = await loop._judge("web:a", 0.5)

    assert verdict["reason"] == "invalid_tool_arguments"
    assert provider.calls == 2
    assert not [record for record in caplog.records if record.exc_info]
    await loop.close()
    store.close()


@pytest.mark.asyncio
async def test_proactive_judge_defaults_to_sixteen_tool_steps(tmp_path) -> None:
    provider = _CountingProvider()
    loop, store = _judge_loop(tmp_path, provider)

    verdict = await loop._judge("web:a", 0.5)

    assert verdict["reason"] == "max_iterations"
    assert provider.calls == 16
    await loop.close()
    store.close()


@pytest.mark.asyncio
async def test_tool_use_can_be_followed_by_single_reply_finish(tmp_path) -> None:
    provider = _ToolThenFinishProvider()
    loop, store = _judge_loop(tmp_path, provider, max_iterations=3)

    verdict = await loop._judge("web:a", 0.5)

    assert verdict["action"] == "reply"
    assert provider.calls == 2
    assert verdict["tools_used"] == ["recall_memory"]
    await loop.close()
    store.close()


@pytest.mark.asyncio
async def test_judge_prompt_allows_value_first_followup_for_ongoing_goal(tmp_path) -> None:
    provider = _ToolThenFinishProvider()
    loop, store = _judge_loop(tmp_path, provider, max_iterations=2)

    await loop._judge("web:a", 0.5)

    prompt = provider.received_messages[0][0]["content"]
    assert "持续目标" in prompt
    assert "不因上一轮已有回复就默认话题结束" in prompt
    assert "具体内容" in prompt
    assert "空泛询问" in prompt
    assert "web_search" in prompt
    assert "web_fetch" in prompt
    assert "不得依赖模型记忆编造最新事实" in prompt
    assert "正在忙" in prompt
    assert "应 skip" in prompt
    assert "waiting_for_proactive_reply" not in prompt
    assert "已空闲" not in prompt
    assert "发送把握阈值" not in prompt
    await loop.close()
    store.close()


@pytest.mark.asyncio
async def test_repeated_identical_tool_call_stops_early(tmp_path) -> None:
    provider = _RepeatedCallProvider()
    loop, store = _judge_loop(tmp_path, provider)

    verdict = await loop._judge("web:a", 0.5)

    assert verdict["reason"] == "repeated_tool_call"
    assert provider.calls == 3
    await loop.close()
    store.close()
