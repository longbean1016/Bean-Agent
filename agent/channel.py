"""WebSocket 帧与 BeanAgent 消息/事件总线之间的 Web 通道。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol
from uuid import uuid4

from agent.agent_loop import InterruptResult
from agent.event_bus import EventBus, StreamDeltaReady, ToolCallCompleted, ToolCallStarted, TurnStarted
from agent.message_bus import InboundMessage, MessageBus, OutboundMessage

logger = logging.getLogger(__name__)


class WebSocketApi(Protocol):
    async def accept(self) -> None: ...
    async def receive_json(self) -> dict[str, Any]: ...
    async def send_json(self, data: dict[str, Any]) -> None: ...


class InterruptController(Protocol):
    def request_interrupt(self, session_key: str) -> InterruptResult: ...


class WebChannel:
    name = "web"

    def __init__(self, bus: MessageBus, event_bus: EventBus, interrupt_controller: InterruptController) -> None:
        self._bus = bus
        self._events = event_bus
        self._interrupt = interrupt_controller
        self._connections: dict[str, set[WebSocketApi]] = {}
        self._lock = asyncio.Lock()
        bus.subscribe_outbound(self.name, self._on_response)
        event_bus.on(TurnStarted, self._on_turn_started)
        event_bus.on(StreamDeltaReady, self._on_stream_delta)
        event_bus.on(ToolCallStarted, self._on_tool_started)
        event_bus.on(ToolCallCompleted, self._on_tool_completed)

    async def handle_websocket(self, websocket: WebSocketApi) -> None:
        await websocket.accept()
        try:
            while True:
                await self.handle_frame(websocket, await websocket.receive_json())
        except Exception:
            # FastAPI/Starlette 的 WebSocketDisconnect 以及测试替身的流结束都在这里
            # 统一清理；协议错误由 handle_frame 自己返回结构化 error，不会走此分支。
            pass
        finally:
            await self._unregister(websocket)

    async def handle_frame(self, websocket: WebSocketApi, frame: dict[str, Any]) -> None:
        frame_type = str(frame.get("type") or "")
        request_id = str(frame.get("request_id") or "")
        if frame_type == "ping":
            await websocket.send_json({"type": "pong", "request_id": request_id})
            return
        if frame_type == "session.create":
            session_key = f"web:{uuid4().hex}"
            await self._register(session_key, websocket)
            await websocket.send_json({"type": "session.created", "request_id": request_id, "session_id": session_key})
            return
        session_key = normalize_web_session_id(frame.get("session_id"))
        if frame_type == "message.send":
            if session_key is None:
                session_key = f"web:{uuid4().hex}"
                await websocket.send_json({"type": "session.created", "request_id": request_id, "session_id": session_key})
            text = str(frame.get("text") or "")
            media = frame.get("media") if isinstance(frame.get("media"), list) else []
            if not text.strip() and not media:
                await self._error(websocket, request_id, "empty_message", "消息不能为空")
                return
            if len(text) > 32 * 1024 or len(media) > 8:
                await self._error(websocket, request_id, "message_too_large", "消息或媒体数量超过限制")
                return
            await self._register(session_key, websocket)
            chat_id = session_key.split(":", 1)[1]
            await self._bus.publish_inbound(InboundMessage(self.name, "web", chat_id, text, list(media), {"request_id": request_id}))
            return
        if frame_type == "turn.stop" and session_key is not None:
            result = self._interrupt.request_interrupt(session_key)
            await websocket.send_json({"type": "turn.interrupted", "request_id": request_id, "session_id": session_key, "turn_id": result.turn_id, "status": result.status})
            return
        await self._error(websocket, request_id, "invalid_frame", f"不支持的帧类型: {frame_type or 'empty'}")

    async def _on_response(self, message: OutboundMessage) -> None:
        session_key = f"{message.channel}:{message.chat_id}"
        await self._broadcast(session_key, {
            "type": "message.final", "request_id": str(message.metadata.get("request_id") or ""),
            "session_id": session_key, "turn_id": str(message.metadata.get("turn_id") or ""),
            "content": message.content, "thinking": message.thinking, "media": list(message.media),
        })

    async def _on_turn_started(self, event: TurnStarted) -> None:
        await self._broadcast(event.session_key, {"type": "turn.started", "request_id": event.request_id, "session_id": event.session_key, "turn_id": event.turn_id})

    async def _on_stream_delta(self, event: StreamDeltaReady) -> None:
        if event.content_delta:
            await self._broadcast(event.session_key, {"type": "answer.delta", "session_id": event.session_key, "turn_id": event.turn_id, "delta": event.content_delta})
        if event.thinking_delta:
            await self._broadcast(event.session_key, {"type": "react.thinking.delta", "session_id": event.session_key, "turn_id": event.turn_id, "delta": event.thinking_delta})

    async def _on_tool_started(self, event: ToolCallStarted) -> None:
        await self._broadcast(event.session_key, {"type": "react.tool.started", "session_id": event.session_key, "turn_id": event.turn_id, "call_id": event.call_id, "tool_name": event.tool_name, "arguments": event.arguments})

    async def _on_tool_completed(self, event: ToolCallCompleted) -> None:
        await self._broadcast(event.session_key, {"type": "react.tool.completed", "session_id": event.session_key, "turn_id": event.turn_id, "call_id": event.call_id, "tool_name": event.tool_name, "status": event.status, "result_preview": event.result_preview})

    async def _register(self, session_key: str, websocket: WebSocketApi) -> None:
        async with self._lock:
            self._connections.setdefault(session_key, set()).add(websocket)

    async def _unregister(self, websocket: WebSocketApi) -> None:
        async with self._lock:
            for key in list(self._connections):
                self._connections[key].discard(websocket)
                if not self._connections[key]:
                    self._connections.pop(key, None)

    async def _broadcast(self, session_key: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._connections.get(session_key, ()))
        failed: list[WebSocketApi] = []
        for websocket in targets:
            try:
                await websocket.send_json(payload)
            except Exception:
                failed.append(websocket)
        for websocket in failed:
            await self._unregister(websocket)

    @staticmethod
    async def _error(websocket: WebSocketApi, request_id: str, code: str, message: str) -> None:
        await websocket.send_json({"type": "error", "request_id": request_id, "code": code, "message": message})

    async def close(self) -> None:
        self._bus.unsubscribe_outbound(self.name, self._on_response)
        self._events.off(TurnStarted, self._on_turn_started)
        self._events.off(StreamDeltaReady, self._on_stream_delta)
        self._events.off(ToolCallStarted, self._on_tool_started)
        self._events.off(ToolCallCompleted, self._on_tool_completed)
        async with self._lock:
            self._connections.clear()


def normalize_web_session_id(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("web:") and text[4:]:
        return text
    if ":" not in text:
        return f"web:{text}"
    return None


__all__ = ["WebChannel", "normalize_web_session_id"]
