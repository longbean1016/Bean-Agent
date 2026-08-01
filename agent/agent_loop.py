"""BeanAgent 单消费者 Turn 编排与中断控制。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from agent.event_bus import (
    EventBus,
    SessionUpdated,
    TurnCommitted,
    TurnQueued,
    TurnQueueRejected,
    TurnPreparing,
    TurnStarted,
)
from agent.message_bus import InboundMessage, MessageBus, OutboundMessage, PipelineResult
from agent.turn_scheduler import QueuePosition, TurnScheduler
from session.manager import SessionManager

logger = logging.getLogger(__name__)

INTERRUPTED_ASSISTANT_CONTENT = "[用户已停止生成]"


class PipelineApi(Protocol):
    async def process(self, message: InboundMessage, *, turn_id: str) -> PipelineResult: ...


class ContextGuardApi(Protocol):
    """普通 Turn 在推理前调用的记忆积压保护接口。"""

    async def ensure_context_ready(self, session_key: str) -> bool: ...

    def needs_context_preparation(self, session_key: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class InterruptResult:
    status: str
    session_key: str
    turn_id: str = ""


@dataclass(slots=True)
class TurnInterruptState:
    """运行中 Turn 的内存快照，用于刷新恢复和中断持久化。"""

    session_key: str
    original_user_message: str
    original_media: list[str] = field(default_factory=list)
    original_metadata: dict[str, Any] = field(default_factory=dict)
    partial_reply: str = ""
    partial_thinking: str | None = None
    tools_used: list[str] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_chain_partial: list[dict[str, Any]] = field(default_factory=list)


class AgentLoop:
    """唯一 Turn 编排者：推理、批量持久化、提交事件和最终出站。"""

    def __init__(
        self,
        bus: MessageBus,
        event_bus: EventBus,
        pipeline: PipelineApi,
        sessions: SessionManager,
        *,
        context_guard: ContextGuardApi | None = None,
        max_concurrent_turns: int = 5,
        max_queued_turns: int = 20,
    ) -> None:
        self._bus = bus
        self._events = event_bus
        self._pipeline = pipeline
        self._sessions = sessions
        self._context_guard = context_guard
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_turn_ids: dict[str, str] = {}
        self._active_turn_states: dict[str, TurnInterruptState] = {}
        self._completion_waiters: dict[int, asyncio.Event] = {}
        self._scheduler = TurnScheduler(
            max_running=max_concurrent_turns,
            max_queued=max_queued_turns,
            start_turn=self._run_scheduled_turn,
            on_queue_positions=self._on_queue_positions,
            on_queued_cancelled=self._on_queued_cancelled,
        )
        self._running = False
        self._closed = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            message = await self._bus.consume_inbound()
            await self._submit(message)

    async def run_once(self) -> None:
        """提交并等待单条消息结束，保留单元测试和手动驱动的确定性语义。"""

        message = await self._bus.consume_inbound()
        completed = asyncio.Event()
        self._completion_waiters[id(message)] = completed
        await self._submit(message)
        await completed.wait()

    async def _submit(self, message: InboundMessage) -> None:
        result = await self._scheduler.submit(message)
        if result.status != "rejected":
            return
        request_id = str(message.metadata.get("request_id") or "")
        await self._events.emit(TurnQueueRejected(
            message.session_key,
            request_id,
            result.reason,
        ))
        await self._bus.complete_inbound(message)
        self._finish_waiter(message)

    async def _run_scheduled_turn(self, message: InboundMessage) -> None:
        turn_id = uuid4().hex
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("调度 Turn 缺少当前 asyncio Task")
        self._active_turn_states[message.session_key] = TurnInterruptState(
            session_key=message.session_key,
            original_user_message=message.content,
            original_media=list(message.media),
            original_metadata=dict(message.metadata),
        )
        self._active_tasks[message.session_key] = task
        self._active_turn_ids[message.session_key] = turn_id
        try:
            await self._process_message(message, turn_id)
        finally:
            if self._active_tasks.get(message.session_key) is task:
                self._active_tasks.pop(message.session_key, None)
                self._active_turn_ids.pop(message.session_key, None)
                self._active_turn_states.pop(message.session_key, None)
            discard = getattr(self._pipeline, "discard_interrupt_snapshot", None)
            if callable(discard):
                discard(turn_id)
            await self._bus.complete_inbound(message)
            self._finish_waiter(message)

    async def _on_queue_positions(self, positions: list[QueuePosition]) -> None:
        for item in positions:
            await self._events.emit(TurnQueued(
                item.message.session_key,
                str(item.message.metadata.get("request_id") or ""),
                item.position,
            ))

    async def _on_queued_cancelled(self, message: InboundMessage) -> None:
        await self._bus.complete_inbound(message)
        self._finish_waiter(message)

    def _finish_waiter(self, message: InboundMessage) -> None:
        waiter = self._completion_waiters.pop(id(message), None)
        if waiter is not None:
            waiter.set()

    def is_session_busy(self, session_key: str) -> bool:
        """供主动循环查询普通 Turn 占用状态，避免与用户请求并发发言。"""

        return self._scheduler.is_busy(session_key)

    async def _process_message(self, message: InboundMessage, turn_id: str) -> None:
        request_id = str(message.metadata.get("request_id") or "")
        # 用户消息在推理结束后才批量落库，因此工具执行前只能从 Session 的
        # 权威 next_seq 预测 ID。系统值必须覆盖入站同名字段，避免伪造 evidence。
        message.metadata["current_user_source_ref"] = (
            await self._sessions.peek_next_message_id(message.session_key)
        )
        needs_preparation = getattr(self._context_guard, "needs_context_preparation", None)
        if (
            self._context_guard is not None
            and not bool(message.metadata.get("skip_memory_context_guard"))
            and callable(needs_preparation)
            and needs_preparation(message.session_key)
        ):
            await self._events.emit(TurnPreparing(
                message.session_key,
                turn_id,
                request_id,
                message.content,
                list(message.media),
            ))
        if (
            self._context_guard is not None
            and not bool(message.metadata.get("skip_memory_context_guard"))
            and not await self._context_guard.ensure_context_ready(message.session_key)
        ):
            # 维护异常不写入用户 Session，否则错误文本会被后续记忆提取误当成
            # 对话事实；前端仍通过本次 Turn 的 final 事件获得明确状态。
            await self._bus.publish_outbound(
                OutboundMessage(
                    message.channel,
                    message.chat_id,
                    "记忆归档当前处于异常积压状态，本轮已暂停，请稍后重试。",
                    metadata={
                        "turn_id": turn_id,
                        "request_id": request_id,
                        "status": "error",
                        "reason": "memory_context_guard",
                    },
                )
            )
            await self._sessions.delete_if_empty(message.session_key)
            return

        await self._events.emit(TurnStarted(message.session_key, turn_id, request_id, message.content))

        # 首条消息进入运行阶段就生成默认标题，刷新会话列表时可以立即恢复标题。
        # 消息正文仍在 Turn 完成后批量写入，不改变消息持久化边界。
        session_summary = await self._sessions.ensure_default_title(
            message.session_key,
            message.content,
            list(message.media),
        )
        if session_summary is not None:
            await self._events.emit(SessionUpdated(message.session_key, session_summary))
        try:
            # 正常被动 Turn 对齐参考实现：Pipeline 推理期间不提前把 user 放进 Session；
            # 推理完成后才把 user 与 assistant 作为一个提交批次写入 SQLite。
            result = await self._pipeline.process(message, turn_id=turn_id)
        except asyncio.CancelledError:
            # Channel 已同步返回 turn.interrupted；这里不写 Session 或发送 final，避免覆盖前端半截回复。
            await self._sessions.delete_if_empty(message.session_key)
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
        context_retry = dict(getattr(result, "context_retry", {}) or {})
        user = session.add_message("user", message.content, media=message.media, turn_id=turn_id)
        assistant = session.add_message(
            "assistant", result.content, media=result.media, turn_id=turn_id,
            reasoning_content=result.thinking, tool_chain=result.tool_chain,
            tools_used=result.tools_used, status="ok",
            metadata={"context_retry": context_retry},
        )
        # append_messages 依次写入同一批的两条消息并原地回填稳定 ID。只有它完整返回，
        # 后台记忆才能看到完整 Turn，因此 TurnCommitted 必须位于此调用之后。
        await self._sessions.append_messages(session, [user, assistant])
        session_summary = await self._sessions.ensure_default_title(
            message.session_key,
            message.content,
            list(message.media),
        )
        if session_summary is not None:
            await self._events.emit(SessionUpdated(message.session_key, session_summary))
        await self._events.emit(TurnCommitted(
            message.session_key, turn_id, str(user["id"]), str(assistant["id"]), "ok"
        ))
        await self._bus.publish_outbound(OutboundMessage(
            message.channel, message.chat_id, result.content, result.thinking, result.media,
            {
                "turn_id": turn_id,
                "request_id": request_id,
                "status": "ok",
                "context_retry": context_retry,
            },
        ))
    async def _persist_terminal_turn(self, message: InboundMessage, turn_id: str, status: str, assistant_content: str) -> None:
        session = await self._sessions.get_or_create(message.session_key)
        user = session.add_message("user", message.content, media=message.media, turn_id=turn_id)
        assistant = session.add_message("assistant", assistant_content, turn_id=turn_id, status=status)
        await self._sessions.append_messages(session, [user, assistant])
        # error/interrupted 表示推理结果不完整，不发 TurnCommitted，避免记忆模块把错误文本
        # 当作正常 assistant 证据进行归档或隐式提取。

    async def request_interrupt(self, session_key: str) -> InterruptResult:
        task = self._active_tasks.get(session_key)
        turn_id = self._active_turn_ids.get(session_key, "")
        state = self._active_turn_states.get(session_key)
        interrupted: TurnInterruptState | None = None
        if task is not None and not task.done() and state is not None:
            snapshotter = getattr(self._pipeline, "snapshot_interrupt_state", None)
            snapshot = snapshotter(turn_id) if callable(snapshotter) else {}
            interrupted = TurnInterruptState(
                session_key=session_key,
                original_user_message=state.original_user_message,
                original_media=list(state.original_media),
                original_metadata=dict(state.original_metadata),
                partial_reply=str(snapshot.get("partial_reply") or ""),
                partial_thinking=str(snapshot.get("partial_thinking") or "") or None,
                tools_used=list(snapshot.get("tools_used") or []),
                tools=list(snapshot.get("tools") or []),
                tool_chain_partial=list(snapshot.get("tool_chain_partial") or []),
            )
        result = await self._scheduler.cancel(session_key)
        if result.status == "interrupted" and task is not None and interrupted is not None:
            await asyncio.gather(task, return_exceptions=True)
            await self._persist_interrupted_turn(interrupted, turn_id)
        return InterruptResult(result.status, session_key, turn_id)

    async def _persist_interrupted_turn(self, state: TurnInterruptState, turn_id: str) -> None:
        """立即保存中断轮，避免刷新或进程退出后丢失可见历史。"""

        session = await self._sessions.get_or_create(state.session_key)
        if any(str(message.get("turn_id") or "") == turn_id for message in session.messages):
            return
        tool_chain = interrupted_tool_chain(state.tool_chain_partial, state.tools)
        has_unfinished_tool = any(
            str(call.get("status") or "") in {"running", "interrupted"}
            for group in tool_chain
            for call in group["calls"]
        )
        tools_used = list(dict.fromkeys(
            str(call.get("name") or "")
            for group in tool_chain
            for call in group["calls"]
            if str(call.get("name") or "")
        ))
        user = session.add_message(
            "user",
            state.original_user_message,
            media=state.original_media,
            turn_id=turn_id,
        )
        assistant = session.add_message(
            "assistant",
            INTERRUPTED_ASSISTANT_CONTENT,
            turn_id=turn_id,
            status="interrupted",
            tools_used=tools_used,
            tool_chain=tool_chain,
            interrupted_display_content=state.partial_reply,
            interrupted_display_reasoning=state.partial_thinking or "",
            # 模型可能先输出过渡正文、随后才返回 tool_calls，不能仅凭 partial_reply
            # 判定思考完成；存在未结束工具时，整个 ReAct 推理链仍属于中断状态。
            interrupted_thinking_status=(
                "completed"
                if state.partial_reply and not has_unfinished_tool
                else "interrupted"
            ),
        )
        # 中断轮不发 TurnCommitted，避免未完成内容参与长期记忆提取。
        await self._sessions.append_messages(session, [user, assistant])

    def get_active_turn_snapshot(self, session_key: str) -> dict[str, Any] | None:
        """导出正在运行的 Turn 快照，供 WebSocket 刷新/重连后按 session 补发。"""

        task = self._active_tasks.get(session_key)
        turn_id = self._active_turn_ids.get(session_key, "")
        state = self._active_turn_states.get(session_key)
        if task is None or task.done() or state is None or not turn_id:
            return None
        snapshotter = getattr(self._pipeline, "snapshot_interrupt_state", None)
        snapshot = snapshotter(turn_id) if callable(snapshotter) else {}
        tools = [
            {
                "call_id": str(tool.get("call_id") or ""),
                "name": str(tool.get("name") or "tool"),
                "arguments": dict(tool.get("arguments") or {}),
                "status": _normalize_snapshot_tool_status(tool.get("status")),
                "result_preview": str(tool.get("result_preview") or ""),
            }
            for tool in snapshot.get("tools") or []
            if isinstance(tool, dict)
        ]
        return {
            "session_id": session_key,
            "turn_id": turn_id,
            "request_id": str(state.original_metadata.get("request_id") or ""),
            "user_message": state.original_user_message,
            "user_media": list(state.original_media),
            "content": str(snapshot.get("partial_reply") or ""),
            "thinking": str(snapshot.get("partial_thinking") or ""),
            "tools": tools,
            "status": "running",
        }

    def stop(self) -> None:
        self._running = False

    async def close(self) -> None:
        """停止准入并回收排队、运行和尚未消费的消息；可重复调用。"""

        if self._closed:
            return
        self._closed = True
        self._running = False
        await self._scheduler.close()
        discarded = await self._bus.discard_pending_inbound()
        for message in discarded:
            self._finish_waiter(message)


def interrupted_tool_chain(
    tool_chain: list[dict[str, Any]],
    live_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """保留已发起的工具快照，并将未结束调用标记为中断。"""

    completed: list[dict[str, Any]] = []
    for group in tool_chain:
        calls = group.get("calls") if isinstance(group, dict) else None
        if not isinstance(calls, list):
            continue
        retained: list[dict[str, Any]] = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            status = str(call.get("status") or "")
            if status not in {"ok", "completed", "error", "running", "interrupted"}:
                continue
            copied_call = dict(call)
            if status == "running":
                copied_call["status"] = "interrupted"
            retained.append(copied_call)
        if retained:
            copied = dict(group)
            copied["calls"] = retained
            completed.append(copied)
    known_call_ids = {
        str(call.get("call_id") or "")
        for group in completed
        for call in group.get("calls", [])
        if isinstance(call, dict)
    }
    interrupted_calls = [
        {
            "call_id": str(tool.get("call_id") or ""),
            "name": str(tool.get("name") or "tool"),
            "arguments": dict(tool.get("arguments") or {}),
            "result": str(tool.get("result_preview") or ""),
            "status": "interrupted",
        }
        for tool in live_tools
        if isinstance(tool, dict)
        and str(tool.get("call_id") or "")
        and str(tool.get("call_id") or "") not in known_call_ids
        and str(tool.get("status") or "") == "running"
    ]
    if interrupted_calls:
        if completed:
            completed[-1]["calls"] = [*completed[-1]["calls"], *interrupted_calls]
        else:
            completed.append({"iteration": 1, "text": "", "calls": interrupted_calls})
    return completed


def _normalize_snapshot_tool_status(value: object) -> str:
    text = str(value or "").strip()
    if text == "error":
        return "error"
    if text == "running":
        return "running"
    return "completed"


__all__ = ["AgentLoop", "InterruptResult", "TurnInterruptState", "interrupted_tool_chain"]
