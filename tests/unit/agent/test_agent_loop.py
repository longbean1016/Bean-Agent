"""AgentLoop 的 Turn 持久化、事件和错误语义测试。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from agent.agent_loop import AgentLoop, TurnInterruptState
from agent.event_bus import EventBus, TurnCommitted
from agent.message_bus import InboundMessage, MessageBus, PipelineResult
from agent.config_models import MemoryConfig
from memory.consolidator import ConsolidationDraft
from memory.engine import MemoryEngine
from session.manager import SessionManager


class Pipeline:
    async def process(self, message, *, turn_id):
        return PipelineResult("回答", thinking="思考", tool_chain=[{"iteration": 1, "calls": []}], tools_used=["echo"])


class BlockingContextGuard:
    def __init__(self, ready: bool) -> None:
        self.ready = ready
        self.calls: list[str] = []

    async def ensure_context_ready(self, session_key: str) -> bool:
        self.calls.append(session_key)
        return self.ready


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
async def test_agent_loop_injects_user_source_ref_before_pipeline(tmp_path: Path) -> None:
    class CapturingPipeline:
        source_ref = ""

        async def process(self, message, *, turn_id):
            self.source_ref = str(message.metadata.get("current_user_source_ref") or "")
            return PipelineResult("回答")

    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    pipeline = CapturingPipeline()
    loop = AgentLoop(bus, EventBus(), pipeline, sessions)
    await bus.publish_inbound(
        InboundMessage(channel="web", sender="u", chat_id="c", content="记住这句话")
    )

    await loop.run_once()

    rows = sessions.store.fetch_session_messages("web:c")
    assert pipeline.source_ref == rows[0]["id"] == "web:c:0"
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
async def test_context_guard_blocks_pipeline_without_persisting_error_turn(
    tmp_path: Path,
) -> None:
    class ForbiddenPipeline:
        async def process(self, message, *, turn_id):
            raise AssertionError("积压保护失败时不应进入 Pipeline")

    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    guard = BlockingContextGuard(False)
    loop = AgentLoop(
        bus,
        EventBus(),
        ForbiddenPipeline(),
        sessions,
        context_guard=guard,
    )
    await bus.publish_inbound(
        InboundMessage(channel="web", sender="u", chat_id="c", content="问题")
    )

    await loop.run_once()

    outbound = await bus.consume_outbound()
    assert guard.calls == ["web:c"]
    assert "记忆归档" in outbound.content
    assert sessions.store.fetch_session_messages("web:c") == []
    await sessions.close()


@pytest.mark.asyncio
async def test_context_guard_can_be_explicitly_skipped_for_internal_turn(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    guard = BlockingContextGuard(False)
    loop = AgentLoop(bus, EventBus(), Pipeline(), sessions, context_guard=guard)
    await bus.publish_inbound(
        InboundMessage(
            channel="scheduler",
            sender="system",
            chat_id="job",
            content="内部任务",
            metadata={"skip_memory_context_guard": True},
        )
    )

    await loop.run_once()

    outbound = await bus.consume_outbound()
    assert outbound.content == "回答"
    assert guard.calls == []
    await sessions.close()


@pytest.mark.asyncio
async def test_interrupt_defers_persistence_until_next_message_and_preserves_tools(tmp_path: Path) -> None:
    class BlockingPipeline:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def process(self, message, *, turn_id):
            self.started.set()
            await asyncio.Event().wait()

        def snapshot_interrupt_state(self, turn_id: str):
            return {
                "partial_reply": "未完成回答",
                "partial_thinking": "未完成思考",
                "tools_used": ["read_file"],
                "tool_chain_partial": [
                    {
                        "iteration": 1,
                        "calls": [
                            {
                                "call_id": "call-1",
                                "name": "read_file",
                                "arguments": {"path": "a.txt"},
                                "result": "文件内容",
                                "status": "ok",
                            }
                        ],
                    }
                ],
            }

        def discard_interrupt_snapshot(self, turn_id: str) -> None:
            pass

    bus = MessageBus()
    events = EventBus()
    sessions = SessionManager(tmp_path)
    pipeline = BlockingPipeline()
    loop = AgentLoop(bus, events, pipeline, sessions)
    first = InboundMessage(channel="web", sender="u", chat_id="c", content="读取文件")
    await bus.publish_inbound(first)
    running = asyncio.create_task(loop.run_once())
    await pipeline.started.wait()

    result = await loop.request_interrupt("web:c")
    await running

    assert result.status == "interrupted"
    assert sessions.store.fetch_session_messages("web:c") == []
    state = loop.interrupt_states["web:c"]
    assert state.original_user_message == "读取文件"
    assert state.partial_reply == "未完成回答"
    assert state.partial_thinking == "未完成思考"
    assert state.tools_used == ["read_file"]

    loop._pipeline = Pipeline()
    await bus.publish_inbound(
        InboundMessage(channel="web", sender="u", chat_id="c", content="继续")
    )
    await loop.run_once()

    rows = sessions.store.fetch_session_messages("web:c")
    assert [row["content"] for row in rows] == ["读取文件", "[interrupted]", "继续", "回答"]
    assert rows[1]["status"] == "interrupted"
    assert rows[1]["tools_used"] == ["read_file"]
    assert rows[1]["tool_chain"][0]["calls"][0]["result"] == "文件内容"
    assert "web:c" not in loop.interrupt_states
    await sessions.close()


@pytest.mark.asyncio
async def test_expired_interrupt_state_is_discarded_without_marker(tmp_path: Path) -> None:
    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    loop = AgentLoop(bus, EventBus(), Pipeline(), sessions)
    loop.interrupt_states["web:c"] = TurnInterruptState(
        session_key="web:c",
        original_user_message="旧问题",
        interrupted_at=time.monotonic() - 1801,
    )
    await bus.publish_inbound(
        InboundMessage(channel="web", sender="u", chat_id="c", content="新问题")
    )

    await loop.run_once()

    rows = sessions.store.fetch_session_messages("web:c")
    assert [row["content"] for row in rows] == ["新问题", "回答"]
    assert "web:c" not in loop.interrupt_states
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
