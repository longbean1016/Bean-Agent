"""WebChannel 附件路径边界测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.agent_loop import InterruptResult
from agent.channel import WebChannel
from agent.event_bus import ContextUsageUpdated, EventBus, TurnQueued, TurnQueueRejected
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


class Approvals:
    def __init__(self) -> None:
        self.available: list[tuple[str, bool]] = []
        self.decisions: list[tuple[str, str, str]] = []

    async def set_session_available(self, session_id: str, available: bool) -> None:
        self.available.append((session_id, available))

    async def pending_for_session(self, session_id: str) -> list[object]:
        if session_id != "web:chat":
            return []
        return [SimpleNamespace(to_wire=lambda: {
            "id": "approval-1",
            "session_id": session_id,
            "state": "pending",
        })]

    async def decide(self, request_id: str, session_id: str, decision: str) -> str:
        self.decisions.append((request_id, session_id, decision))
        return decision

    async def cancel_session(self, _session_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_message_send_does_not_publish_session_title_before_turn_runs() -> None:
    bus = MessageBus()
    channel = WebChannel(bus, EventBus(), Interrupt())
    socket = Socket()

    await channel.handle_frame(socket, {
        "type": "message.send",
        "request_id": "r-title",
        "session_id": "web:chat",
        "text": "first question title",
        "media": [],
    })

    assert socket.frames == []
    assert not bus._inbound.empty()
    await channel.close()


@pytest.mark.asyncio
async def test_message_send_without_session_creates_session_after_validation() -> None:
    bus = MessageBus()
    created: list[str] = []

    async def ensure_session(session_key: str) -> object:
        created.append(session_key)
        return object()

    channel = WebChannel(bus, EventBus(), Interrupt(), ensure_session=ensure_session)
    socket = Socket()

    await channel.handle_frame(socket, {
        "type": "message.send",
        "request_id": "r-empty",
        "text": "   ",
        "media": [],
    })

    assert created == []
    assert socket.frames[-1]["code"] == "empty_message"
    assert bus._inbound.empty()

    await channel.handle_frame(socket, {
        "type": "message.send",
        "request_id": "r-new",
        "text": "first question",
        "media": [],
    })

    assert len(created) == 1
    assert created[0].startswith("web:")
    assert socket.frames[-1] == {
        "type": "session.created",
        "request_id": "r-new",
        "session_id": created[0],
    }
    assert not bus._inbound.empty()
    await channel.close()


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
async def test_web_channel_handles_sandbox_workspace_and_approval_protocol() -> None:
    bus = MessageBus()
    approvals = Approvals()
    mode_updates: list[tuple[str, str]] = []
    workspace_updates: list[tuple[str, str | None]] = []

    async def load_sandbox(session_id: str) -> dict:
        return {
            "session_id": session_id,
            "workspace_id": "workspace-1",
            "workspace_path": "D:/project",
            "sandbox_mode": "workspace-write",
            "workspace_valid": True,
        }

    async def set_mode(session_id: str, mode: str) -> dict:
        mode_updates.append((session_id, mode))
        return {**await load_sandbox(session_id), "sandbox_mode": mode}

    async def set_workspace(session_id: str, workspace_id: str | None) -> dict:
        workspace_updates.append((session_id, workspace_id))
        return {**await load_sandbox(session_id), "workspace_id": workspace_id}

    channel = WebChannel(
        bus,
        EventBus(),
        Interrupt(),
        sandbox_loader=load_sandbox,
        sandbox_mode_writer=set_mode,
        workspace_writer=set_workspace,
        approvals=approvals,  # type: ignore[arg-type]
    )
    socket = Socket()

    await channel.handle_frame(socket, {
        "type": "session.subscribe",
        "request_id": "subscribe",
        "session_id": "web:chat",
    })

    assert [frame["type"] for frame in socket.frames[:3]] == [
        "session.subscribed",
        "sandbox.updated",
        "approval.requested",
    ]
    assert approvals.available[-1] == ("web:chat", True)

    await channel.handle_frame(socket, {
        "type": "sandbox.mode.set",
        "request_id": "mode-denied",
        "session_id": "web:chat",
        "sandbox_mode": "danger-full-access",
    })
    assert socket.frames[-1]["code"] == "invalid_sandbox"

    await channel.handle_frame(socket, {
        "type": "sandbox.mode.set",
        "request_id": "mode-ok",
        "session_id": "web:chat",
        "sandbox_mode": "danger-full-access",
        "risk_confirmed": True,
    })
    assert mode_updates == [("web:chat", "danger-full-access")]
    assert socket.frames[-1]["sandbox"]["sandbox_mode"] == "danger-full-access"

    await channel.handle_frame(socket, {
        "type": "workspace.bind",
        "request_id": "workspace",
        "session_id": "web:chat",
        "workspace_id": "workspace-2",
    })
    assert workspace_updates == [("web:chat", "workspace-2")]

    await channel.handle_frame(socket, {
        "type": "approval.decide",
        "request_id": "approval",
        "session_id": "web:chat",
        "approval_id": "approval-1",
        "decision": "allowed-once",
    })
    assert approvals.decisions == [
        ("approval-1", "web:chat", "allowed-once")
    ]
    assert socket.frames[-1]["type"] == "approval.resolved"
    await channel.close()


@pytest.mark.asyncio
async def test_session_subscribe_restores_context_usage_snapshot() -> None:
    bus = MessageBus()

    async def load_context_usage(session_key: str) -> dict:
        assert session_key == "web:chat"
        return {
            "pressure_tokens": 1200,
            "projected_tokens": 1400,
            "context_window": 10000,
            "soft_limit_tokens": 8000,
            "hard_input_tokens": 9800,
            "context_window_source": "provider_catalog",
            "system_tokens": 100,
            "tools_tokens": 200,
            "message_tokens": 900,
            "model_runtime_id": "provider:model",
            "model": "model",
        }

    channel = WebChannel(
        bus,
        EventBus(),
        Interrupt(),
        context_usage_loader=load_context_usage,
    )
    socket = Socket()

    await channel.handle_frame(socket, {
        "type": "session.subscribe",
        "request_id": "subscribe",
        "session_id": "web:chat",
    })

    assert socket.frames[1] == {
        "type": "context.usage.updated",
        "session_id": "web:chat",
        "turn_id": "",
        "used_tokens": 1400,
        "context_window": 10000,
        "soft_limit_tokens": 8000,
        "hard_input_tokens": 9800,
        "context_window_source": "provider_catalog",
        "estimate_source": "provider_projected",
        "breakdown": {
            "system_prompt_tokens": 100,
            "tools_tokens": 200,
            "conversation_tokens": 900,
            "overhead_tokens": 0,
        },
        "sections": [],
        "pressure_tokens": 1200,
        "projected_tokens": 1400,
        "system_tokens": 100,
        "tools_tokens": 200,
        "message_tokens": 900,
        "model_runtime_id": "provider:model",
        "model": "model",
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


@pytest.mark.asyncio
async def test_web_channel_maps_context_usage_event_to_websocket_frame() -> None:
    bus = MessageBus()
    events = EventBus()
    channel = WebChannel(bus, events, Interrupt())
    socket = Socket()
    await channel.handle_frame(socket, {
        "type": "session.subscribe",
        "request_id": "subscribe",
        "session_id": "web:chat",
    })

    await events.emit(ContextUsageUpdated(
        session_key="web:chat",
        turn_id="turn-1",
        used_tokens=65500,
        context_window=1_000_000,
        soft_limit_tokens=740_000,
        hard_input_tokens=991_808,
        context_window_source="provider_catalog",
        estimate_source="heuristic",
        breakdown={
            "system_prompt_tokens": 1600,
            "tools_tokens": 6900,
            "conversation_tokens": 49700,
            "overhead_tokens": 7300,
        },
        sections=({"name": "identity", "estimated_tokens": 120, "static": True, "cache_hit": True},),
    ))

    assert socket.frames[-1] == {
        "type": "context.usage.updated",
        "session_id": "web:chat",
        "turn_id": "turn-1",
        "used_tokens": 65500,
        "context_window": 1_000_000,
        "soft_limit_tokens": 740_000,
        "hard_input_tokens": 991_808,
        "context_window_source": "provider_catalog",
        "estimate_source": "heuristic",
        "breakdown": {
            "system_prompt_tokens": 1600,
            "tools_tokens": 6900,
            "conversation_tokens": 49700,
            "overhead_tokens": 7300,
        },
        "sections": [{"name": "identity", "estimated_tokens": 120, "static": True, "cache_hit": True}],
    }
    await channel.close()
