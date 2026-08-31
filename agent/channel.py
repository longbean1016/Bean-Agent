"""WebSocket 帧与 BeanAgent 消息/事件总线之间的 Web 通道。"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from agent.agent_loop import InterruptResult
from agent.event_bus import (
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ContextCompactionStarted,
    ContextUsageUpdated,
    EventBus,
    SessionUsageUpdated,
    SessionUpdated,
    StreamDeltaReady,
    ToolCallCompleted,
    ToolCallStarted,
    TurnQueued,
    TurnQueueRejected,
    TurnStarted,
)
from agent.message_bus import InboundMessage, MessageBus, OutboundMessage
from proactive.notification_service import notification_metadata
from proactive.store import ProactiveStore

logger = logging.getLogger(__name__)


class WebSocketApi(Protocol):
    async def accept(self) -> None: ...
    async def receive_json(self) -> dict[str, Any]: ...
    async def send_json(self, data: dict[str, Any]) -> None: ...


class InterruptController(Protocol):
    async def request_interrupt(self, session_key: str) -> InterruptResult: ...


class WebChannel:
    name = "web"

    def __init__(
        self,
        bus: MessageBus,
        event_bus: EventBus,
        interrupt_controller: InterruptController,
        *,
        media_root: Path | None = None,
        proactive_store: ProactiveStore | None = None,
        ensure_session: Callable[[str], Awaitable[object]] | None = None,
        context_usage_loader: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
        session_usage_loader: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
        context_runtime_id: str = "",
    ) -> None:
        self._bus = bus
        self._events = event_bus
        self._interrupt = interrupt_controller
        self._media_root = media_root.resolve() if media_root is not None else None
        self._proactive_store = proactive_store
        self._ensure_session = ensure_session
        self._context_usage_loader = context_usage_loader
        self._session_usage_loader = session_usage_loader
        self._context_runtime_id = str(context_runtime_id or "")
        self._connections: dict[str, set[WebSocketApi]] = {}
        self._socket_send_locks: weakref.WeakKeyDictionary[WebSocketApi, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )
        self._lock = asyncio.Lock()
        bus.subscribe_outbound(self.name, self._on_response)
        event_bus.on(TurnStarted, self._on_turn_started)
        event_bus.on(ContextCompactionStarted, self._on_context_compaction_started)
        event_bus.on(ContextCompactionCompleted, self._on_context_compaction_completed)
        event_bus.on(ContextCompactionFailed, self._on_context_compaction_failed)
        event_bus.on(ContextUsageUpdated, self._on_context_usage_updated)
        event_bus.on(SessionUsageUpdated, self._on_session_usage_updated)
        event_bus.on(SessionUpdated, self._on_session_updated)
        event_bus.on(TurnQueued, self._on_turn_queued)
        event_bus.on(TurnQueueRejected, self._on_turn_queue_rejected)
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
            await self._send_json(websocket, {"type": "pong", "request_id": request_id})
            return
        if frame_type == "session.create":
            session_key = await self._create_session()
            await self._register(session_key, websocket)
            await self._send_json(websocket, {"type": "session.created", "request_id": request_id, "session_id": session_key})
            return
        session_key = normalize_web_session_id(frame.get("session_id"))
        if frame_type == "session.subscribe" and session_key is not None:
            await self._register(session_key, websocket)
            await self._send_json(websocket, {
                "type": "session.subscribed",
                "request_id": request_id,
                "session_id": session_key,
            })
            await self._send_context_usage_snapshot(session_key, websocket)
            await self._send_session_usage_snapshot(session_key, websocket)
            # 订阅确认后只给当前连接补发运行中快照，用于刷新/重连恢复流式草稿。
            await self._send_active_turn_snapshot(session_key, websocket)
            await self._replay_pending_notifications(session_key, websocket)
            return
        if frame_type == "message.send":
            text = str(frame.get("text") or "")
            media = [str(item).strip() for item in frame.get("media", []) if isinstance(item, str) and str(item).strip()] if isinstance(frame.get("media"), list) else []
            if not text.strip() and not media:
                await self._error(websocket, request_id, "empty_message", "消息不能为空")
                return
            if len(text) > 32 * 1024 or len(media) > 8:
                await self._error(websocket, request_id, "message_too_large", "消息或媒体数量超过限制")
                return
            if not self._media_is_allowed(media):
                await self._error(websocket, request_id, "invalid_media", "附件路径无效或不属于当前 workspace")
                return
            if session_key is None:
                session_key = await self._create_session()
                await self._send_json(websocket, {"type": "session.created", "request_id": request_id, "session_id": session_key})
            await self._register(session_key, websocket)
            chat_id = session_key.split(":", 1)[1]
            await self._bus.publish_inbound(InboundMessage(self.name, "web", chat_id, text, list(media), {"request_id": request_id}))
            return
        if frame_type == "turn.stop" and session_key is not None:
            result = await self._interrupt.request_interrupt(session_key)
            await self._send_json(websocket, {"type": "turn.interrupted", "request_id": request_id, "session_id": session_key, "turn_id": result.turn_id, "status": result.status})
            return
        await self._error(websocket, request_id, "invalid_frame", f"不支持的帧类型: {frame_type or 'empty'}")

    async def _create_session(self) -> str:
        """创建 Web 会话并先持久化空元数据，保证随后 HTTP 查询不会短暂返回 404。"""

        session_key = f"web:{uuid4().hex}"
        if self._ensure_session is not None:
            await self._ensure_session(session_key)
        return session_key

    def _media_is_allowed(self, media: list[str]) -> bool:
        if not media or self._media_root is None:
            return True
        for value in media:
            try:
                path = Path(value).expanduser().resolve()
                path.relative_to(self._media_root)
            except (OSError, ValueError):
                return False
            if not path.is_file():
                return False
        return True

    async def _on_response(self, message: OutboundMessage) -> None:
        session_key = f"{message.channel}:{message.chat_id}"
        metadata = dict(message.metadata)
        sent = await self._broadcast(session_key, {
            "type": "message.final", "request_id": str(message.metadata.get("request_id") or ""),
            "session_id": session_key, "turn_id": str(message.metadata.get("turn_id") or ""),
            "content": message.content, "thinking": message.thinking, "media": list(message.media),
            "message_id": str(metadata.get("message_id") or ""), "metadata": metadata,
        })
        notification_id = str(metadata.get("notification_id") or "")
        if sent and notification_id and self._proactive_store is not None:
            await asyncio.to_thread(self._proactive_store.mark_notification_delivered, notification_id)

    async def _replay_pending_notifications(self, session_key: str, websocket: WebSocketApi) -> None:
        """订阅会话时补发离线通知；失败时保持 pending，供下一次连接重试。"""

        if self._proactive_store is None:
            return
        items = await asyncio.to_thread(
            self._proactive_store.list_notifications,
            session_key,
            pending_only=True,
        )
        for item in items:
            try:
                async def send() -> None:
                    await self._send_json(websocket, {
                        "type": "message.final",
                        "request_id": "",
                        "session_id": session_key,
                        "turn_id": "",
                        "content": item.content,
                        "thinking": "",
                        "media": [],
                        "message_id": item.id,
                        "metadata": notification_metadata(item),
                    })

                channel, chat_id = session_key.split(":", 1)
                await self._bus.chat_lane.run_non_passive(channel, chat_id, send)
            except Exception:
                return
            await asyncio.to_thread(self._proactive_store.mark_notification_delivered, item.id)

    async def _send_active_turn_snapshot(self, session_key: str, websocket: WebSocketApi) -> None:
        """从 AgentLoop 读取纯内存 running turn 快照，避免刷新后前端丢失正在生成的内容。"""

        snapshotter = getattr(self._interrupt, "get_active_turn_snapshot", None)
        if not callable(snapshotter):
            return
        snapshot = snapshotter(session_key)
        if not snapshot:
            return
        await self._send_json(websocket, {"type": "turn.snapshot", **dict(snapshot)})

    async def _send_context_usage_snapshot(self, session_key: str, websocket: WebSocketApi) -> None:
        """订阅时恢复与当前模型身份匹配的计量快照。"""

        if self._context_usage_loader is None:
            return
        try:
            snapshot = await self._context_usage_loader(session_key)
        except Exception as error:
            logger.warning("读取 Web 上下文计量快照失败 session=%s error=%s", session_key, error)
            await self._send_json(websocket, {
                "type": "context.usage.reset",
                "session_id": session_key,
            })
            return
        if not isinstance(snapshot, dict):
            await self._send_json(websocket, {
                "type": "context.usage.reset",
                "session_id": session_key,
            })
            return
        runtime_id = str(snapshot.get("model_runtime_id") or "")
        if self._context_runtime_id and runtime_id and runtime_id != self._context_runtime_id:
            # 模型切换后的旧 pressure 不得与新容量合并；等新模型返回 usage。
            await self._send_json(websocket, {
                "type": "context.usage.reset",
                "session_id": session_key,
            })
            return
        if snapshot.get("pressure_tokens") is None:
            await self._send_json(websocket, {
                "type": "context.usage.reset",
                "session_id": session_key,
            })
            return
        await self._send_json(websocket, {
            "type": "context.usage.updated",
            "session_id": session_key,
            "turn_id": "",
            **_context_usage_frame(snapshot),
        })

    async def _send_session_usage_snapshot(self, session_key: str, websocket: WebSocketApi) -> None:
        """订阅时恢复底栏累计用量；没有真实调用记录则保持隐藏。"""

        if self._session_usage_loader is None:
            return
        try:
            usage = await self._session_usage_loader(session_key)
        except Exception as error:
            logger.warning("读取会话累计用量失败 session=%s error=%s", session_key, error)
            return
        if isinstance(usage, dict):
            await self._send_json(websocket, {
                "type": "session.usage.updated",
                "session_id": session_key,
                "turn_id": "",
                **_session_usage_frame(usage),
            })

    async def _on_turn_started(self, event: TurnStarted) -> None:
        await self._broadcast(event.session_key, {"type": "turn.started", "request_id": event.request_id, "session_id": event.session_key, "turn_id": event.turn_id})

    async def _on_context_compaction_started(self, event: ContextCompactionStarted) -> None:
        await self._broadcast(event.session_key, {
            "type": "context.compaction.started",
            "session_id": event.session_key,
            "turn_id": event.turn_id,
            "trigger": event.trigger,
            "estimated_tokens": event.estimated_tokens,
        })

    async def _on_context_compaction_completed(self, event: ContextCompactionCompleted) -> None:
        await self._broadcast(event.session_key, {
            "type": "context.compaction.completed",
            "session_id": event.session_key,
            "turn_id": event.turn_id,
            "trigger": event.trigger,
            "estimated_tokens": event.estimated_tokens,
            "compacted": event.compacted,
        })

    async def _on_context_compaction_failed(self, event: ContextCompactionFailed) -> None:
        await self._broadcast(event.session_key, {
            "type": "context.compaction.failed",
            "session_id": event.session_key,
            "turn_id": event.turn_id,
            "trigger": event.trigger,
            "estimated_tokens": event.estimated_tokens,
            "message": event.error,
        })

    async def _on_context_usage_updated(self, event: ContextUsageUpdated) -> None:
        frame = {
            "type": "context.usage.updated",
            "session_id": event.session_key,
            "turn_id": event.turn_id,
            "used_tokens": event.used_tokens,
            "context_window": event.context_window,
            "soft_limit_tokens": event.soft_limit_tokens,
            "hard_input_tokens": event.hard_input_tokens,
            "context_window_source": event.context_window_source,
            "estimate_source": event.estimate_source,
            "breakdown": dict(event.breakdown),
            "sections": [dict(section) for section in event.sections],
        }
        optional = {
            "pressure_tokens": event.pressure_tokens,
            "projected_tokens": event.projected_tokens,
            "surface_tokens": event.surface_tokens,
            "system_tokens": event.system_tokens,
            "tools_tokens": event.tools_tokens,
            "message_tokens": event.message_tokens,
            "as_of_seq": event.as_of_seq,
            "model_runtime_id": event.model_runtime_id,
            "model": event.model,
        }
        frame.update({key: value for key, value in optional.items() if value is not None})
        await self._broadcast(event.session_key, frame)

    async def _on_session_usage_updated(self, event: SessionUsageUpdated) -> None:
        await self._broadcast(event.session_key, {
            "type": "session.usage.updated",
            "session_id": event.session_key,
            "turn_id": event.turn_id,
            "total_uncached_input_tokens": event.total_uncached_input_tokens,
            "total_cache_read_tokens": event.total_cache_read_tokens,
            "total_cache_write_tokens": event.total_cache_write_tokens,
            "total_input_tokens": event.total_input_tokens,
            "cache_hit_rate": event.cache_hit_rate,
            "total_output_tokens": event.total_output_tokens,
        })

    async def _on_session_updated(self, event: SessionUpdated) -> None:
        await self._broadcast(event.session_key, {
            "type": "session.updated",
            "session": dict(event.session),
        })

    async def _on_turn_queued(self, event: TurnQueued) -> None:
        await self._broadcast(event.session_key, {
            "type": "turn.queued",
            "request_id": event.request_id,
            "session_id": event.session_key,
            "position": event.position,
        })

    async def _on_turn_queue_rejected(self, event: TurnQueueRejected) -> None:
        messages = {
            "queue_full": "当前任务较多，请稍后再试",
            "session_busy": "当前会话正在处理消息",
            "closed": "服务正在关闭，请稍后再试",
        }
        await self._broadcast(event.session_key, {
            "type": "error",
            "request_id": event.request_id,
            "session_id": event.session_key,
            "code": event.reason,
            "message": messages.get(event.reason, "消息暂时无法处理"),
        })

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
            self._socket_send_locks.setdefault(websocket, asyncio.Lock())

    async def _unregister(self, websocket: WebSocketApi) -> None:
        async with self._lock:
            for key in list(self._connections):
                self._connections[key].discard(websocket)
                if not self._connections[key]:
                    self._connections.pop(key, None)
            # 发送锁使用弱引用保存；连接对象仍存活时始终复用同一把锁，断开且无引用后
            # 自动回收，避免清理与并发发送之间出现双锁竞态。

    async def _broadcast(self, session_key: str, payload: dict[str, Any]) -> int:
        async with self._lock:
            targets = list(self._connections.get(session_key, ()))
        failed: list[WebSocketApi] = []
        sent = 0
        for websocket in targets:
            try:
                await self._send_json(websocket, payload)
                sent += 1
            except Exception:
                failed.append(websocket)
        for websocket in failed:
            await self._unregister(websocket)
        return sent

    async def _send_json(
        self,
        websocket: WebSocketApi,
        payload: dict[str, Any],
    ) -> None:
        """同一 WebSocket 的流式帧和后台消息必须串行写入，避免并发发送异常。"""

        async with self._lock:
            send_lock = self._socket_send_locks.setdefault(websocket, asyncio.Lock())
        async with send_lock:
            await websocket.send_json(payload)

    async def _error(self, websocket: WebSocketApi, request_id: str, code: str, message: str) -> None:
        await self._send_json(websocket, {"type": "error", "request_id": request_id, "code": code, "message": message})

    async def close(self) -> None:
        self._bus.unsubscribe_outbound(self.name, self._on_response)
        self._events.off(TurnStarted, self._on_turn_started)
        self._events.off(ContextCompactionStarted, self._on_context_compaction_started)
        self._events.off(ContextCompactionCompleted, self._on_context_compaction_completed)
        self._events.off(ContextCompactionFailed, self._on_context_compaction_failed)
        self._events.off(ContextUsageUpdated, self._on_context_usage_updated)
        self._events.off(SessionUsageUpdated, self._on_session_usage_updated)
        self._events.off(SessionUpdated, self._on_session_updated)
        self._events.off(TurnQueued, self._on_turn_queued)
        self._events.off(TurnQueueRejected, self._on_turn_queue_rejected)
        self._events.off(StreamDeltaReady, self._on_stream_delta)
        self._events.off(ToolCallStarted, self._on_tool_started)
        self._events.off(ToolCallCompleted, self._on_tool_completed)
        async with self._lock:
            self._connections.clear()
            self._socket_send_locks.clear()


def _context_usage_frame(snapshot: dict[str, Any]) -> dict[str, Any]:
    """把持久化快照转成兼容实时事件的 WebSocket 帧。"""

    pressure = snapshot.get("pressure_tokens")
    projected = snapshot.get("projected_tokens")
    used = projected if projected is not None else pressure
    breakdown = {
        "system_prompt_tokens": int(snapshot.get("system_tokens") or 0),
        "tools_tokens": int(snapshot.get("tools_tokens") or 0),
        "conversation_tokens": int(snapshot.get("message_tokens") or 0),
    }


def _session_usage_frame(snapshot: dict[str, Any]) -> dict[str, Any]:
    """把累计字段限制在前端协议的明确命名和非负范围内。"""

    values = {
        "total_uncached_input_tokens": max(0, int(snapshot.get("total_uncached_input_tokens") or 0)),
        "total_cache_read_tokens": max(0, int(snapshot.get("total_cache_read_tokens") or 0)),
        "total_cache_write_tokens": max(0, int(snapshot.get("total_cache_write_tokens") or 0)),
        "total_input_tokens": max(0, int(snapshot.get("total_input_tokens") or 0)),
        "total_output_tokens": max(0, int(snapshot.get("total_output_tokens") or 0)),
    }
    values["cache_hit_rate"] = (
        float(snapshot["cache_hit_rate"])
        if snapshot.get("cache_hit_rate") is not None else None
    )
    return values
    return {
        "used_tokens": int(used or 0),
        "context_window": int(snapshot.get("context_window") or 0),
        "soft_limit_tokens": int(snapshot.get("soft_limit_tokens") or 0),
        "hard_input_tokens": int(snapshot.get("hard_input_tokens") or 0),
        "context_window_source": str(snapshot.get("context_window_source") or "unknown"),
        "estimate_source": "provider_projected" if projected is not None else "unknown",
        "breakdown": breakdown,
        "sections": [],
        **{key: snapshot[key] for key in (
            "pressure_tokens", "projected_tokens", "surface_tokens", "system_tokens",
            "tools_tokens", "message_tokens", "as_of_seq", "model_runtime_id", "model",
        ) if snapshot.get(key) is not None},
    }


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
