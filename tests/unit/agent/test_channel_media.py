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
