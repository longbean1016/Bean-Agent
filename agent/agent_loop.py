"""BeanAgent 单消费者 Turn 编排与中断控制。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from agent.event_bus import EventBus, TurnCommitted, TurnStarted
from agent.message_bus import InboundMessage, MessageBus, OutboundMessage, PipelineResult
from session.manager import SessionManager

logger = logging.getLogger(__name__)


class PipelineApi(Protocol):
    async def process(self, message: InboundMessage, *, turn_id: str) -> PipelineResult: ...


@dataclass(frozen=True, slots=True)
class InterruptResult:
    status: str
    session_key: str
    turn_id: str = ""


@dataclass(slots=True)
class TurnInterruptState:
    """被中断 Turn 的纯内存快照；30 分钟内由同一会话的下一条消息消费。"""

    session_key: str
    original_user_message: str
    original_metadata: dict[str, Any] = field(default_factory=dict)
    partial_reply: str = ""
    partial_thinking: str | None = None
    tools_used: list[str] = field(default_factory=list)
    tool_chain_partial: list[dict[str, Any]] = field(default_factory=list)
    interrupted_at: float = field(default_factory=time.monotonic)
    ttl_seconds: int = 1800

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.interrupted_at) > self.ttl_seconds


class AgentLoop:
    """唯一 Turn 编排者：推理、批量持久化、提交事件和最终出站。"""

    def __init__(self, bus: MessageBus, event_bus: EventBus, pipeline: PipelineApi, sessions: SessionManager) -> None:
        self._bus = bus
        self._events = event_bus
        self._pipeline = pipeline
        self._sessions = sessions
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_turn_ids: dict[str, str] = {}
        self._active_turn_states: dict[str, TurnInterruptState] = {}
        self.interrupt_states: dict[str, TurnInterruptState] = {}
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            await self.run_once()

    async def run_once(self) -> None:
        message = await self._bus.consume_inbound()
        turn_id = uuid4().hex
        self._active_turn_states[message.session_key] = TurnInterruptState(
            session_key=message.session_key,
            original_user_message=message.content,
            original_metadata=dict(message.metadata),
        )
        task = asyncio.create_task(self._process_message(message, turn_id), name=f"agent-turn:{message.session_key}")
        self._active_tasks[message.session_key] = task
        self._active_turn_ids[message.session_key] = turn_id
        try:
            await task
        finally:
            if self._active_tasks.get(message.session_key) is task:
                self._active_tasks.pop(message.session_key, None)
                self._active_turn_ids.pop(message.session_key, None)
                self._active_turn_states.pop(message.session_key, None)
            discard = getattr(self._pipeline, "discard_interrupt_snapshot", None)
            if callable(discard):
                discard(turn_id)
            self._bus.complete_inbound()

    def is_session_busy(self, session_key: str) -> bool:
        """供主动循环查询普通 Turn 占用状态，避免与用户请求并发发言。"""

        task = self._active_tasks.get(session_key)
        return task is not None and not task.done()

    async def _process_message(self, message: InboundMessage, turn_id: str) -> None:
        request_id = str(message.metadata.get("request_id") or "")
        resumed = await self._persist_pending_interrupt(message.session_key)
        # 用户消息在推理结束后才批量落库，因此工具执行前只能从 Session 的
        # 权威 next_seq 预测 ID。系统值必须覆盖入站同名字段，避免伪造 evidence。
        message.metadata["current_user_source_ref"] = (
            await self._sessions.peek_next_message_id(message.session_key)
        )
        await self._events.emit(TurnStarted(message.session_key, turn_id, request_id, message.content))
        try:
            # 正常被动 Turn 对齐参考实现：Pipeline 推理期间不提前把 user 放进 Session；
            # 推理完成后才把 user 与 assistant 作为一个提交批次写入 SQLite。
            result = await self._pipeline.process(message, turn_id=turn_id)
        except asyncio.CancelledError:
            # Channel 已同步返回 turn.interrupted；这里不写 Session 或发送 final，避免覆盖前端半截回复。
            return
        except Exception as error:
            logger.exception("Agent 推理异常: session_key=%s", message.session_key)
            await self._persist_terminal_turn(message, turn_id, "error", f"出错：{error}")
            await self._bus.publish_outbound(OutboundMessage(
                message.channel, message.chat_id, f"出错：{error}",
                metadata={"turn_id": turn_id, "request_id": request_id, "status": "error"},
            ))
            return

        session = await self._sessions.get_or_create(message.session_key)
        user = session.add_message("user", message.content, media=message.media, turn_id=turn_id)
        assistant = session.add_message(
            "assistant", result.content, media=result.media, turn_id=turn_id,
            reasoning_content=result.thinking, tool_chain=result.tool_chain,
            tools_used=result.tools_used, status="ok",
        )
        # append_messages 依次写入同一批的两条消息并原地回填稳定 ID。只有它完整返回，
        # 后台记忆才能看到完整 Turn，因此 TurnCommitted 必须位于此调用之后。
        await self._sessions.append_messages(session, [user, assistant])
        await self._events.emit(TurnCommitted(
            message.session_key, turn_id, str(user["id"]), str(assistant["id"]), "ok"
        ))
        await self._bus.publish_outbound(OutboundMessage(
            message.channel, message.chat_id, result.content, result.thinking, result.media,
            {"turn_id": turn_id, "request_id": request_id, "status": "ok"},
        ))
        if resumed:
            self.interrupt_states.pop(message.session_key, None)

    async def _persist_pending_interrupt(self, session_key: str) -> bool:
        state = self.interrupt_states.get(session_key)
        if state is None:
            return False
        if state.expired:
            # TTL 在下一条消息到达时惰性检查，避免为少量内存状态启动额外清理任务。
            self.interrupt_states.pop(session_key, None)
            return False
        if not state.original_user_message.strip():
            return True

        session = await self._sessions.get_or_create(session_key)
        user = session.add_message("user", state.original_user_message)
        assistant = session.add_message(
            "assistant",
            "[interrupted]",
            status="interrupted",
            tools_used=list(state.tools_used),
            tool_chain=list(state.tool_chain_partial),
        )
        # 中断标记必须成对批量落库；不发 TurnCommitted，避免未完成 Turn 进入长期记忆。
        await self._sessions.append_messages(session, [user, assistant])
        return True

    async def _persist_terminal_turn(self, message: InboundMessage, turn_id: str, status: str, assistant_content: str) -> None:
        session = await self._sessions.get_or_create(message.session_key)
        user = session.add_message("user", message.content, media=message.media, turn_id=turn_id)
        assistant = session.add_message("assistant", assistant_content, turn_id=turn_id, status=status)
        await self._sessions.append_messages(session, [user, assistant])
        # error/interrupted 表示推理结果不完整，不发 TurnCommitted，避免记忆模块把错误文本
        # 当作正常 assistant 证据进行归档或隐式提取。

    def request_interrupt(self, session_key: str) -> InterruptResult:
        task = self._active_tasks.get(session_key)
        if task is None or task.done():
            return InterruptResult("idle", session_key)
        turn_id = self._active_turn_ids.get(session_key, "")
        state = self._active_turn_states.get(session_key)
        if state is not None:
            snapshotter = getattr(self._pipeline, "snapshot_interrupt_state", None)
            snapshot = snapshotter(turn_id) if callable(snapshotter) else {}
            self.interrupt_states[session_key] = TurnInterruptState(
                session_key=session_key,
                original_user_message=state.original_user_message,
                original_metadata=dict(state.original_metadata),
                partial_reply=str(snapshot.get("partial_reply") or ""),
                partial_thinking=str(snapshot.get("partial_thinking") or "") or None,
                tools_used=list(snapshot.get("tools_used") or []),
                tool_chain_partial=list(snapshot.get("tool_chain_partial") or []),
            )
        task.cancel()
        return InterruptResult("interrupted", session_key, turn_id)

    def stop(self) -> None:
        self._running = False


__all__ = ["AgentLoop", "InterruptResult", "TurnInterruptState"]
