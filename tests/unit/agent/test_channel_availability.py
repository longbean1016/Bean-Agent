"""WebChannel 连接生命周期与审批可用性竞态测试。"""

from __future__ import annotations

import asyncio

import pytest

from agent.agent_loop import InterruptResult
from agent.channel import WebChannel
from agent.event_bus import EventBus
from agent.message_bus import MessageBus


class Interrupt:
    async def request_interrupt(self, _session_key: str) -> InterruptResult:
        return InterruptResult("idle", "")


class Socket:
    async def accept(self) -> None:
        return None

    async def receive_json(self) -> dict[str, object]:
        raise RuntimeError("closed")

    async def send_json(self, _payload: dict[str, object]) -> None:
        return None


def _availability_recorder(channel: WebChannel) -> list[tuple[str, bool]]:
    calls: list[tuple[str, bool]] = []

    async def set_session_available(session_key: str, available: bool) -> None:
        calls.append((session_key, available))

    channel._commands.set_session_available = set_session_available  # type: ignore[method-assign]
    return calls


@pytest.mark.asyncio
async def test_old_unregister_cannot_make_new_connection_unavailable() -> None:
    channel = WebChannel(MessageBus(), EventBus(), Interrupt())
    calls = _availability_recorder(channel)
    old_socket = Socket()
    new_socket = Socket()

    await channel._register("web:chat", old_socket)
    assert calls == [("web:chat", True)]

    false_started = asyncio.Event()
    release_false = asyncio.Event()

    async def delayed_set_session_available(session_key: str, available: bool) -> None:
        calls.append((session_key, available))
        if not available:
            false_started.set()
            await release_false.wait()

    channel._commands.set_session_available = delayed_set_session_available  # type: ignore[method-assign]
    unregister_task = asyncio.create_task(channel._unregister(old_socket))
    await false_started.wait()

    # 新连接已进入集合，但旧 unregister 尚未完成 coordinator 更新。
    register_task = asyncio.create_task(channel._register("web:chat", new_socket))
    await asyncio.sleep(0)
    release_false.set()
    await unregister_task
    await register_task

    assert channel._connections["web:chat"] == {new_socket}
    assert calls[-1] == ("web:chat", True)
    await channel.close()


@pytest.mark.asyncio
async def test_only_last_connection_marks_session_unavailable() -> None:
    channel = WebChannel(MessageBus(), EventBus(), Interrupt())
    calls = _availability_recorder(channel)
    first = Socket()
    second = Socket()

    await channel._register("web:chat", first)
    await channel._register("web:chat", second)
    await channel._unregister(first)
    assert ("web:chat", False) not in calls

    await channel._unregister(second)
    assert calls[-1] == ("web:chat", False)
    await channel.close()


@pytest.mark.asyncio
async def test_close_synchronizes_all_connected_sessions() -> None:
    channel = WebChannel(MessageBus(), EventBus(), Interrupt())
    calls = _availability_recorder(channel)
    await channel._register("web:a", Socket())
    await channel._register("web:b", Socket())

    await channel.close()

    assert {item for item in calls if item[1] is False} == {
        ("web:a", False),
        ("web:b", False),
    }
