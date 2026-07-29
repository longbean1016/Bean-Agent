"""WebChannel 附件路径边界测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.agent_loop import InterruptResult
from agent.channel import WebChannel
from agent.event_bus import EventBus, TurnQueued, TurnQueueRejected
from agent.message_bus import MessageBus


class Interrupt:
    async def request_interrupt(self, session_key: str) -> InterruptResult:
        return InterruptResult("idle", "")


class SnapshotInterrupt(Interrupt):
    def get_active_turn_snapshot(self, session_key: str) -> dict | None:
        if session_key != "web:chat":
            return None
        return {
            "session_id": "web:chat",
            "turn_id": "turn-1",
            "request_id": "r1",
            "user_message": "read it",
            "user_media": [],
            "content": "partial",
            "thinking": "thinking",
            "tools": [],
            "status": "running",
        }


class Socket:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def accept(self) -> None:
        return None

    async def receive_json(self) -> dict:
        raise RuntimeError("done")

    async def send_json(self, data: dict) -> None:
        self.frames.append(data)


@pytest.mark.asyncio
async def test_web_channel_rejects_media_outside_upload_root(tmp_path: Path) -> None:
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("不允许读取", encoding="utf-8")
    bus = MessageBus()
    channel = WebChannel(bus, EventBus(), Interrupt(), media_root=upload_root)
    socket = Socket()

    await channel.handle_frame(socket, {
        "type": "message.send",
        "request_id": "r-media",
        "session_id": "web:chat",
        "text": "读取附件",
        "media": [str(outside)],
    })

    assert socket.frames[-1]["code"] == "invalid_media"
    assert bus._inbound.empty()
    await channel.close()


@pytest.mark.asyncio
async def test_session_subscribe_replays_active_turn_snapshot() -> None:
    bus = MessageBus()
    channel = WebChannel(bus, EventBus(), SnapshotInterrupt())
    socket = Socket()

    await channel.handle_frame(socket, {
        "type": "session.subscribe",
        "request_id": "subscribe",
        "session_id": "web:chat",
    })

    assert socket.frames[0] == {
        "type": "session.subscribed",
        "request_id": "subscribe",
        "session_id": "web:chat",
    }
    assert socket.frames[1] == {
        "type": "turn.snapshot",
        "session_id": "web:chat",
        "turn_id": "turn-1",
        "request_id": "r1",
        "user_message": "read it",
        "user_media": [],
        "content": "partial",
        "thinking": "thinking",
        "tools": [],
        "status": "running",
    }
    await channel.close()


@pytest.mark.asyncio
async def test_web_channel_maps_turn_queue_events_to_websocket_frames() -> None:
    bus = MessageBus()
    events = EventBus()
    channel = WebChannel(bus, events, Interrupt())
    socket = Socket()
    await channel.handle_frame(socket, {
        "type": "session.subscribe",
        "request_id": "subscribe",
        "session_id": "web:chat",
    })

    await events.emit(TurnQueued("web:chat", "r-queued", 2))
    await events.emit(TurnQueueRejected("web:chat", "r-full", "queue_full"))

    assert socket.frames[-2] == {
        "type": "turn.queued",
        "request_id": "r-queued",
        "session_id": "web:chat",
        "position": 2,
    }
    assert socket.frames[-1]["type"] == "error"
    assert socket.frames[-1]["code"] == "queue_full"
    assert socket.frames[-1]["message"] == "当前任务较多，请稍后再试"
    await channel.close()
