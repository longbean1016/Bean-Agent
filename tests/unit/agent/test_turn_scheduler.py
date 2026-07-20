"""多会话 TurnScheduler 的准入、FIFO、取消和异常释放测试。"""

from __future__ import annotations

import asyncio

import pytest

from agent.message_bus import InboundMessage
from agent.turn_scheduler import TurnScheduler


def _message(chat_id: str) -> InboundMessage:
    return InboundMessage("web", "user", chat_id, f"问题-{chat_id}")


@pytest.mark.asyncio
async def test_scheduler_starts_five_and_queues_following_sessions_fifo() -> None:
    releases = {chat_id: asyncio.Event() for chat_id in "abcdefg"}
    started: list[str] = []

    async def start(message: InboundMessage) -> None:
        started.append(message.chat_id)
        await releases[message.chat_id].wait()

    scheduler = TurnScheduler(max_running=5, max_queued=20, start_turn=start)
    results = [await scheduler.submit(_message(chat_id)) for chat_id in "abcdefg"]
    await asyncio.sleep(0)

    assert [item.status for item in results] == ["started"] * 5 + ["queued"] * 2
    assert [results[5].position, results[6].position] == [1, 2]
    assert set(started) == set("abcde")

    releases["a"].set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert "f" in started
    assert "g" not in started

    for event in releases.values():
        event.set()
    await scheduler.close()


@pytest.mark.asyncio
async def test_scheduler_rejects_duplicate_session_and_full_queue() -> None:
    release = asyncio.Event()

    async def start(_message: InboundMessage) -> None:
        await release.wait()

    scheduler = TurnScheduler(max_running=1, max_queued=2, start_turn=start)
    first = await scheduler.submit(_message("a"))
    duplicate = await scheduler.submit(_message("a"))
    queued_b = await scheduler.submit(_message("b"))
    queued_c = await scheduler.submit(_message("c"))
    full = await scheduler.submit(_message("d"))

    assert first.status == "started"
    assert duplicate.status == "rejected" and duplicate.reason == "session_busy"
    assert [queued_b.position, queued_c.position] == [1, 2]
    assert full.status == "rejected" and full.reason == "queue_full"

    release.set()
    await scheduler.close()


@pytest.mark.asyncio
async def test_scheduler_cancelled_queue_item_updates_positions() -> None:
    release = asyncio.Event()
    updates: list[list[tuple[str, int]]] = []
    cancelled: list[str] = []

    async def start(_message: InboundMessage) -> None:
        await release.wait()

    async def positions(items) -> None:
        updates.append([(item.message.chat_id, item.position) for item in items])

    async def queued_cancelled(message: InboundMessage) -> None:
        cancelled.append(message.chat_id)

    scheduler = TurnScheduler(
        max_running=1,
        max_queued=3,
        start_turn=start,
        on_queue_positions=positions,
        on_queued_cancelled=queued_cancelled,
    )
    await scheduler.submit(_message("a"))
    await scheduler.submit(_message("b"))
    await scheduler.submit(_message("c"))

    result = await scheduler.cancel("web:b")

    assert result.status == "cancelled"
    assert cancelled == ["b"]
    assert updates[-1] == [("c", 1)]
    release.set()
    await scheduler.close()


@pytest.mark.asyncio
async def test_scheduler_failure_releases_slot_and_starts_next() -> None:
    next_started = asyncio.Event()

    async def start(message: InboundMessage) -> None:
        if message.chat_id == "a":
            raise RuntimeError("失败")
        next_started.set()

    scheduler = TurnScheduler(max_running=1, max_queued=1, start_turn=start)
    await scheduler.submit(_message("a"))
    await scheduler.submit(_message("b"))

    await asyncio.wait_for(next_started.wait(), timeout=1)

    assert not scheduler.is_busy("a")
    await scheduler.close()
