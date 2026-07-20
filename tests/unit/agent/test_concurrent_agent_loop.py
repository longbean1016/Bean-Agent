"""AgentLoop 跨会话并发、排队事件和取消语义测试。"""

from __future__ import annotations

import asyncio

import pytest

from agent.agent_loop import AgentLoop
from agent.event_bus import EventBus, TurnQueueRejected, TurnQueued, TurnStarted
from agent.message_bus import InboundMessage, MessageBus, PipelineResult
from session.manager import SessionManager


class _BlockingPipeline:
    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.started: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}

    def started_event(self, chat_id: str) -> asyncio.Event:
        return self.started.setdefault(chat_id, asyncio.Event())

    def release_event(self, chat_id: str) -> asyncio.Event:
        return self.release.setdefault(chat_id, asyncio.Event())

    async def process(self, message, *, turn_id):
        self.started_event(message.chat_id).set()
        event = self.release_event(message.chat_id)
        await event.wait()
        if message.chat_id in self.fail:
            raise RuntimeError("fake failure")
        return PipelineResult(f"回答-{message.chat_id}")


def _message(chat_id: str, request_id: str | None = None) -> InboundMessage:
    return InboundMessage(
        "web",
        "user",
        chat_id,
        f"问题-{chat_id}",
        metadata={"request_id": request_id or f"r-{chat_id}"},
    )


@pytest.mark.asyncio
async def test_agent_loop_runs_different_sessions_concurrently_and_promotes_queue(
    tmp_path,
) -> None:
    bus = MessageBus()
    events = EventBus()
    pipeline = _BlockingPipeline()
    sessions = SessionManager(tmp_path)
    queued: list[TurnQueued] = []
    started: list[TurnStarted] = []
    events.on(TurnQueued, queued.append)
    events.on(TurnStarted, started.append)
    loop = AgentLoop(
        bus,
        events,
        pipeline,
        sessions,
        max_concurrent_turns=2,
        max_queued_turns=2,
    )
    runner = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(_message("a"))
        await bus.publish_inbound(_message("b"))
        await asyncio.wait_for(pipeline.started_event("a").wait(), timeout=1)
        await asyncio.wait_for(pipeline.started_event("b").wait(), timeout=1)
        await bus.publish_inbound(_message("c"))
        await asyncio.sleep(0)

        assert queued[-1].session_key == "web:c"
        assert queued[-1].position == 1
        assert {event.session_key for event in started} == {"web:a", "web:b"}

        pipeline.release_event("a").set()
        await asyncio.wait_for(pipeline.started_event("c").wait(), timeout=1)
        assert {event.session_key for event in started} == {"web:a", "web:b", "web:c"}
    finally:
        for event in pipeline.release.values():
            event.set()
        await loop.close()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await sessions.close()


@pytest.mark.asyncio
async def test_agent_loop_rejects_duplicate_and_full_queue_and_cancels_waiting_turn(
    tmp_path,
) -> None:
    bus = MessageBus()
    events = EventBus()
    pipeline = _BlockingPipeline()
    sessions = SessionManager(tmp_path)
    rejected: list[TurnQueueRejected] = []
    queued: list[TurnQueued] = []
    events.on(TurnQueueRejected, rejected.append)
    events.on(TurnQueued, queued.append)
    loop = AgentLoop(
        bus,
        events,
        pipeline,
        sessions,
        max_concurrent_turns=1,
        max_queued_turns=1,
    )
    runner = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(_message("a"))
        await asyncio.wait_for(pipeline.started_event("a").wait(), timeout=1)
        await bus.publish_inbound(_message("b"))
        await bus.publish_inbound(_message("c"))
        await bus.publish_inbound(_message("a", "r-duplicate"))
        await asyncio.sleep(0)

        assert [(item.session_key, item.reason) for item in rejected] == [
            ("web:c", "queue_full"),
            ("web:a", "session_busy"),
        ]
        result = await loop.request_interrupt("web:b")
        assert result.status == "cancelled"
        assert not loop.is_session_busy("web:b")
    finally:
        for event in pipeline.release.values():
            event.set()
        await loop.close()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await sessions.close()


@pytest.mark.asyncio
async def test_agent_loop_failure_releases_slot_for_next_session(tmp_path) -> None:
    bus = MessageBus()
    pipeline = _BlockingPipeline(fail={"a"})
    sessions = SessionManager(tmp_path)
    loop = AgentLoop(
        bus,
        EventBus(),
        pipeline,
        sessions,
        max_concurrent_turns=1,
        max_queued_turns=1,
    )
    runner = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(_message("a"))
        await asyncio.wait_for(pipeline.started_event("a").wait(), timeout=1)
        await bus.publish_inbound(_message("b"))
        pipeline.release_event("a").set()
        await asyncio.wait_for(pipeline.started_event("b").wait(), timeout=1)
    finally:
        for event in pipeline.release.values():
            event.set()
        await loop.close()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await sessions.close()


@pytest.mark.asyncio
async def test_interrupting_running_session_does_not_cancel_other_session(tmp_path) -> None:
    bus = MessageBus()
    pipeline = _BlockingPipeline()
    sessions = SessionManager(tmp_path)
    loop = AgentLoop(
        bus,
        EventBus(),
        pipeline,
        sessions,
        max_concurrent_turns=2,
        max_queued_turns=1,
    )
    runner = asyncio.create_task(loop.run())
    try:
        await bus.publish_inbound(_message("a"))
        await bus.publish_inbound(_message("b"))
        await asyncio.wait_for(pipeline.started_event("a").wait(), timeout=1)
        await asyncio.wait_for(pipeline.started_event("b").wait(), timeout=1)

        result = await loop.request_interrupt("web:a")
        await asyncio.sleep(0)

        assert result.status == "interrupted"
        assert loop.is_session_busy("web:b")
        pipeline.release_event("b").set()
    finally:
        for event in pipeline.release.values():
            event.set()
        await loop.close()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await sessions.close()
