"""BeanAgent 单消费者 Turn 编排与中断控制。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol
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


class AgentLoop:
    """唯一 Turn 编排者：推理、批量持久化、提交事件和最终出站。"""

    def __init__(self, bus: MessageBus, event_bus: EventBus, pipeline: PipelineApi, sessions: SessionManager) -> None:
        self._bus = bus
        self._events = event_bus
        self._pipeline = pipeline
        self._sessions = sessions
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_turn_ids: dict[str, str] = {}
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            await self.run_once()

    async def run_once(self) -> None:
        message = await self._bus.consume_inbound()
        turn_id = uuid4().hex
        task = asyncio.create_task(self._process_message(message, turn_id), name=f"agent-turn:{message.session_key}")
        self._active_tasks[message.session_key] = task
        self._active_turn_ids[message.session_key] = turn_id
        try:
            await task
        finally:
            if self._active_tasks.get(message.session_key) is task:
                self._active_tasks.pop(message.session_key, None)
                self._active_turn_ids.pop(message.session_key, None)
            self._bus.complete_inbound()

    async def _process_message(self, message: InboundMessage, turn_id: str) -> None:
        request_id = str(message.metadata.get("request_id") or "")
        await self._events.emit(TurnStarted(message.session_key, turn_id, request_id, message.content))
        try:
            # 正常被动 Turn 对齐参考实现：Pipeline 推理期间不提前把 user 放进 Session；
            # 推理完成后才把 user 与 assistant 作为一个提交批次写入 SQLite。
            result = await self._pipeline.process(message, turn_id=turn_id)
        except asyncio.CancelledError:
            await self._persist_terminal_turn(message, turn_id, "interrupted", "对话已中断")
            await self._bus.publish_outbound(OutboundMessage(
                message.channel, message.chat_id, "对话已中断",
                metadata={"turn_id": turn_id, "request_id": request_id, "status": "interrupted"},
            ))
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
        task.cancel()
        return InterruptResult("interrupted", session_key, self._active_turn_ids.get(session_key, ""))

    def stop(self) -> None:
        self._running = False


__all__ = ["AgentLoop", "InterruptResult"]
