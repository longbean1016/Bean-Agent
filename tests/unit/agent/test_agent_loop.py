"""AgentLoop 的 Turn 持久化、事件和错误语义测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.agent_loop import AgentLoop
from agent.event_bus import EventBus, TurnCommitted
from agent.message_bus import InboundMessage, MessageBus, PipelineResult
from agent.config_models import MemoryConfig
from memory.consolidator import ConsolidationDraft
from memory.engine import MemoryEngine
from session.manager import SessionManager


class Pipeline:
    async def process(self, message, *, turn_id):
        return PipelineResult("回答", thinking="思考", tool_chain=[{"iteration": 1, "calls": []}], tools_used=["echo"])


@pytest.mark.asyncio
async def test_run_once_persists_complete_turn_before_committed_and_outbound(tmp_path: Path) -> None:
    bus = MessageBus()
    events = EventBus()
    sessions = SessionManager(tmp_path)
    committed = []
    events.on(TurnCommitted, lambda event: committed.append(event))
    loop = AgentLoop(bus, events, Pipeline(), sessions)
    await bus.publish_inbound(InboundMessage(channel="web", sender="u", chat_id="c", content="问题", metadata={"request_id": "r1"}))

    await loop.run_once()

    rows = sessions.store.fetch_session_messages("web:c")
    outbound = await bus.consume_outbound()
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[1]["tool_chain"][0]["iteration"] == 1
    assert committed[0].user_message_id == rows[0]["id"]
    assert committed[0].assistant_message_id == rows[1]["id"]
    assert outbound.content == "回答"
    await sessions.close()


@pytest.mark.asyncio
async def test_pipeline_failure_persists_error_turn_without_committed(tmp_path: Path) -> None:
    class FailingPipeline:
        async def process(self, message, *, turn_id):
            raise RuntimeError("模型失败")

    bus = MessageBus()
    events = EventBus()
    sessions = SessionManager(tmp_path)
    committed = []
    events.on(TurnCommitted, lambda event: committed.append(event))
    loop = AgentLoop(bus, events, FailingPipeline(), sessions)
    await bus.publish_inbound(InboundMessage(channel="web", sender="u", chat_id="c", content="问题"))

    await loop.run_once()

    rows = sessions.store.fetch_session_messages("web:c")
    outbound = await bus.consume_outbound()
    assert rows[1]["status"] == "error"
    assert committed == []
    assert "模型失败" in outbound.content
    await sessions.close()


@pytest.mark.asyncio
async def test_turn_committed_reaches_memory_worker_with_persisted_snapshot(tmp_path: Path) -> None:
    class Embedder:
        async def embed(self, text): return [1.0, 0.0]
        async def embed_batch(self, texts): return [[1.0, 0.0] for _ in texts]
        async def close(self): pass

    class Provider:
        async def complete(self, messages, tools=None): return type("R", (), {"content": "[]"})()

    class Extractor:
        async def extract(self, messages, previous_recent_context): return ConsolidationDraft()

    bus = MessageBus()
    events = EventBus()
    sessions = SessionManager(tmp_path)
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    memory = MemoryEngine(tmp_path, Embedder(), Provider(), sessions.store, config=config, consolidation_extractor=Extractor())
    captured = []
    memory._post_response.handle = lambda event: _capture(captured, event)
    memory.bind_events(events)
    loop = AgentLoop(bus, events, Pipeline(), sessions)
    await bus.publish_inbound(InboundMessage(channel="web", sender="u", chat_id="c", content="问题"))
    try:
        await loop.run_once()
        await memory.drain()
    finally:
        await memory.close()
        await sessions.close()

    assert captured[0].user_message == "问题"
    assert captured[0].assistant_response == "回答"
    assert captured[0].tool_chain[0]["iteration"] == 1
    assert captured[0].channel == "web"
    assert captured[0].chat_id == "c"


async def _capture(target, event):
    target.append(event)
