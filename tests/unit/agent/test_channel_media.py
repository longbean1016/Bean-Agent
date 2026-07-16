"""WebChannel 附件路径边界测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.agent_loop import InterruptResult
from agent.channel import WebChannel
from agent.event_bus import EventBus
from agent.message_bus import MessageBus


class Interrupt:
    def request_interrupt(self, session_key: str) -> InterruptResult:
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
