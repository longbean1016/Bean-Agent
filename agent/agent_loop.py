"""BeanAgent 单消费者 Turn 编排与中断控制。"""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from agent.event_bus import (
    EventBus,
    SessionUpdated,
    TurnCommitted,
    TurnQueued,
    TurnQueueRejected,
    TurnStarted,
)
from agent.message_bus import InboundMessage, MessageBus, OutboundMessage, PipelineResult
from agent.turn_scheduler import QueuePosition, TurnScheduler
from session.manager import SessionManager
from session.model_surface import (
    INTERRUPTED_TOOL_RESULT_CONTENT,
    TOOL_NOT_STARTED_RESULT_CONTENT,
    TOOL_OUTCOME_UNKNOWN_RESULT_CONTENT,
)
from session.store import NewSessionEvent, NewSurfaceEvent, turn_timing_from_events
from tools.runtime import serialize_tool_result_messages

logger = logging.getLogger(__name__)
_LOCAL_TZ = ZoneInfo("Asia/Shanghai")

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
    duration_ms: int | None = None
    ended_at: str = ""


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
    llm_user_content: object | None = None
    llm_context_frame: str = ""
    llm_message_timestamp: str = ""
    llm_epoch_id: str = ""
    llm_surface_messages: list[dict[str, Any]] = field(default_factory=list)
    llm_surface_persisted: bool = False
    turn_started_at: str = ""
    iteration: int = 0


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
        # 兼容旧调用方保留引用形状；上下文准备不再由 AgentLoop 触发，token gate
        # 统一在 Pipeline 组装完整 payload 后执行。
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
            snapshotter = getattr(self._pipeline, "snapshot_interrupt_state", None)
            snapshot = snapshotter(turn_id) if callable(snapshotter) else {}
            terminal_timing = await self._persist_terminal_turn(
                message,
                turn_id,
                "error",
                f"出错：{error}",
                snapshot=snapshot,
            )
            await self._bus.publish_outbound(OutboundMessage(
                message.channel, message.chat_id, f"出错：{error}",
                metadata={
                    "turn_id": turn_id,
                    "request_id": request_id,
                    "status": "error",
                    **({"duration_ms": terminal_timing.get("duration_ms")} if terminal_timing.get("duration_ms") is not None else {}),
                    **({"generated_at": terminal_timing.get("ended_at")} if terminal_timing.get("ended_at") else {}),
                },
            ))
            return

        session = await self._sessions.get_or_create(message.session_key)
        context_retry = dict(getattr(result, "context_retry", {}) or {})
        persisted_timing = await self._load_turn_timing(message.session_key, turn_id)
        duration_ms = _duration_value(getattr(result, "duration_ms", None))
        if duration_ms is None:
            duration_ms = _duration_value(persisted_timing.get("duration_ms"))
        turn_started_at = str(
            getattr(result, "turn_started_at", "")
            or persisted_timing.get("started_at")
            or ""
        )
        turn_ended_at = str(
            getattr(result, "turn_ended_at", "")
            or persisted_timing.get("ended_at")
            or ""
        )
        user_projection: dict[str, Any] = {}
        llm_user_content = getattr(result, "llm_user_content", None)
        if llm_user_content is not None:
            user_projection["llm_user_content"] = deepcopy(llm_user_content)
        llm_context_frame = str(getattr(result, "llm_context_frame", "") or "")
        if llm_context_frame:
            user_projection["llm_context_frame"] = llm_context_frame
        llm_message_timestamp = str(
            getattr(result, "llm_message_timestamp", "") or ""
        )
        if llm_message_timestamp:
            user_projection["llm_message_timestamp"] = llm_message_timestamp
        llm_epoch_id = str(getattr(result, "llm_epoch_id", "") or "")
        if llm_epoch_id:
            user_projection["llm_epoch_id"] = llm_epoch_id
        llm_surface_messages = getattr(result, "llm_surface_messages", None)
        llm_surface_persisted = bool(getattr(result, "llm_surface_persisted", False))
        if not llm_surface_persisted and isinstance(llm_surface_messages, list) and llm_surface_messages:
            user_projection["llm_surface_messages"] = deepcopy(
                [item for item in llm_surface_messages if isinstance(item, dict)]
            )
        user_fields: dict[str, Any] = {"turn_id": turn_id, **user_projection}
        if turn_started_at:
            user_fields["timestamp"] = turn_started_at
        user = session.add_message(
            "user", message.content, media=message.media, **user_fields,
        )
        assistant_metadata: dict[str, Any] = {"context_retry": context_retry}
        route_metadata = _public_model_route(message.metadata.get("model_route"))
        if route_metadata:
            assistant_metadata["model_route"] = route_metadata
        if duration_ms is not None:
            assistant_metadata["duration_ms"] = duration_ms
        assistant_fields: dict[str, Any] = {
            "turn_id": turn_id,
            "reasoning_content": result.thinking,
            "tool_chain": result.tool_chain,
            "tools_used": result.tools_used,
            "status": "ok",
            "metadata": assistant_metadata,
        }
        if turn_ended_at:
            # assistant 的展示时间取 turn/end，而不是消息批量写入完成时间。
            assistant_fields["timestamp"] = turn_ended_at
        assistant = session.add_message(
            "assistant", result.content, media=result.media, **assistant_fields,
            # 终答轮单独思考写入 message dict；SessionManager.append_messages
            # 会把不在 fixed_fields 中的键自动归类到 extra，落库到
            # ``extra.final_reasoning_content``。下次 Turn 重建历史时优先读
            # 此字段还原到终答 assistant 的 reasoning_content，避免终答
            # ``reasoning_content``（拼接版）里重复携带工具轮思考。
            final_reasoning_content=result.final_reasoning,
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
                **({"model_route": route_metadata} if route_metadata else {}),
                **({"duration_ms": duration_ms} if duration_ms is not None else {}),
                **({"generated_at": turn_ended_at} if turn_ended_at else {}),
            },
        ))
    async def _persist_terminal_turn(
        self,
        message: InboundMessage,
        turn_id: str,
        status: str,
        assistant_content: str,
        *,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = await self._sessions.get_or_create(message.session_key)
        if snapshot and bool(snapshot.get("llm_surface_persisted")):
            # Provider/工具异常也可能留下已发送但未闭合的 tool-call；错误路径
            # 与主动中断共享同一 repair，避免下一轮携带非法的模型 transcript。
            await self._repair_durable_surface(
                TurnInterruptState(
                    session_key=message.session_key,
                    original_user_message=message.content,
                    original_media=list(message.media),
                    original_metadata=dict(message.metadata),
                    partial_reply=str(snapshot.get("partial_reply") or ""),
                    partial_thinking=str(snapshot.get("partial_thinking") or "") or None,
                    llm_user_content=deepcopy(snapshot.get("llm_user_content")),
                    llm_context_frame=str(snapshot.get("llm_context_frame") or ""),
                    llm_message_timestamp=str(snapshot.get("llm_message_timestamp") or ""),
                    llm_epoch_id=str(snapshot.get("llm_epoch_id") or ""),
                    llm_surface_messages=deepcopy(
                        [item for item in snapshot.get("llm_surface_messages") or [] if isinstance(item, dict)]
                    ),
                    llm_surface_persisted=True,
                    turn_started_at=str(snapshot.get("turn_started_at") or ""),
                    iteration=max(0, int(snapshot.get("iteration") or 0)),
                ),
                turn_id,
            )
        timing = await self._finish_turn_event(
            message.session_key,
            turn_id,
            status=status,
            step=max(0, int(snapshot.get("iteration") or 0)) if isinstance(snapshot, dict) else 0,
        )
        duration_ms = _duration_value(timing.get("duration_ms"))
        ended_at = str(timing.get("ended_at") or "")
        projection = _model_projection_from_snapshot(snapshot or {})
        partial_tool_chain = snapshot.get("tool_chain_partial") if isinstance(snapshot, dict) else []
        live_tools = snapshot.get("tools") if isinstance(snapshot, dict) else []
        if not isinstance(partial_tool_chain, list):
            partial_tool_chain = []
        if not isinstance(live_tools, list):
            live_tools = []
        tool_chain = interrupted_tool_chain(partial_tool_chain, live_tools)
        tools_used = list(dict.fromkeys(
            str(call.get("name") or "")
            for group in tool_chain
            for call in group.get("calls", [])
            if isinstance(call, dict) and str(call.get("name") or "")
        ))
        user_fields: dict[str, Any] = {"turn_id": turn_id, **projection}
        started_at = str(timing.get("started_at") or "")
        if started_at:
            user_fields["timestamp"] = started_at
        user = session.add_message("user", message.content, media=message.media, **user_fields)
        assistant_fields: dict[str, Any] = {
            "turn_id": turn_id,
            "status": status,
            "tools_used": tools_used,
            "tool_chain": tool_chain,
        }
        if duration_ms is not None:
            assistant_fields["metadata"] = {"duration_ms": duration_ms}
        if ended_at:
            assistant_fields["timestamp"] = ended_at
        assistant = session.add_message("assistant", assistant_content, **assistant_fields)
        await self._sessions.append_messages(session, [user, assistant])
        # error/interrupted 表示推理结果不完整，不发 TurnCommitted，避免记忆模块把错误文本
        # 当作正常 assistant 证据进行归档或隐式提取。
        return timing

    async def _load_turn_timing(self, session_key: str, turn_id: str) -> dict[str, Any]:
        fetch_events = getattr(self._sessions, "fetch_session_events", None)
        if not callable(fetch_events):
            return {}
        events = await fetch_events(session_key)
        return turn_timing_from_events(events, turn_id)

    async def _finish_turn_event(
        self,
        session_key: str,
        turn_id: str,
        *,
        status: str,
        step: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """为异常/中断路径补写唯一 turn/end，并返回可展示计时。"""

        timing = await self._load_turn_timing(session_key, turn_id)
        if timing.get("ended_at"):
            return timing
        append_event = getattr(self._sessions, "append_session_event", None)
        if not callable(append_event):
            return timing
        ended_at = datetime.now(_LOCAL_TZ).isoformat()
        data: dict[str, Any] = {"status": status, "ended_at": ended_at}
        started_at = str(timing.get("started_at") or "")
        if started_at:
            data["started_at"] = started_at
            duration_ms = _elapsed_duration_ms(started_at, ended_at)
            if duration_ms is not None:
                data["duration_ms"] = duration_ms
        if reason:
            data["reason"] = reason
        try:
            await append_event(NewSessionEvent(
                session_key=session_key,
                event_type="turn/end",
                turn_id=turn_id,
                step=max(0, int(step)),
                data=data,
                # 所有终态共用同一个 key，重复恢复只返回原事件，不制造第二个边界。
                operation_key=f"{turn_id}:turn-end",
            ))
        except ValueError:
            # 并发恢复可能同时计算出不同的当前时间；已提交的幂等事件才是权威结果。
            current = await self._load_turn_timing(session_key, turn_id)
            if not current.get("ended_at"):
                raise
        return await self._load_turn_timing(session_key, turn_id)

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
                llm_user_content=deepcopy(snapshot.get("llm_user_content")),
                llm_context_frame=str(snapshot.get("llm_context_frame") or ""),
                llm_message_timestamp=str(snapshot.get("llm_message_timestamp") or ""),
                llm_epoch_id=str(snapshot.get("llm_epoch_id") or ""),
                llm_surface_messages=deepcopy(
                    [item for item in snapshot.get("llm_surface_messages") or [] if isinstance(item, dict)]
                ),
                llm_surface_persisted=bool(snapshot.get("llm_surface_persisted")),
                turn_started_at=str(snapshot.get("turn_started_at") or ""),
                iteration=max(0, int(snapshot.get("iteration") or 0)),
            )
        result = await self._scheduler.cancel(session_key)
        if result.status == "interrupted" and task is not None and interrupted is not None:
            await asyncio.gather(task, return_exceptions=True)
            await self._persist_interrupted_turn(interrupted, turn_id)
        timing = await self._load_turn_timing(session_key, turn_id) if turn_id else {}
        return InterruptResult(
            result.status,
            session_key,
            turn_id,
            _duration_value(timing.get("duration_ms")),
            str(timing.get("ended_at") or ""),
        )

    async def _persist_interrupted_turn(self, state: TurnInterruptState, turn_id: str) -> None:
        """立即保存中断轮，避免刷新或进程退出后丢失可见历史。"""

        session = await self._sessions.get_or_create(state.session_key)
        if any(str(message.get("turn_id") or "") == turn_id for message in session.messages):
            return
        if state.llm_surface_persisted:
            await self._repair_durable_surface(state, turn_id)
        timing = await self._finish_turn_event(
            state.session_key,
            turn_id,
            status="interrupted",
            step=max(0, self._interrupt_iteration(state)),
        )
        duration_ms = _duration_value(timing.get("duration_ms"))
        ended_at = str(timing.get("ended_at") or "")
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
        projection = _model_projection_from_state(state)
        surface = projection.get("llm_surface_messages")
        if isinstance(surface, list) and surface:
            # 模型侧沿用参考实现的恢复规则：已经收到的半截 assistant 内容继续保留，
            # 未完成工具只补确定性的占位结果；前端看到的 interrupted 文案仍只在
            # 语义 assistant 中保存，不能把第二个 assistant 消息插入模型前缀。
            _repair_interrupted_surface(
                surface,
                started_call_ids={
                    str(item.get("call_id") or "")
                    for item in state.tools
                    if isinstance(item, dict)
                    and str(item.get("call_id") or "")
                    and str(item.get("status") or "") in {"running", "completed", "error"}
                },
            )
        user_fields: dict[str, Any] = {
            "turn_id": turn_id,
            **projection,
        }
        started_at = str(timing.get("started_at") or state.turn_started_at or "")
        if started_at:
            user_fields["timestamp"] = started_at
        user = session.add_message(
            "user", state.original_user_message, media=state.original_media, **user_fields,
        )
        assistant = session.add_message(
            "assistant",
            INTERRUPTED_ASSISTANT_CONTENT,
            turn_id=turn_id,
            status="interrupted",
            tools_used=tools_used,
            tool_chain=tool_chain,
            **({"metadata": {"duration_ms": duration_ms}} if duration_ms is not None else {}),
            **({"timestamp": ended_at} if ended_at else {}),
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

    async def _repair_durable_surface(
        self,
        state: TurnInterruptState,
        turn_id: str,
    ) -> None:
        """为已持久化的中断轮补半截 assistant 和未完成工具结果。"""

        nodes = await self._sessions.load_surface_events(state.session_key)
        fetch_events = getattr(self._sessions, "fetch_session_events", None)
        session_events = (
            await fetch_events(state.session_key)
            if callable(fetch_events) else []
        )
        started_call_ids = {
            str(item.get("data", {}).get("call_id") or "")
            for item in session_events
            if isinstance(item, dict)
            and str(item.get("turn_id") or "") == turn_id
            and item.get("event_type") == "tool/call"
            and isinstance(item.get("data"), dict)
        }
        # 旧调用方可能只持久化了 surface，没有事件日志；此时沿用旧语义，
        # 已存在的 assistant tool-call 按“结果未知”处理，避免误判成未启动。
        has_event_log = any(
            isinstance(item, dict) and str(item.get("turn_id") or "") == turn_id
            for item in session_events
        )
        current = [
            node for node in nodes
            if str(node.get("turn_id") or "") == turn_id
        ]
        epoch_id = str(
            state.llm_epoch_id
            or (current[-1].get("epoch_id") if current else "")
            or "default"
        )
        # 追加器在协程取消窗口内可能已经收到 Provider 消息但尚未完成写事务；
        # 用快照与已落库节点做顺序前缀对账，缺失尾部按稳定 operation_key 补写。
        persisted_messages = [
            node.get("message")
            for node in current
            if isinstance(node.get("message"), dict)
        ]
        snapshot_messages = [
            item for item in state.llm_surface_messages
            if isinstance(item, dict)
        ]
        recovered_partial_reply = ""
        recovered_partial_thinking = ""
        recovered_assistant_messages: list[tuple[int, dict[str, Any]]] = []
        if not snapshot_messages and has_event_log:
            # 进程重启后没有内存快照时，从完整消息事件和 chunk 事实恢复本轮
            # 模型前缀；chunk 只在没有 assistant/message 投影时合并成半截消息。
            recovered_messages: list[dict[str, Any]] = []
            assistant_steps: set[int] = set()
            chunks: dict[int, dict[str, str]] = {}
            for item in session_events:
                if not isinstance(item, dict) or str(item.get("turn_id") or "") != turn_id:
                    continue
                event_type = str(item.get("event_type") or "")
                data = item.get("data")
                if not isinstance(data, dict):
                    continue
                if event_type in {"user/message", "assistant/message", "tool/result"}:
                    model_message = data.get("message")
                    if isinstance(model_message, dict):
                        recovered_messages.append(deepcopy(model_message))
                        if event_type == "assistant/message":
                            assistant_steps.add(int(item.get("step") or 0))
                elif event_type == "assistant/chunk":
                    step = max(0, int(item.get("step") or 0))
                    bucket = chunks.setdefault(step, {"content": "", "thinking": ""})
                    bucket["content"] += str(data.get("content_delta") or "")
                    bucket["thinking"] += str(data.get("thinking_delta") or "")
            for step, chunk in sorted(chunks.items()):
                if step in assistant_steps or not (chunk["content"] or chunk["thinking"]):
                    continue
                recovered_message = {
                    "role": "assistant",
                    "content": chunk["content"],
                    **({"reasoning_content": chunk["thinking"]} if chunk["thinking"] else {}),
                }
                recovered_messages.append(recovered_message)
                recovered_assistant_messages.append((step, recovered_message))
                recovered_partial_reply = chunk["content"]
                recovered_partial_thinking = chunk["thinking"]
            snapshot_messages = recovered_messages
        append_event = getattr(self._sessions, "append_session_event", None)
        if recovered_assistant_messages and callable(append_event):
            for step, recovered_message in recovered_assistant_messages:
                await append_event(NewSessionEvent(
                    session_key=state.session_key,
                    event_type="assistant/message",
                    turn_id=turn_id,
                    step=step,
                    data={"message": deepcopy(recovered_message), "recovered": True},
                    operation_key=f"{turn_id}:{step}:recovered-assistant-message",
                ))
        common = 0
        while (
            common < len(persisted_messages)
            and common < len(snapshot_messages)
            and persisted_messages[common] == snapshot_messages[common]
        ):
            common += 1
        recovery_iteration = max(0, int(self._interrupt_iteration(state)))
        for index, model_message in enumerate(snapshot_messages[common:], start=common):
            role = str(model_message.get("role") or "").strip().lower()
            if role not in {"system", "user", "assistant", "tool"}:
                continue
            if role == "assistant" and model_message.get("tool_calls"):
                source_kind = "assistant_tool_call"
            elif role == "tool":
                source_kind = "tool_result"
            elif role == "user" and str(model_message.get("content") or "").startswith(
                '<system-reminder data-system-context-frame="true">'
            ):
                source_kind = "context_frame"
            elif role == "user":
                source_kind = "user_message"
            else:
                source_kind = "recovered_message"
            await self._sessions.append_surface(NewSurfaceEvent(
                session_key=state.session_key,
                epoch_id=epoch_id,
                turn_id=turn_id,
                iteration=recovery_iteration,
                role=role,
                content=deepcopy(model_message),
                source_kind=source_kind,
                operation_key=f"{turn_id}:recovery:{index}",
            ))

        nodes = await self._sessions.load_surface_events(state.session_key)
        current = [
            node for node in nodes
            if str(node.get("turn_id") or "") == turn_id
        ]
        if not current:
            return
        assistant_calls: list[tuple[int, str]] = []
        completed_calls: set[str] = set()
        for node in current:
            message = node.get("message")
            if not isinstance(message, dict):
                continue
            if str(message.get("role") or "") == "assistant":
                for call in message.get("tool_calls") or []:
                    if isinstance(call, dict) and str(call.get("id") or ""):
                        assistant_calls.append((int(node.get("iteration") or 0), str(call["id"])))
            elif str(message.get("role") or "") == "tool":
                call_id = str(message.get("tool_call_id") or "")
                if call_id:
                    completed_calls.add(call_id)

        # 流式 Provider 尚未返回完整 assistant 时，仍把已交付正文保存在模型侧 surface；
        # UI 的停止文案仍只写语义 assistant，不会污染这个模型前缀。
        partial_reply = str(state.partial_reply or "") or recovered_partial_reply
        partial_thinking = str(state.partial_thinking or "") or recovered_partial_thinking
        if partial_reply or partial_thinking:
            already_persisted = any(
                isinstance(node.get("message"), dict)
                and node["message"].get("role") == "assistant"
                and node["message"].get("content") == partial_reply
                for node in current
            )
            if not already_persisted:
                await self._sessions.append_surface(NewSurfaceEvent(
                    session_key=state.session_key,
                    epoch_id=epoch_id,
                    turn_id=turn_id,
                    iteration=max(0, int(self._interrupt_iteration(state))),
                    role="assistant",
                    content={
                        "role": "assistant",
                        "content": partial_reply,
                        **({"reasoning_content": partial_thinking} if partial_thinking else {}),
                    },
                    source_kind="interrupted_partial_assistant",
                    operation_key=f"{turn_id}:interrupted-partial-assistant",
                ))

        for iteration, call_id in assistant_calls:
            if call_id in completed_calls:
                continue
            placeholder = (
                TOOL_OUTCOME_UNKNOWN_RESULT_CONTENT
                if call_id in started_call_ids or not has_event_log
                else TOOL_NOT_STARTED_RESULT_CONTENT
            )
            await self._sessions.append_surface(NewSurfaceEvent(
                session_key=state.session_key,
                epoch_id=epoch_id,
                turn_id=turn_id,
                iteration=iteration,
                role="tool",
                content={
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": placeholder,
                },
                source_kind="interrupted_tool_result",
                operation_key=f"{turn_id}:{iteration}:interrupted-tool-result:{call_id}",
            ))
            append_event = getattr(self._sessions, "append_session_event", None)
            if callable(append_event):
                await append_event(NewSessionEvent(
                    session_key=state.session_key,
                    event_type="tool/result",
                    turn_id=turn_id,
                    step=iteration,
                    data={
                        "call_id": call_id,
                        "content": placeholder,
                        "recovery": True,
                        "outcome_known": False,
                    },
                    operation_key=f"{turn_id}:{iteration}:interrupted-tool-result-event:{call_id}",
                ))

        append_event = getattr(self._sessions, "append_session_event", None)
        if callable(append_event):
            await append_event(NewSessionEvent(
                session_key=state.session_key,
                event_type="step/end",
                turn_id=turn_id,
                step=recovery_iteration,
                data={"status": "interrupted", "repaired": True},
                operation_key=f"{turn_id}:{recovery_iteration}:repair-step-end",
            ))

    @staticmethod
    def _interrupt_iteration(state: TurnInterruptState) -> int:
        """从快照扩展字段读取流式中断所在 iteration。"""

        value = getattr(state, "iteration", 0)
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

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
            "started_at": str(snapshot.get("turn_started_at") or ""),
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


def _model_projection_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """从失败/中断快照提取隐藏模型投影，不改变语义消息正文。"""

    if bool(snapshot.get("llm_surface_persisted")):
        return {}

    projection = _model_projection(
        llm_user_content=snapshot.get("llm_user_content"),
        llm_context_frame=str(snapshot.get("llm_context_frame") or ""),
        llm_message_timestamp=str(snapshot.get("llm_message_timestamp") or ""),
        llm_epoch_id=str(snapshot.get("llm_epoch_id") or ""),
        llm_surface_messages=snapshot.get("llm_surface_messages"),
        llm_surface_persisted=bool(snapshot.get("llm_surface_persisted")),
    )
    surface = projection.get("llm_surface_messages")
    if isinstance(surface, list) and surface:
        _repair_interrupted_surface(
            surface,
            started_call_ids={
                str(item.get("call_id") or "")
                for item in snapshot.get("tools") or []
                if isinstance(item, dict)
                and str(item.get("call_id") or "")
                and str(item.get("status") or "") in {"running", "completed", "error"}
            },
        )
    return projection


def _model_projection_from_state(state: TurnInterruptState) -> dict[str, Any]:
    """从已复制的中断状态提取可重放的模型侧消息。"""

    return _model_projection(
        llm_user_content=state.llm_user_content,
        llm_context_frame=state.llm_context_frame,
        llm_message_timestamp=state.llm_message_timestamp,
        llm_epoch_id=state.llm_epoch_id,
        llm_surface_messages=state.llm_surface_messages,
        llm_surface_persisted=state.llm_surface_persisted,
        started_call_ids={
            str(item.get("call_id") or "")
            for item in state.tools
            if isinstance(item, dict)
            and str(item.get("call_id") or "")
            and str(item.get("status") or "") in {"running", "completed", "error"}
        },
    )


def _model_projection(
    *,
    llm_user_content: object | None,
    llm_context_frame: str,
    llm_message_timestamp: str,
    llm_epoch_id: str,
    llm_surface_messages: object,
    llm_surface_persisted: bool = False,
    started_call_ids: set[str] | None = None,
) -> dict[str, Any]:
    """统一组装隐藏字段，避免正常、失败和中断路径发生字段漂移。"""

    if llm_surface_persisted:
        return {}
    projection: dict[str, Any] = {}
    if llm_user_content is not None:
        projection["llm_user_content"] = deepcopy(llm_user_content)
    if llm_context_frame:
        projection["llm_context_frame"] = llm_context_frame
    if llm_message_timestamp:
        projection["llm_message_timestamp"] = llm_message_timestamp
    if llm_epoch_id:
        projection["llm_epoch_id"] = llm_epoch_id
    if isinstance(llm_surface_messages, list):
        copied = [item for item in llm_surface_messages if isinstance(item, dict)]
        if copied:
            projection["llm_surface_messages"] = deepcopy(copied)
    return projection


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


def _repair_interrupted_surface(
    surface: list[dict[str, Any]],
    *,
    started_call_ids: set[str] | None = None,
) -> None:
    """闭合中断时已发出的工具调用，保持模型下一轮请求协议合法。"""

    tool_call_names: dict[str, str] = {}
    tool_call_order: list[str] = []
    result_ids: set[str] = set()
    for message in surface:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "assistant":
            calls = message.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                call_id = str(call.get("id") or "").strip()
                if not call_id:
                    continue
                if call_id not in tool_call_names:
                    tool_call_order.append(call_id)
                tool_call_names[call_id] = (
                    str(function.get("name") or "")
                    if isinstance(function, dict)
                    else ""
                )
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "").strip()
            if call_id:
                result_ids.add(call_id)

    # 只为模型已经收到的 tool-call 补结果；没有 assistant tool-call 的语义快照
    # 仍走旧历史重建逻辑，不能凭 UI 的 running 工具凭空制造孤立 tool 消息。
    missing_ids = [
        call_id
        for call_id in tool_call_order
        if call_id not in result_ids
    ]
    for call_id in missing_ids:
        placeholder = (
            TOOL_OUTCOME_UNKNOWN_RESULT_CONTENT
            if started_call_ids is None or call_id in started_call_ids
            else TOOL_NOT_STARTED_RESULT_CONTENT
        )
        surface.extend(
            serialize_tool_result_messages(
                tool_call_id=call_id,
                content=placeholder,
                tool_name=tool_call_names.get(call_id) or None,
            )
        )


def _normalize_snapshot_tool_status(value: object) -> str:
    text = str(value or "").strip()
    if text == "error":
        return "error"
    if text == "running":
        return "running"
    return "completed"


def _duration_value(value: object) -> int | None:
    try:
        duration = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return duration if duration >= 0 else None


def _public_model_route(value: object) -> dict[str, Any]:
    """移除仅供进程内 lease 查找的键，再写入消息和 WebSocket。"""

    if not isinstance(value, dict):
        return {}
    allowed = {
        "connection_id", "connection_revision", "model_id", "model_revision",
        "adapter", "reasoning_effort", "model_runtime_id",
    }
    return {key: value[key] for key in allowed if key in value}


def _elapsed_duration_ms(started_at: str, ended_at: str) -> int | None:
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=_LOCAL_TZ)
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=_LOCAL_TZ)
    return max(0, int(round((ended - started).total_seconds() * 1000)))


__all__ = ["AgentLoop", "InterruptResult", "TurnInterruptState", "interrupted_tool_chain"]
