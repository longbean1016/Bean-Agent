"""WebSocket 生命周期与传输无关命令/事件之间的适配通道。"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from agent.event_bus import (
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ContextCompactionStarted,
    ContextUsageUpdated,
    EventBus,
    SandboxApprovalRequested,
    SessionUsageUpdated,
    SessionUpdated,
    StreamDeltaReady,
    ToolCallCompleted,
    ToolCallStarted,
    TurnQueued,
    TurnQueueRejected,
    TurnStarted,
)
from agent.message_bus import MessageBus, OutboundMessage
from agent.web_commands import (
    InterruptController,
    WebCommandError,
    WebCommandService,
    normalize_web_session_id,
)
from agent.web_events import MappedWebEvent, WebEventMapper, WebLifecycleEvent
from proactive.notification_service import notification_metadata
from proactive.store import ProactiveStore

logger = logging.getLogger(__name__)


class WebSocketApi(Protocol):
    async def accept(self) -> None: ...
    async def receive_json(self) -> dict[str, Any]: ...
    async def send_json(self, data: dict[str, Any]) -> None: ...


_WEB_EVENT_TYPES = (
    TurnStarted,
    ContextCompactionStarted,
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ContextUsageUpdated,
    SessionUsageUpdated,
    SessionUpdated,
    TurnQueued,
    TurnQueueRejected,
    StreamDeltaReady,
    ToolCallStarted,
    ToolCallCompleted,
    SandboxApprovalRequested,
)


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
        ensure_session: Callable[..., Awaitable[object]] | None = None,
        context_usage_loader: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
        session_usage_loader: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
        context_runtime_id: str = "",
        sandbox_loader: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
        sandbox_mode_writer: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
        workspace_writer: Callable[[str, str | None], Awaitable[dict[str, Any]]] | None = None,
        approvals: Any | None = None,
    ) -> None:
        self._bus = bus
        self._events = event_bus
        self._commands = WebCommandService(
            bus,
            interrupt_controller,
            media_root=media_root,
            ensure_session=ensure_session,
            sandbox_loader=sandbox_loader,
            sandbox_mode_writer=sandbox_mode_writer,
            workspace_writer=workspace_writer,
            approvals=approvals,
        )
        self._mapper = WebEventMapper()
        self._proactive_store = proactive_store
        self._context_usage_loader = context_usage_loader
        self._session_usage_loader = session_usage_loader
        self._context_runtime_id = str(context_runtime_id or "")
        self._connections: dict[str, set[WebSocketApi]] = {}
        self._socket_send_locks: weakref.WeakKeyDictionary[WebSocketApi, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )
        self._lock = asyncio.Lock()
        bus.subscribe_outbound(self.name, self._on_response)
        for event_type in _WEB_EVENT_TYPES:
            event_bus.on(event_type, self._on_event)

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
            try:
                session_key = await self._commands.create_session(
                    workspace_id=str(frame.get("workspace_id") or "") or None,
                    sandbox_mode=str(frame.get("sandbox_mode") or "read-only"),
                    risk_confirmed=frame.get("risk_confirmed") is True,
                )
            except WebCommandError as error:
                await self._error(websocket, request_id, error.code, error.message)
                return
            await self._register(session_key, websocket)
            await self._send_json(websocket, {
                "type": "session.created",
                "request_id": request_id,
                "session_id": session_key,
            })
            await self._send_sandbox_snapshot(session_key, websocket)
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
            await self._send_sandbox_snapshot(session_key, websocket)
            await self._send_pending_approvals(session_key, websocket)
            # 订阅确认后只给当前连接补发运行中快照，用于刷新/重连恢复流式草稿。
            await self._send_active_turn_snapshot(session_key, websocket)
            await self._replay_pending_notifications(session_key, websocket)
            return
        if frame_type == "message.send":
            try:
                prepared = await self._commands.prepare_message(
                    request_id=request_id,
                    session_id=frame.get("session_id"),
                    text=frame.get("text"),
                    media=frame.get("media", []),
                    workspace_id=str(frame.get("workspace_id") or "") or None,
                    sandbox_mode=str(frame.get("sandbox_mode") or "read-only"),
                    risk_confirmed=frame.get("risk_confirmed") is True,
                )
            except WebCommandError as error:
                await self._error(websocket, request_id, error.code, error.message)
                return

            # 发布可能同步触发 Turn 事件，因此连接注册和新会话确认必须先完成。
            await self._register(prepared.session_key, websocket)
            if prepared.created_session:
                await self._send_json(websocket, {
                    "type": "session.created",
                    "request_id": request_id,
                    "session_id": prepared.session_key,
                })
                await self._send_sandbox_snapshot(prepared.session_key, websocket)
            await self._commands.submit_message(prepared)
            return
        if frame_type == "turn.stop" and session_key is not None:
            result = await self._commands.stop_turn(session_key)
            await self._send_mapped(
                websocket,
                self._mapper.map_interrupted(session_key, request_id, result),
            )
            return
        if frame_type == "sandbox.mode.set" and session_key is not None:
            try:
                snapshot = await self._commands.set_sandbox_mode(
                    session_key,
                    str(frame.get("sandbox_mode") or ""),
                    risk_confirmed=frame.get("risk_confirmed") is True,
                )
            except WebCommandError as error:
                await self._error(websocket, request_id, error.code, error.message)
                return
            await self._broadcast(session_key, {
                "type": "sandbox.updated",
                "request_id": request_id,
                "sandbox": snapshot,
            })
            return
        if frame_type == "workspace.bind" and session_key is not None:
            try:
                snapshot = await self._commands.bind_workspace(
                    session_key,
                    str(frame.get("workspace_id") or "").strip() or None,
                )
            except WebCommandError as error:
                await self._error(websocket, request_id, error.code, error.message)
                return
            await self._broadcast(session_key, {
                "type": "sandbox.updated",
                "request_id": request_id,
                "sandbox": snapshot,
            })
            return
        if frame_type == "approval.decide" and session_key is not None:
            approval_id = str(frame.get("approval_id") or "")
            decision = str(frame.get("decision") or "")
            try:
                await self._commands.decide_approval(approval_id, session_key, decision)
            except WebCommandError as error:
                await self._error(websocket, request_id, error.code, error.message)
                return
            await self._broadcast(session_key, {
                "type": "approval.resolved",
                "request_id": request_id,
                "session_id": session_key,
                "approval_id": approval_id,
                "decision": decision,
            })
            return
        await self._error(
            websocket,
            request_id,
            "invalid_frame",
            f"不支持的帧类型: {frame_type or 'empty'}",
        )

    async def _on_response(self, message: OutboundMessage) -> None:
        mapped = self._mapper.map_outbound(message)
        sent = await self._broadcast_mapped(mapped)
        notification_id = str(message.metadata.get("notification_id") or "")
        if sent and notification_id and self._proactive_store is not None:
            await asyncio.to_thread(
                self._proactive_store.mark_notification_delivered,
                notification_id,
            )

    async def _on_event(self, event: WebLifecycleEvent) -> None:
        await self._broadcast_mapped(self._mapper.map_event(event))

    async def _replay_pending_notifications(
        self,
        session_key: str,
        websocket: WebSocketApi,
    ) -> None:
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
                mapped = self._mapper.map_pending_notification(
                    session_key,
                    content=item.content,
                    message_id=item.id,
                    metadata=notification_metadata(item),
                )

                async def send() -> None:
                    await self._send_mapped(websocket, mapped)

                channel, chat_id = session_key.split(":", 1)
                await self._bus.chat_lane.run_non_passive(channel, chat_id, send)
            except Exception:
                return
            await asyncio.to_thread(
                self._proactive_store.mark_notification_delivered,
                item.id,
            )

    async def _send_active_turn_snapshot(
        self,
        session_key: str,
        websocket: WebSocketApi,
    ) -> None:
        """只向新订阅连接恢复 AgentLoop 中尚未持久化的运行快照。"""

        snapshot = self._commands.get_active_turn_snapshot(session_key)
        if snapshot is None:
            return
        await self._send_mapped(
            websocket,
            self._mapper.map_active_turn_snapshot(session_key, snapshot),
        )

    async def _send_context_usage_snapshot(
        self,
        session_key: str,
        websocket: WebSocketApi,
    ) -> None:
        """订阅时恢复与当前模型身份匹配的计量快照。"""

        if self._context_usage_loader is None:
            return
        try:
            snapshot = await self._context_usage_loader(session_key)
        except Exception as error:
            logger.warning("读取 Web 上下文计量快照失败 session=%s error=%s", session_key, error)
            await self._send_mapped(
                websocket,
                self._mapper.map_context_usage_reset(session_key),
            )
            return
        if not isinstance(snapshot, dict):
            await self._send_mapped(
                websocket,
                self._mapper.map_context_usage_reset(session_key),
            )
            return
        runtime_id = str(snapshot.get("model_runtime_id") or "")
        if self._context_runtime_id and runtime_id and runtime_id != self._context_runtime_id:
            # 模型切换后的旧 pressure 不得与新容量合并；等新模型返回 usage。
            await self._send_mapped(
                websocket,
                self._mapper.map_context_usage_reset(session_key),
            )
            return
        if snapshot.get("pressure_tokens") is None:
            await self._send_mapped(
                websocket,
                self._mapper.map_context_usage_reset(session_key),
            )
            return
        await self._send_mapped(
            websocket,
            self._mapper.map_context_usage_snapshot(session_key, snapshot),
        )

    async def _send_session_usage_snapshot(
        self,
        session_key: str,
        websocket: WebSocketApi,
    ) -> None:
        """订阅时恢复底栏累计用量；没有真实调用记录则保持隐藏。"""

        if self._session_usage_loader is None:
            return
        try:
            usage = await self._session_usage_loader(session_key)
        except Exception as error:
            logger.warning("读取会话累计用量失败 session=%s error=%s", session_key, error)
            return
        if isinstance(usage, dict):
            await self._send_mapped(
                websocket,
                self._mapper.map_session_usage_snapshot(session_key, usage),
            )

    async def _send_sandbox_snapshot(
        self,
        session_key: str,
        websocket: WebSocketApi,
    ) -> None:
        snapshot = await self._commands.sandbox_snapshot(session_key)
        if snapshot is not None:
            await self._send_json(websocket, {
                "type": "sandbox.updated",
                "request_id": "",
                "sandbox": snapshot,
            })

    async def _send_pending_approvals(
        self,
        session_key: str,
        websocket: WebSocketApi,
    ) -> None:
        for request in await self._commands.pending_approvals(session_key):
            await self._send_json(websocket, {
                "type": "approval.requested",
                "session_id": session_key,
                "approval": request.to_wire(),
            })

    async def _register(self, session_key: str, websocket: WebSocketApi) -> None:
        async with self._lock:
            self._connections.setdefault(session_key, set()).add(websocket)
            self._socket_send_locks.setdefault(websocket, asyncio.Lock())
        await self._commands.set_session_available(session_key, True)

    async def _unregister(self, websocket: WebSocketApi) -> None:
        unavailable: list[str] = []
        async with self._lock:
            for key in list(self._connections):
                self._connections[key].discard(websocket)
                if not self._connections[key]:
                    self._connections.pop(key, None)
                    unavailable.append(key)
            # 发送锁使用弱引用保存；连接对象仍存活时始终复用同一把锁，断开且无引用后
            # 自动回收，避免清理与并发发送之间出现双锁竞态。
        for session_key in unavailable:
            await self._commands.set_session_available(session_key, False)

    async def _broadcast_mapped(self, mapped: MappedWebEvent) -> int:
        sent = 0
        for payload in mapped.payloads:
            sent = await self._broadcast(mapped.session_key, payload)
        return sent

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

    async def _send_mapped(
        self,
        websocket: WebSocketApi,
        mapped: MappedWebEvent,
    ) -> None:
        for payload in mapped.payloads:
            await self._send_json(websocket, payload)

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

    async def _error(
        self,
        websocket: WebSocketApi,
        request_id: str,
        code: str,
        message: str,
    ) -> None:
        await self._send_json(websocket, {
            "type": "error",
            "request_id": request_id,
            "code": code,
            "message": message,
        })

    async def close(self) -> None:
        self._bus.unsubscribe_outbound(self.name, self._on_response)
        for event_type in _WEB_EVENT_TYPES:
            self._events.off(event_type, self._on_event)
        async with self._lock:
            self._connections.clear()
            self._socket_send_locks.clear()


__all__ = ["WebChannel", "normalize_web_session_id"]
