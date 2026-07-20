"""ChatLane 的同会话顺序、跨会话隔离和取消恢复测试。"""

from __future__ import annotations

import asyncio

import pytest

from agent.chat_lane import ChatLaneManager


@pytest.mark.asyncio
async def test_non_passive_waits_for_passive_turn_and_final_send() -> None:
    lane = ChatLaneManager()
    order: list[str] = []
    await lane.mark_passive_pending("web", "a")

    non_passive = asyncio.create_task(
        lane.run_non_passive("web", "a", lambda: _append(order, "notification"))
    )
    await asyncio.sleep(0)
    assert order == []

    await lane.mark_passive_send_pending("web", "a")
    await lane.run_passive("web", "a", lambda: _append(order, "reply"))
    assert order == ["reply"]
    await lane.mark_passive_done("web", "a")
    await non_passive

    assert order == ["reply", "notification"]


@pytest.mark.asyncio
async def test_different_sessions_do_not_block_each_other() -> None:
    lane = ChatLaneManager()
    sent: list[str] = []
    await lane.mark_passive_pending("web", "a")

    await lane.run_non_passive("web", "b", lambda: _append(sent, "session-b"))

    assert sent == ["session-b"]
    await lane.mark_passive_done("web", "a")


@pytest.mark.asyncio
async def test_cancelled_non_passive_ticket_does_not_block_next_sender() -> None:
    lane = ChatLaneManager()
    sent: list[str] = []
    await lane.mark_passive_pending("web", "a")
    cancelled = asyncio.create_task(
        lane.run_non_passive("web", "a", lambda: _append(sent, "cancelled"))
    )
    await asyncio.sleep(0)
    following = asyncio.create_task(
        lane.run_non_passive("web", "a", lambda: _append(sent, "following"))
    )
    await asyncio.sleep(0)

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    await lane.mark_passive_done("web", "a")
    await following

    assert sent == ["following"]


@pytest.mark.asyncio
async def test_cancelled_waiting_passive_send_releases_pending_count() -> None:
    lane = ChatLaneManager()
    blocker_started = asyncio.Event()
    release_blocker = asyncio.Event()

    async def block() -> None:
        blocker_started.set()
        await release_blocker.wait()

    blocker = asyncio.create_task(lane.run_non_passive("web", "a", block))
    await blocker_started.wait()
    await lane.mark_passive_send_pending("web", "a")
    waiting = asyncio.create_task(
        lane.run_passive("web", "a", lambda: _append([], "never"))
    )
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release_blocker.set()
    await blocker

    sent: list[str] = []
    await asyncio.wait_for(
        lane.run_non_passive("web", "a", lambda: _append(sent, "recovered")),
        timeout=0.1,
    )
    assert sent == ["recovered"]


async def _append(target: list[str], value: str) -> None:
    target.append(value)
