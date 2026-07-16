"""EventBus 的有序派发、异常隔离和生命周期事件测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agent.event_bus import EventBus, StreamDeltaReady, TurnCommitted


@pytest.mark.asyncio
async def test_emit_runs_sync_and_async_handlers_in_registration_order() -> None:
    bus = EventBus()
    calls: list[str] = []

    def first(event: StreamDeltaReady) -> None:
        calls.append(f"first:{event.content_delta}")

    async def second(event: StreamDeltaReady) -> None:
        calls.append(f"second:{event.content_delta}")

    bus.on(StreamDeltaReady, first)
    bus.on(StreamDeltaReady, second)

    await bus.emit(StreamDeltaReady(session_key="web:c", turn_id="t1", content_delta="A"))

    assert calls == ["first:A", "second:A"]


@pytest.mark.asyncio
async def test_handler_failure_isolated_and_off_is_idempotent(caplog) -> None:
    bus = EventBus()
    calls: list[str] = []

    def broken(_event: StreamDeltaReady) -> None:
        raise RuntimeError("observer failed")

    def healthy(_event: StreamDeltaReady) -> None:
        calls.append("healthy")

    bus.on(StreamDeltaReady, broken)
    bus.on(StreamDeltaReady, healthy)
    await bus.emit(StreamDeltaReady(session_key="web:c", turn_id="t1"))
    bus.off(StreamDeltaReady, healthy)
    bus.off(StreamDeltaReady, healthy)
    await bus.emit(StreamDeltaReady(session_key="web:c", turn_id="t2"))

    assert calls == ["healthy"]
    assert "observer failed" in caplog.text


def test_lifecycle_events_are_immutable() -> None:
    event = TurnCommitted(
        session_key="web:c",
        turn_id="t1",
        user_message_id="web:c:0",
        assistant_message_id="web:c:1",
        status="ok",
    )

    with pytest.raises(FrozenInstanceError):
        event.status = "error"
