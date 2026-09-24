"""真实存储链路的保存后推送、Turn 工具投影与中断恢复回归。"""

import asyncio

import pytest

from agent.agent_loop import AgentLoop, TurnInterruptState
from agent.event_bus import EventBus, StreamDeltaReady, TurnCommitted
from agent.message_bus import InboundMessage, MessageBus
from agent.pipeline import Pipeline
from agent.prompt_assembler import MessageEnvelopeBuilder, PromptAssembler
from agent.prompt_block import SectionCache, SystemPromptBuilder, default_prompt_blocks
from agent.provider import LLMResponse, ToolCall
from session.manager import SessionManager
from tests.unit.agent.test_pipeline import EchoTool
from tools.registry import ToolRegistry


def pipeline_for(provider, sessions, events, workspace):
    tools = ToolRegistry()
    tools.register(EchoTool())
    return Pipeline(
        provider, tools, events,
        PromptAssembler(SystemPromptBuilder(default_prompt_blocks(), SectionCache()), MessageEnvelopeBuilder()),
        workspace=str(workspace),
        event_appender=sessions.append_session_event,
        surface_appender=sessions.append_surface,
        surface_loader=sessions.load_surface,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_write", [False, True])
async def test_persist_before_publish_and_keep_one_user_assistant_turn(tmp_path, fail_write):
    class Provider:
        calls = 0

        async def chat(self, messages, tools=None, on_content_delta=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                await on_content_delta({"thinking_delta": "先查询"})
                return LLMResponse(None, [ToolCall("call-1", "echo", {"text": "hi"})], thinking="先查询")
            for part in ["完整", "回答", "尾字"]:
                await on_content_delta({"content_delta": part})
            return LLMResponse("完整回答尾字")

    sessions = SessionManager(tmp_path)
    events = EventBus()
    bus = MessageBus()
    published = []
    committed = []

    async def observe(event):
        # 收到页面事件时再从数据库读取，证明推送没有跑到提交前面。
        rows = await sessions.fetch_session_events(event.session_key)
        published.append((event, rows[-1]))

    events.on(StreamDeltaReady, observe)
    events.on(TurnCommitted, lambda event: committed.append(event))
    if fail_write:
        sessions.store._conn.execute("""CREATE TRIGGER fail_stream BEFORE INSERT ON session_events
            WHEN NEW.event_type = 'assistant/chunk' BEGIN SELECT RAISE(ABORT, 'fixture failure'); END""")
    pipeline = pipeline_for(Provider(), sessions, events, tmp_path)
    loop = AgentLoop(bus, events, pipeline, sessions)
    await bus.publish_inbound(InboundMessage("web", "u", "stream", "问题"))
    await loop.run_once()
    if fail_write:
        assert published == []
        assert not any(event["event_type"] == "assistant/chunk"
                       for event in await sessions.fetch_session_events("web:stream"))
    else:
        assert len(published) == 4
        for event, row in published:
            assert row["event_type"] == "assistant/chunk"
            assert row["turn_id"] == event.turn_id
            assert row["data"].get("content_delta", "") == event.content_delta
            assert row["data"].get("thinking_delta", "") == event.thinking_delta
        messages = (await sessions.get_or_create("web:stream")).messages
        assert [row["role"] for row in messages] == ["user", "assistant"]
        assert messages[-1]["content"] == "完整回答尾字"
        assert messages[-1]["tool_chain"][0]["calls"][0]["result"] == "echo:hi:web:stream"
        assert len(committed) == 1
    await sessions.close()
    reopened = SessionManager(tmp_path)
    if not fail_write:
        reloaded = (await reopened.get_or_create("web:stream")).messages
        assert reloaded == messages
    await reopened.close()


@pytest.mark.asyncio
async def test_cancel_close_restart_recovers_partial_without_duplicate(tmp_path):
    ready = asyncio.Event()

    class Provider:
        async def chat(self, messages, tools=None, on_content_delta=None, **kwargs):
            await on_content_delta({"content_delta": "半截回答", "thinking_delta": "思考"})
            ready.set()
            await asyncio.Event().wait()

    sessions = SessionManager(tmp_path)
    pipeline = pipeline_for(Provider(), sessions, EventBus(), tmp_path)
    task = asyncio.create_task(pipeline.process(InboundMessage("web", "u", "stream", "问题"), turn_id="turn"))
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await sessions.close()
    reopened = SessionManager(tmp_path)
    loop = AgentLoop(MessageBus(), EventBus(), pipeline, reopened)
    state = TurnInterruptState(session_key="web:stream", original_user_message="问题", llm_surface_persisted=True)
    await loop._repair_durable_surface(state, "turn")
    first = await reopened.load_surface("web:stream")
    await loop._repair_durable_surface(state, "turn")
    assert await reopened.load_surface("web:stream") == first
    assistants = [item for item in first if item["role"] == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["content"] == "半截回答"
    assert assistants[0]["reasoning_content"] == "思考"
    await reopened.close()
