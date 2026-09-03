"""记忆检索、Prompt 组装、LLM ReAct 与工具执行流水线。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from copy import deepcopy
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from agent.attachment_content import build_current_user_content
from agent.context_budget import (
    estimate_payload_breakdown,
    estimate_payload_tokens,
    hard_input_limit,
    should_compact,
    soft_limit_tokens,
)
from agent.event_bus import (
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ContextCompactionStarted,
    ContextUsageUpdated,
    EventBus,
    SessionUsageUpdated,
    StreamDeltaReady,
    ToolCallCompleted,
    ToolCallStarted,
)
from agent.message_bus import InboundMessage, PipelineResult
from agent.prompt_assembler import PromptAssembler
from agent.prompt_block import TurnContext
from agent.prompt_cache_diagnostics import (
    PromptCacheDiagnostics,
    PromptCacheRequestDiagnostics,
    canonical_header_hash,
)
from agent.prompt_cache_log import PromptCacheLogWriter
from agent.provider import ContextLengthError, LLMResponse, ProviderUsage
from agent.skills import SkillsLoader, collect_skill_mentions
from agent.tool_runtime import ToolRuntimeView
from session.store import NewSessionEvent, NewSurfaceEvent
from tools.base import normalize_tool_result
from tools.registry import ToolRegistry
from tools.runtime import append_tool_result


class ProviderApi(Protocol):
    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> LLMResponse: ...


HistoryLoader = Callable[[str, int | None], Awaitable[list[dict[str, Any]]]]
SurfaceLoader = Callable[[str], Awaitable[list[dict[str, Any]]]]
SurfaceAppender = Callable[[NewSurfaceEvent], Awaitable[dict[str, Any]]]
SessionEventAppender = Callable[[NewSessionEvent], Awaitable[dict[str, Any]]]
ContextCompactor = Callable[..., Awaitable[bool]]
ContextUsageLoader = Callable[[str], Awaitable[dict[str, Any] | None]]
ContextUsageWriter = Callable[[str, dict[str, Any]], Awaitable[None]]
SessionUsageWriter = Callable[[str, str, int, dict[str, Any]], Awaitable[dict[str, Any]]]

logger = logging.getLogger(__name__)

_LOCAL_TZ = ZoneInfo("Asia/Shanghai")
_SUMMARY_MAX_TOKENS = 512
_INCOMPLETE_SUMMARY_PROMPT = """当前任务需要先暂停继续调用工具，请直接输出给用户看的中文阶段性回复。
必须基于已有上下文，不要编造结果。
必须包含四点：
1) 已经使用了哪些工具或操作，以及拿到了什么关键信息；
2) 当前已经做到哪一步；
3) 还缺什么信息或步骤；
4) 如果继续，下一步会怎么做。
可以提到工具名称和关键结果，但不要暴露 tool_call_id、schema、内部 prompt 或原始参数 JSON。
禁止输出"已达到最大迭代次数"这类模板句；不要输出 JSON。"""


class Pipeline:
    """执行单个 Turn 的纯推理部分，不负责 Session 持久化或最终出站。"""

    def __init__(
        self,
        provider: ProviderApi,
        tools: ToolRegistry,
        event_bus: EventBus,
        assembler: PromptAssembler,
        *,
        workspace: str,
        memory: Any | None = None,
        skills: SkillsLoader | None = None,
        prompt_cache_log: PromptCacheLogWriter | None = None,
        history_loader: HistoryLoader | None = None,
        surface_loader: SurfaceLoader | None = None,
        surface_appender: SurfaceAppender | None = None,
        event_appender: SessionEventAppender | None = None,
        context_compactor: ContextCompactor | None = None,
        context_usage_loader: ContextUsageLoader | None = None,
        context_usage_writer: ContextUsageWriter | None = None,
        session_usage_writer: SessionUsageWriter | None = None,
        max_iterations: int = 10,
        multimodal: bool = True,
        vl_available: bool = False,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._events = event_bus
        self._assembler = assembler
        self._workspace = workspace
        self._memory = memory
        self._skills = skills
        self._prompt_cache_log = prompt_cache_log
        # 诊断锚点按会话隔离；它只保存消息指纹，不保存 Prompt 正文。
        self._prompt_cache_diagnostics = PromptCacheDiagnostics()
        self._history_loader = history_loader
        self._surface_loader = surface_loader
        self._surface_appender = surface_appender
        self._event_appender = event_appender
        self._context_compactor = context_compactor
        self._context_usage_loader = context_usage_loader
        self._context_usage_writer = context_usage_writer
        self._session_usage_writer = session_usage_writer
        self._max_iterations = max(1, int(max_iterations))
        # 图片能力在应用组装时确定，Turn 内只读取这份稳定快照。纯文本主模型不能
        # 接收 image_url；独立 VL 可用时则由现有 ReAct 工具链负责真正读图。
        self._multimodal = bool(multimodal)
        self._vl_available = bool(vl_available)
        # 中断快照只服务于当前进程内的停止/续跑语义，不进入 Session 或长期记忆。
        self._interrupt_snapshots: dict[str, dict[str, Any]] = {}
        # 只保存每个会话最近一次供应商 usage 锚点；完整快照由 SessionStore
        # 持久化，进程重启或 WebSocket 重连都不会依赖这份内存缓存。
        self._context_measurements: dict[str, dict[str, Any]] = {}

    async def process(self, message: InboundMessage, *, turn_id: str) -> PipelineResult:
        turn_started_at = datetime.now(_LOCAL_TZ).isoformat()
        turn_started_monotonic = time.perf_counter()
        self._interrupt_snapshots[turn_id] = {
            "partial_reply": "",
            "partial_thinking": "",
            "tools": [],
            "tools_used": [],
            "tool_chain_partial": [],
            "llm_surface_messages": [],
            "llm_user_content": None,
            "llm_context_frame": "",
            "llm_message_timestamp": "",
            "llm_epoch_id": "",
            "llm_surface_persisted": False,
            "turn_started_at": turn_started_at,
        }
        if self._event_appender is not None:
            await self._event_appender(NewSessionEvent(
                session_key=message.session_key,
                event_type="turn/start",
                turn_id=turn_id,
                step=0,
                data={
                    "channel": message.channel,
                    "chat_id": message.chat_id,
                    "started_at": turn_started_at,
                },
                operation_key=f"{turn_id}:turn-start",
            ))
        raw_allowed_tools = message.metadata.get("allowed_tools")
        allowed_tools = (
            [str(name) for name in raw_allowed_tools if str(name).strip()]
            if isinstance(raw_allowed_tools, (list, tuple, set))
            else None
        )
        initial_tool_names = (
            [name for name in allowed_tools if self._tools.has_tool(name)]
            if allowed_tools is not None
            else self._tools.get_always_on_order()
        )
        tool_view = ToolRuntimeView.create(
            channel=message.channel,
            chat_id=message.chat_id,
            session_key=message.session_key,
            current_user_source_ref=str(
                message.metadata.get("current_user_source_ref") or ""
            ),
            visible_names=initial_tool_names,
        )
        # Scheduler soft Turn 与目标 Web 会话隔离；普通 Turn 不设置这些系统字段，
        # 因而继续使用原有历史、记忆、流式事件和工具可见性。
        skip_history = bool(message.metadata.get("skip_history"))
        skip_memory = bool(message.metadata.get("skip_memory_retrieval"))
        suppress_stream = bool(message.metadata.get("suppress_stream_events"))
        if self._surface_loader and not skip_history:
            # Provider 历史只从 durable surface 派生；语义消息由 AgentLoop 单独保存，
            # 不能在这里再次拼接，否则同一 Turn 会出现两份模型消息。
            history = await self._surface_loader(message.session_key)
        else:
            history = (
                await self._history_loader(message.session_key, None)
                if self._history_loader and not skip_history
                else []
            )
        measurement = await self._load_context_measurement(message.session_key)
        retrieved = await self._memory.retrieve_for_turn(message) if self._memory and not skip_memory else ""
        names = list(tool_view.visible_order)
        available_skills = (
            [record.name for record in self._skills.list_skill_records()]
            if self._skills
            else []
        )
        active_skills = collect_skill_mentions(message.content, available_skills)
        deferred_hint = _build_deferred_tools_hint(self._tools, tool_view.visible_names)
        checkpoint_summary = ""
        if self._memory and not skip_memory and self._surface_loader is None:
            read_checkpoint = getattr(self._memory, "read_checkpoint_summary", None)
            if callable(read_checkpoint):
                checkpoint_summary = str(read_checkpoint(message.session_key) or "")
        context = TurnContext(
            workspace=self._workspace,
            channel=message.channel,
            chat_id=message.chat_id,
            memory=None if skip_memory else self._memory,
            retrieved_memory_block=retrieved,
            checkpoint_summary=checkpoint_summary,
            active_tool_names=names,
            skills=self._skills,
            active_skill_names=active_skills,
            deferred_tools_hint=deferred_hint,
        )
        current_content = await build_current_user_content(
            message.content,
            message.media,
            multimodal=self._multimodal,
            vl_available=self._vl_available,
        )
        message_timestamp = datetime.now(_LOCAL_TZ)
        # 记录最终模型消息中的用户包装和动态 frame；生产链路由 durable surface
        # 在 Provider 调用前增量落库，下一轮直接重放而不是重新生成时间戳或媒体 block。
        llm_user_content: object | None = None
        llm_context_frame = ""
        llm_surface_messages: list[dict[str, Any]] = []
        context_retry: dict[str, Any] = {
            "attempts": [],
            "selected_plan": "token_gate",
            "history_messages": len(history),
            # 保留诊断字段供旧观测端读取；新链路永远不按 section 退避。
            "disabled_sections": [],
        }
        react_messages: list[dict[str, Any]] = []
        tool_chain: list[dict[str, Any]] = []
        tools_used: list[str] = []
        thinking_parts: list[str] = []
        last_base_messages: list[dict[str, Any]] = []
        forced_compaction_attempted = False
        llm_epoch_id = ""
        surface_turn_initialized = False
        surface_persisted = False
        surface_tail_seq: int | None = None
        chunk_index: dict[int, int] = {}
        chunk_event_seqs: dict[int, list[int]] = {}

        async def append_session_event(
            event_type: str,
            *,
            iteration: int,
            data: dict[str, Any],
            operation_suffix: str,
            source_event_seqs: list[int] | None = None,
        ) -> dict[str, Any] | None:
            if self._event_appender is None:
                return None
            return await self._event_appender(NewSessionEvent(
                session_key=message.session_key,
                event_type=event_type,
                turn_id=turn_id,
                step=max(0, int(iteration)),
                data=deepcopy(data),
                operation_key=f"{turn_id}:{operation_suffix}",
                source_event_seqs=source_event_seqs,
            ))

        turn_end: dict[str, Any] | None = None

        async def append_turn_end(
            *,
            iteration: int,
            status: str,
            reason: str | None = None,
        ) -> dict[str, Any]:
            """记录一次幂等 Turn 结束，并把真实计时同步到中断快照。"""

            nonlocal turn_end
            if turn_end is not None:
                return turn_end
            ended_at = datetime.now(_LOCAL_TZ).isoformat()
            duration_ms = max(
                0,
                int(round((time.perf_counter() - turn_started_monotonic) * 1000)),
            )
            data: dict[str, Any] = {
                "status": status,
                "started_at": turn_started_at,
                "ended_at": ended_at,
                "duration_ms": duration_ms,
            }
            if reason:
                data["reason"] = reason
            await append_session_event(
                "turn/end",
                iteration=iteration,
                data=data,
                operation_suffix="turn-end",
            )
            turn_end = {
                "duration_ms": duration_ms,
                "turn_started_at": turn_started_at,
                "turn_ended_at": ended_at,
            }
            self._interrupt_snapshots[turn_id].update(turn_end)
            return turn_end

        async def append_surface_message(
            model_message: dict[str, Any],
            *,
            iteration: int,
            source_kind: str,
            operation_suffix: str,
            status: str = "committed",
        ) -> None:
            """按 Provider 发送顺序写入 durable surface；无写入器时保留旧测试路径。"""

            nonlocal surface_persisted, surface_tail_seq
            role = str(model_message.get("role") or "").strip().lower()
            if self._surface_appender is not None:
                # 首轮 frame/user 在请求发出前没有诊断 epoch，沿用预计算值；后续
                # Provider 返回后优先使用实际请求 header 的 epoch，避免工具 schema
                # 或配置变化仍把新节点错误归入旧缓存域。
                epoch = str(llm_epoch_id or surface_epoch_id or "default")
                persisted = await self._surface_appender(NewSurfaceEvent(
                    session_key=message.session_key,
                    epoch_id=epoch,
                    turn_id=turn_id,
                    iteration=iteration,
                    role=role,
                    content=deepcopy(model_message),
                    source_kind=source_kind,
                    operation_key=f"{turn_id}:{iteration}:{operation_suffix}",
                    status=status,
                ))
                surface_persisted = True
                if isinstance(persisted, dict) and persisted.get("surface_seq") is not None:
                    surface_tail_seq = max(0, int(persisted["surface_seq"]))
            event_type = {
                "context_frame": "user/message",
                "user_message": "user/message",
                "assistant_tool_call": "assistant/message",
                "assistant_empty": "assistant/message",
                "assistant_final": "assistant/message",
                "tool_result": "tool/result",
            }.get(source_kind)
            if event_type:
                await append_session_event(
                    event_type,
                    iteration=iteration,
                    data={"message": deepcopy(model_message)},
                    operation_suffix=f"{iteration}:{operation_suffix}:message",
                    source_event_seqs=(
                        list(chunk_event_seqs.get(iteration, []))
                        if event_type == "assistant/message" else None
                    ),
                )

        surface_epoch_id = ""

        def sync_surface_snapshot() -> None:
            """把已发送的模型侧前缀同步到中断/失败快照，供恢复时精确重放。"""

            snapshot = self._interrupt_snapshots.get(turn_id)
            if snapshot is None:
                return
            snapshot.update({
                "llm_surface_messages": deepcopy(llm_surface_messages),
                "llm_user_content": deepcopy(llm_user_content),
                "llm_context_frame": llm_context_frame,
                "llm_message_timestamp": message_timestamp.isoformat(),
                "llm_epoch_id": llm_epoch_id,
                "llm_surface_persisted": surface_persisted,
            })

        async def compact_with_status(*, estimated_tokens: int, force: bool) -> bool:
            """等待当前 Turn 所需的 summary，同时把压缩阶段明确广播给前端。"""

            trigger = "context_overflow" if force else "soft_limit"
            await self._events.emit(ContextCompactionStarted(
                session_key=message.session_key,
                turn_id=turn_id,
                trigger=trigger,
                estimated_tokens=estimated_tokens,
            ))
            try:
                compacted = await self._context_compactor(
                    message.session_key,
                    estimated_tokens=estimated_tokens,
                    force=force,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._events.emit(ContextCompactionFailed(
                    session_key=message.session_key,
                    turn_id=turn_id,
                    trigger=trigger,
                    estimated_tokens=estimated_tokens,
                    error=str(error),
                ))
                raise
            await self._events.emit(ContextCompactionCompleted(
                session_key=message.session_key,
                turn_id=turn_id,
                trigger=trigger,
                estimated_tokens=estimated_tokens,
                compacted=bool(compacted),
            ))
            return bool(compacted)

        def append_turn_thinking(value: str | None) -> None:
            text = str(value or "").strip()
            if not text:
                return
            # thinking 是整个 Turn 的过程信息；工具轮与最终轮都要保留，避免完成后缩水。
            if thinking_parts and thinking_parts[-1] == text:
                return
            thinking_parts.append(text)

        def merged_turn_thinking() -> str:
            return "\n\n".join(thinking_parts)

        async def on_delta(delta: dict[str, str]) -> None:
            iteration = max(0, int(self._interrupt_snapshots[turn_id].get("iteration") or 0))
            index = chunk_index.get(iteration, 0)
            chunk_index[iteration] = index + 1
            snapshot = self._interrupt_snapshots[turn_id]
            snapshot["partial_reply"] += str(delta.get("content_delta") or "")
            snapshot["partial_thinking"] += str(delta.get("thinking_delta") or "")
            event = await append_session_event(
                "assistant/chunk",
                iteration=iteration,
                data={
                    "content_delta": str(delta.get("content_delta") or ""),
                    "thinking_delta": str(delta.get("thinking_delta") or ""),
                    "index": index,
                },
                operation_suffix=f"{iteration}:assistant-chunk:{index}",
            )
            if event is not None and event.get("event_seq") is not None:
                chunk_event_seqs.setdefault(iteration, []).append(int(event["event_seq"]))
            if suppress_stream:
                return
            await self._events.emit(StreamDeltaReady(
                session_key=message.session_key,
                turn_id=turn_id,
                content_delta=str(delta.get("content_delta") or ""),
                thinking_delta=str(delta.get("thinking_delta") or ""),
            ))

        async def chat_with_context_retry() -> tuple[LLMResponse, PromptCacheRequestDiagnostics]:
            nonlocal last_base_messages, history, forced_compaction_attempted, measurement
            nonlocal llm_user_content, llm_context_frame
            nonlocal surface_turn_initialized, surface_epoch_id
            while True:
                history_for_attempt = history
                disabled_sections: set[str] = set()
                context_retry["attempts"].append(
                    {
                        "name": "token_gate",
                        "history_messages": len(history_for_attempt),
                    }
                )
                assembled = self._assembler.assemble(
                    turn_ctx=context,
                    history=history_for_attempt,
                    current_message=current_content,
                    message_timestamp=message_timestamp,
                    disabled_sections=disabled_sections,
                )
                if assembled.messages:
                    last_message = assembled.messages[-1]
                    if last_message.get("role") == "user":
                        llm_user_content = deepcopy(last_message.get("content"))
                    for candidate in reversed(assembled.messages[:-1]):
                        content = candidate.get("content")
                        if (
                            candidate.get("role") == "user"
                            and isinstance(content, str)
                            and content.startswith("<system-reminder data-system-context-frame=\"true\">")
                        ):
                            llm_context_frame = content
                            break
                    if not llm_surface_messages:
                        if llm_context_frame:
                            llm_surface_messages.append({
                                "role": "user",
                                "content": llm_context_frame,
                            })
                        if llm_user_content is not None:
                            llm_surface_messages.append({
                                "role": "user",
                                "content": deepcopy(llm_user_content),
                            })
                        sync_surface_snapshot()
                tool_schemas = self._tools.get_schemas(
                    visible_names=tool_view.visible_names,
                    visible_order=tool_view.visible_order,
                )
                if (
                    (self._surface_appender is not None or self._event_appender is not None)
                    and not surface_turn_initialized
                ):
                    surface_epoch_id = canonical_header_hash(
                        _request_header_identity(
                            self._provider,
                            assembled.messages,
                            tool_schemas,
                            tool_choice="auto",
                        )
                    )[:16]
                    if llm_context_frame:
                        frame_message = {
                            "role": "user",
                            "content": llm_context_frame,
                        }
                        await append_surface_message(
                            frame_message,
                            iteration=0,
                            source_kind="context_frame",
                            operation_suffix="frame",
                        )
                    if llm_user_content is not None:
                        user_message = {
                            "role": "user",
                            "content": deepcopy(llm_user_content),
                        }
                        await append_surface_message(
                            user_message,
                            iteration=0,
                            source_kind="user_message",
                            operation_suffix="user",
                        )
                    surface_turn_initialized = True
                    sync_surface_snapshot()
                # ReAct 后缀可能包含已经执行过的工具结果。超限重试只重建基础
                # Prompt，原样保留该后缀，避免重复执行有副作用的工具。
                model_messages = [*assembled.messages, *react_messages]
                last_base_messages = assembled.messages
                estimate = (
                    self._provider.estimate_context_tokens(model_messages, tool_schemas)
                    if callable(getattr(self._provider, "estimate_context_tokens", None))
                    else estimate_payload_tokens(model_messages, tool_schemas)
                )
                provider_window = int(getattr(self._provider, "context_window", 0) or 0)
                provider_output = int(getattr(self._provider, "max_tokens", 0) or 0)
                provider_model = str(getattr(self._provider, "model", "") or "")
                provider_runtime_id = str(
                    getattr(self._provider, "runtime_id", "")
                    or f"{getattr(self._provider, 'provider_name', '')}:{provider_model}:{provider_window}"
                )
                soft_limit = soft_limit_tokens(provider_window) if provider_window > 0 else 0
                hard_limit = (
                    hard_input_limit(provider_window, provider_output)
                    if provider_window > 0 and 0 <= provider_output < provider_window
                    else 0
                )
                context_sections = tuple(
                    {
                        "name": item.name,
                        "estimated_tokens": item.est_tokens,
                        "static": item.is_static,
                        "cache_hit": item.cache_hit,
                    }
                    for item in assembled.debug_breakdown
                )
                breakdown = estimate_payload_breakdown(model_messages, tool_schemas)
                surface_tokens = int(breakdown.get("conversation_tokens", 0))
                active_measurement = measurement
                if active_measurement and active_measurement.get("model_runtime_id") != provider_runtime_id:
                    # 模型切换后旧模型的 pressure 不能与新容量拼接，等新模型
                    # 首次返回 usage 后再建立新的锚点。
                    active_measurement = None
                    measurement = None
                pressure_tokens = (
                    int(active_measurement["pressure_tokens"])
                    if active_measurement and active_measurement.get("pressure_tokens") is not None
                    else None
                )
                anchor_tokens = (
                    int(active_measurement["anchor_tokens"])
                    if active_measurement and active_measurement.get("anchor_tokens") is not None
                    else None
                )
                projected_tokens = (
                    max(0, pressure_tokens + estimate - anchor_tokens)
                    if pressure_tokens is not None and anchor_tokens is not None
                    else None
                )
                # 圆圈主指标只接受供应商 pressure；没有 usage 时 used_tokens
                # 仅作为旧观测端的 gate 估算，不得让界面误认为精确值。
                display_tokens = projected_tokens if projected_tokens is not None else estimate
                snapshot = {
                    "pressure_tokens": pressure_tokens,
                    "projected_tokens": projected_tokens,
                    "surface_tokens": surface_tokens,
                    "system_tokens": int(breakdown.get("system_prompt_tokens", 0)),
                    "tools_tokens": int(breakdown.get("tools_tokens", 0)),
                    "message_tokens": surface_tokens,
                    "as_of_seq": _context_as_of_seq(message),
                    "model_runtime_id": provider_runtime_id,
                    "model": provider_model,
                    "context_window": provider_window,
                    "context_window_source": str(
                        getattr(self._provider, "context_window_source", "unknown") or "unknown"
                    ),
                    "soft_limit_tokens": soft_limit,
                    "hard_input_tokens": hard_limit,
                    "anchor_tokens": anchor_tokens,
                }
                await self._persist_context_measurement(message.session_key, snapshot)
                await self._events.emit(ContextUsageUpdated(
                    session_key=message.session_key,
                    turn_id=turn_id,
                    used_tokens=display_tokens,
                    context_window=provider_window,
                    soft_limit_tokens=soft_limit,
                    hard_input_tokens=hard_limit,
                    context_window_source=str(
                        getattr(self._provider, "context_window_source", "unknown") or "unknown"
                    ),
                    estimate_source="provider_projected" if projected_tokens is not None else "heuristic",
                    breakdown=breakdown,
                    sections=context_sections,
                    pressure_tokens=pressure_tokens,
                    projected_tokens=projected_tokens,
                    surface_tokens=surface_tokens,
                    system_tokens=snapshot["system_tokens"],
                    tools_tokens=snapshot["tools_tokens"],
                    message_tokens=surface_tokens,
                    as_of_seq=snapshot["as_of_seq"],
                    model_runtime_id=provider_runtime_id,
                    model=provider_model,
                ))
                # 已有 Provider pressure 时，压缩门控必须使用校准后的投影；
                # 仅使用本地 heuristic 会在实际输入已越过阈值时漏触发压缩。
                gate_tokens = projected_tokens if projected_tokens is not None else estimate
                if (
                    not react_messages
                    and self._context_compactor is not None
                    and should_compact(
                        gate_tokens,
                        context_window=provider_window,
                        max_output_tokens=provider_output,
                    )
                ):
                    compacted = await compact_with_status(
                        estimated_tokens=gate_tokens,
                        force=False,
                    )
                    if compacted and (self._surface_loader or self._history_loader) is not None:
                        history = (
                            await self._surface_loader(message.session_key)
                            if self._surface_loader
                            else await self._history_loader(message.session_key, None)
                        )
                        if self._surface_loader:
                            history = _drop_current_surface_suffix(
                                history,
                                context_frame=llm_context_frame,
                                user_content=llm_user_content,
                            )
                        read_checkpoint = getattr(self._memory, "read_checkpoint_summary", None)
                        if callable(read_checkpoint) and self._surface_loader is None:
                            context.checkpoint_summary = str(
                                read_checkpoint(message.session_key) or ""
                            )
                        # checkpoint 改变了历史边界和动态摘要，必须重新组装完整 payload，
                        # 不能只替换已生成的 history 列表，否则 system 顺序和 cache key 会漂移。
                        continue
                try:
                    request_diagnostic: PromptCacheRequestDiagnostics | None = None

                    def on_request(
                        sent_messages: list[dict[str, Any]],
                        sent_tools: list[dict[str, Any]],
                    ) -> None:
                        nonlocal request_diagnostic, llm_epoch_id
                        request_diagnostic = self._prompt_cache_diagnostics.observe(
                            message.session_key,
                            sent_messages,
                            sent_tools,
                            header=_request_header_identity(
                                self._provider,
                                sent_messages,
                                sent_tools,
                                tool_choice="auto",
                            ),
                            surface_seq=surface_tail_seq,
                        )
                        llm_epoch_id = request_diagnostic.epoch_id
                        sync_surface_snapshot()

                    async def on_usage(usage: ProviderUsage) -> None:
                        # 同一 iteration 的所有 usage 样本都写入同一幂等键；存储层
                        # 用最终样本覆盖早到样本，流中断时也保留已经收到的事实。
                        await self._record_session_usage_values(
                            message.session_key,
                            turn_id,
                            iteration,
                            usage,
                        )

                    response = await self._provider.chat(
                        model_messages,
                        tool_schemas,
                        tool_choice="auto",
                        on_content_delta=on_delta,
                        on_usage=on_usage,
                        on_request=on_request,
                    )
                    if request_diagnostic is None:
                        # 测试 Provider 或旧适配器可能忽略诊断回调；仍以送入
                        # 适配器的完整数组建立基线，但不伪称已捕获最终规范化值。
                        request_diagnostic = self._prompt_cache_diagnostics.observe(
                            message.session_key,
                            model_messages,
                            tool_schemas,
                            header=_request_header_identity(
                                self._provider,
                                model_messages,
                                tool_schemas,
                                tool_choice="auto",
                            ),
                            surface_seq=surface_tail_seq,
                        )
                        llm_epoch_id = request_diagnostic.epoch_id
                        sync_surface_snapshot()
                    provider_pressure = _provider_pressure_tokens(response)
                    if provider_pressure is not None:
                        measurement = {
                            **snapshot,
                            "pressure_tokens": provider_pressure,
                            "projected_tokens": provider_pressure,
                            "anchor_tokens": estimate,
                        }
                        await self._persist_context_measurement(
                            message.session_key, measurement
                        )
                        await self._events.emit(ContextUsageUpdated(
                            session_key=message.session_key,
                            turn_id=turn_id,
                            used_tokens=provider_pressure,
                            context_window=provider_window,
                            soft_limit_tokens=soft_limit,
                            hard_input_tokens=hard_limit,
                            context_window_source=str(
                                getattr(self._provider, "context_window_source", "unknown") or "unknown"
                            ),
                            estimate_source="provider_usage",
                            breakdown=breakdown,
                            sections=context_sections,
                            pressure_tokens=provider_pressure,
                            projected_tokens=provider_pressure,
                            surface_tokens=surface_tokens,
                            system_tokens=snapshot["system_tokens"],
                            tools_tokens=snapshot["tools_tokens"],
                            message_tokens=surface_tokens,
                            as_of_seq=snapshot["as_of_seq"],
                            model_runtime_id=provider_runtime_id,
                            model=provider_model,
                        ))
                    context_retry["history_messages"] = len(history_for_attempt)
                    context_retry["disabled_sections"] = sorted(disabled_sections)
                    return response, request_diagnostic
                except ContextLengthError:
                    if (
                        self._context_compactor is not None
                        and not forced_compaction_attempted
                        and not react_messages
                    ):
                        forced_compaction_attempted = True
                        compacted = await compact_with_status(
                            estimated_tokens=estimate,
                            force=True,
                        )
                        if compacted and (self._surface_loader or self._history_loader) is not None:
                            history = (
                                await self._surface_loader(message.session_key)
                                if self._surface_loader
                                else await self._history_loader(message.session_key, None)
                            )
                            if self._surface_loader:
                                history = _drop_current_surface_suffix(
                                    history,
                                    context_frame=llm_context_frame,
                                    user_content=llm_user_content,
                                )
                            read_checkpoint = getattr(self._memory, "read_checkpoint_summary", None)
                            if callable(read_checkpoint) and self._surface_loader is None:
                                context.checkpoint_summary = str(
                                    read_checkpoint(message.session_key) or ""
                                )
                            continue
                    raise

        for iteration in range(1, self._max_iterations + 1):
            self._interrupt_snapshots[turn_id]["iteration"] = iteration
            await append_session_event(
                "step/start",
                iteration=iteration,
                data={"iteration": iteration},
                operation_suffix=f"{iteration}:step-start",
            )
            response, request_diagnostic = await chat_with_context_retry()
            llm_epoch_id = request_diagnostic.epoch_id
            append_turn_thinking(response.thinking)
            await self._record_session_usage(
                message.session_key,
                turn_id,
                iteration,
                response,
            )
            _log_prompt_cache_usage(
                session_key=message.session_key,
                iteration=iteration,
                prompt_tokens=response.cache_prompt_tokens,
                hit_tokens=response.cache_hit_tokens,
            )
            if self._prompt_cache_log is not None:
                try:
                    self._prompt_cache_log.write(
                        session_key=message.session_key,
                        turn_id=turn_id,
                        iteration=iteration,
                        prompt_tokens=response.cache_prompt_tokens,
                        hit_tokens=response.cache_hit_tokens,
                        diagnostics=request_diagnostic,
                    )
                except OSError as error:
                    # 缓存观测是辅助能力，磁盘只读或空间不足不能中断用户对话。
                    logger.warning(
                        "Prompt Cache 日志写入失败: session=%s error=%s",
                        message.session_key,
                        error,
                    )
            if not response.tool_calls:
                # 模型只输出了 thinking 但没有正文时，重试一次催出正式回复。
                content = str(response.content or "").strip()
                thinking = str(response.thinking or "")
                if not content and thinking:
                    logger.warning(
                        "空回复重试: session=%s iteration=%d content 为空但 thinking 非空",
                        message.session_key,
                        iteration,
                    )
                    empty_assistant = {"role": "assistant", "content": ""}
                    react_messages.append(empty_assistant)
                    llm_surface_messages.append(deepcopy(empty_assistant))
                    await append_surface_message(
                        empty_assistant,
                        iteration=iteration,
                        source_kind="assistant_empty",
                        operation_suffix="assistant-empty",
                    )
                    sync_surface_snapshot()
                    nudge_message = {
                        "role": "user",
                        "content": "你刚才只输出了思考过程，没有给出正式回复。请直接回复用户，不要重复思考。",
                    }
                    react_messages.append(nudge_message)
                    llm_surface_messages.append(deepcopy(nudge_message))
                    await append_surface_message(
                        nudge_message,
                        iteration=iteration,
                        source_kind="retry_nudge",
                        operation_suffix="retry-nudge",
                    )
                    sync_surface_snapshot()
                    retry_diagnostic: PromptCacheRequestDiagnostics | None = None

                    def on_retry_request(
                        sent_messages: list[dict[str, Any]],
                        sent_tools: list[dict[str, Any]],
                    ) -> None:
                        nonlocal retry_diagnostic, llm_epoch_id
                        retry_diagnostic = self._prompt_cache_diagnostics.observe(
                            message.session_key,
                            sent_messages,
                            sent_tools,
                            header=_request_header_identity(
                                self._provider,
                                sent_messages,
                                sent_tools,
                                tool_choice="none",
                                max_tokens=_SUMMARY_MAX_TOKENS,
                            ),
                            surface_seq=surface_tail_seq,
                        )
                        llm_epoch_id = retry_diagnostic.epoch_id
                        sync_surface_snapshot()

                    retry = await self._provider.chat(
                        [*last_base_messages, *react_messages],
                        tools=[],
                        tool_choice="none",
                        on_request=on_retry_request,
                    )
                    if retry_diagnostic is not None:
                        request_diagnostic = retry_diagnostic
                    else:
                        request_diagnostic = self._prompt_cache_diagnostics.observe(
                            message.session_key,
                            [*last_base_messages, *react_messages],
                            [],
                            header=_request_header_identity(
                                self._provider,
                                [*last_base_messages, *react_messages],
                                [],
                                tool_choice="none",
                                max_tokens=_SUMMARY_MAX_TOKENS,
                            ),
                            surface_seq=surface_tail_seq,
                        )
                        llm_epoch_id = request_diagnostic.epoch_id
                        sync_surface_snapshot()
                    if retry.content:
                        response = retry
                        append_turn_thinking(response.thinking)
                        content = str(retry.content or "").strip()
                    else:
                        logger.warning("空回复重试仍为空: session=%s", message.session_key)
                final_model_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": response.content or "",
                }
                final_model_message.update(dict(response.provider_fields))
                if response.thinking and "reasoning_content" not in final_model_message:
                    final_model_message["reasoning_content"] = response.thinking
                llm_surface_messages.append(final_model_message)
                await append_surface_message(
                    final_model_message,
                    iteration=iteration,
                    source_kind="assistant_final",
                    operation_suffix="assistant-final",
                )
                sync_surface_snapshot()
                await append_session_event(
                    "step/end",
                    iteration=iteration,
                    data={"status": "completed", "reason": "assistant_final"},
                    operation_suffix=f"{iteration}:step-end",
                )
                timing = await append_turn_end(iteration=iteration, status="completed")
                return PipelineResult(
                    content=str(content or ""),
                    thinking=merged_turn_thinking(),
                    # 终答轮思考：取当前 ``response.thinking``，可能来自原始终答 chat，
                    # 也可能来自上面"空回复重试"分支成功后的 retry chat。无论哪种情况，
                    # ``response`` 此刻都指向"最后一次 chat 调用"，其思考就是终答轮思考。
                    final_reasoning=str(response.thinking or ""),
                    tool_chain=tool_chain,
                    tools_used=list(dict.fromkeys(tools_used)),
                    context_retry=context_retry,
                    llm_user_content=deepcopy(llm_user_content),
                    llm_context_frame=llm_context_frame,
                    llm_message_timestamp=message_timestamp.isoformat(),
                    llm_epoch_id=llm_epoch_id,
                    llm_surface_messages=deepcopy(llm_surface_messages),
                    llm_surface_persisted=surface_persisted,
                    **timing,
                )

            # 工具调用 assistant 消息必须原样进入下一轮，尤其要保留 DeepSeek 的
            # reasoning_content；否则供应商会拒绝后续 tool 消息。
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
                    for call in response.tool_calls
                ],
                **response.provider_fields,
            }
            react_messages.append(assistant_message)
            llm_surface_messages.append(deepcopy(assistant_message))
            await append_surface_message(
                assistant_message,
                iteration=iteration,
                source_kind="assistant_tool_call",
                operation_suffix="assistant-tool-call",
            )
            sync_surface_snapshot()
            group: dict[str, Any] = {"iteration": iteration, "text": response.content or "", "calls": [], "provider_fields": dict(response.provider_fields)}
            for call in response.tool_calls:
                if call.name not in tool_view.visible_names:
                    result = normalize_tool_result(f"工具 '{call.name}' 未被当前执行上下文授权")
                    before_tool_messages = len(react_messages)
                    append_tool_result(react_messages, tool_call_id=call.id, content=result, tool_name=call.name)
                    llm_surface_messages.extend(
                        deepcopy(react_messages[before_tool_messages:])
                    )
                    for offset, tool_message in enumerate(react_messages[before_tool_messages:]):
                        await append_surface_message(
                            tool_message,
                            iteration=iteration,
                            source_kind="tool_result",
                            operation_suffix=f"tool-result-{call.id}-{offset}",
                        )
                    sync_surface_snapshot()
                    group["calls"].append({"call_id": call.id, "name": call.name, "arguments": dict(call.arguments), "result": result.text, "content_blocks": deepcopy(result.content_blocks), "status": "error"})
                    continue
                await append_session_event(
                    "tool/call",
                    iteration=iteration,
                    data={
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": deepcopy(call.arguments),
                    },
                    operation_suffix=f"{iteration}:tool-call:{call.id}",
                )
                if not suppress_stream:
                    # 先写入运行中快照，再发实时事件；刷新重连正好发生在事件前后时，订阅端都能恢复工具图标。
                    self._upsert_interrupt_tool(
                        turn_id,
                        call_id=call.id,
                        name=call.name,
                        arguments=dict(call.arguments),
                        status="running",
                        result_preview="",
                    )
                    await self._events.emit(ToolCallStarted(message.session_key, turn_id, call.id, call.name, dict(call.arguments)))
                execution_context: dict[str, Any] = tool_view.context
                execution_context.update({
                    "session_key": message.session_key,
                    "channel": message.channel,
                    "chat_id": message.chat_id,
                    "request_time": str(message.metadata.get("request_time") or message.metadata.get("received_at") or ""),
                })
                if call.name == "tool_search":
                    # 搜索工具需要知道哪些 Schema 已在当前 Turn 可见，但不能
                    # 持有 View 本身，否则状态会重新泄漏到全局工具实例。
                    execution_context["excluded_names"] = set(tool_view.visible_names)
                raw_result = await self._tools.execute(
                    call.name,
                    call.arguments,
                    context=execution_context,
                )
                result = normalize_tool_result(raw_result)
                if call.name == "tool_search":
                    try:
                        payload = json.loads(result.text)
                        unlocked = payload.get("unlocked", [])
                        if isinstance(unlocked, list):
                            tool_view.unlock(
                                name for name in unlocked if isinstance(name, str)
                            )
                    except (json.JSONDecodeError, AttributeError):
                        # 搜索结果异常只表示本次没有解锁，原工具结果仍完整交给模型。
                        logger.warning("tool_search 返回了无法解析的结果")
                status = "error" if result.text.startswith("工具执行出错:") or result.text.startswith("工具 '") else "ok"
                before_tool_messages = len(react_messages)
                append_tool_result(react_messages, tool_call_id=call.id, content=result, tool_name=call.name)
                llm_surface_messages.extend(
                    deepcopy(react_messages[before_tool_messages:])
                )
                for offset, tool_message in enumerate(react_messages[before_tool_messages:]):
                    await append_surface_message(
                        tool_message,
                        iteration=iteration,
                        source_kind="tool_result",
                        operation_suffix=f"tool-result-{call.id}-{offset}",
                    )
                sync_surface_snapshot()
                if not suppress_stream:
                    preview = result.preview()[:500]
                    # 完成态同样先落快照再发事件，避免刷新窗口里看到旧的 running 状态。
                    self._upsert_interrupt_tool(
                        turn_id,
                        call_id=call.id,
                        name=call.name,
                        arguments=dict(call.arguments),
                        status="error" if status == "error" else "completed",
                        result_preview=preview,
                    )
                    await self._events.emit(ToolCallCompleted(message.session_key, turn_id, call.id, call.name, status, preview))
                group["calls"].append({"call_id": call.id, "name": call.name, "arguments": dict(call.arguments), "result": result.text, "content_blocks": deepcopy(result.content_blocks), "status": status})
                tools_used.append(call.name)
                # 一组内后续工具也可能阻塞或被取消；每完成一个调用就刷新快照，避免丢失已完成结果。
                snapshot = self._interrupt_snapshots[turn_id]
                snapshot["tools_used"] = list(dict.fromkeys(tools_used))
                snapshot["tool_chain_partial"] = deepcopy([*tool_chain, group])
            tool_chain.append(group)
            await append_session_event(
                "step/end",
                iteration=iteration,
                data={"status": "completed", "reason": "tool_results"},
                operation_suffix=f"{iteration}:step-end",
            )
        # 达到最大迭代次数后生成阶段性进度总结，不直接崩溃。
        logger.warning(
            "ReAct 达到最大迭代次数: session=%s iteration=%d tools=%s",
            message.session_key,
            self._max_iterations,
            ", ".join(tools_used) if tools_used else "无",
        )
        summary = await self._summarize_incomplete_progress(
            last_base_messages, react_messages,
            reason="max_iterations",
            iteration=self._max_iterations,
            tools_used=tools_used,
        )
        timing = await append_turn_end(
            iteration=self._max_iterations,
            status="incomplete",
            reason="max_iterations",
        )
        return PipelineResult(
            content=summary,
            thinking=merged_turn_thinking(),
            # 达到最大迭代次数的 fallback 路径没有真正完成终答，没有单轮终答
            # 思考可记录。留空时持久化为空字符串，下次重建历史走 fallback 机制
            # 使用旧 ``reasoning_content`` 字段或 strategy 临场补齐 ``reasoning_content=""``。
            final_reasoning="",
            tool_chain=tool_chain,
            tools_used=list(dict.fromkeys(tools_used)),
            context_retry=context_retry,
            llm_user_content=deepcopy(llm_user_content),
            llm_context_frame=llm_context_frame,
            llm_message_timestamp=message_timestamp.isoformat(),
            llm_epoch_id=llm_epoch_id,
            llm_surface_messages=deepcopy(llm_surface_messages),
            llm_surface_persisted=surface_persisted,
            **timing,
        )

    def snapshot_interrupt_state(self, turn_id: str) -> dict[str, Any]:
        """返回当前 Turn 的隔离副本，避免取消后的清理影响中断状态。"""

        state = deepcopy(self._interrupt_snapshots.get(turn_id, {}))
        surface = state.get("llm_surface_messages")
        if not isinstance(surface, list):
            surface = []
            state["llm_surface_messages"] = surface
        partial_reply = str(state.get("partial_reply") or "")
        partial_thinking = str(state.get("partial_thinking") or "")
        if partial_reply or partial_thinking:
            # 流式取消时 Provider 尚未返回完整响应，仍保留已送达用户的模型前缀；
            # 未完成的工具调用继续由语义中断快照单独审计，不能伪造 tool result。
            if not surface or not isinstance(surface[-1], dict) or surface[-1].get("role") != "assistant":
                message: dict[str, Any] = {
                    "role": "assistant",
                    "content": partial_reply,
                }
                if partial_thinking:
                    message["reasoning_content"] = partial_thinking
                surface.append(message)
        return state

    def _upsert_interrupt_tool(
        self,
        turn_id: str,
        *,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
        status: str,
        result_preview: str,
    ) -> None:
        """维护当前进程内 running turn 的工具快照，供刷新/重连后补发 UI 状态。"""

        snapshot = self._interrupt_snapshots.get(turn_id)
        if snapshot is None:
            return
        tools = snapshot.setdefault("tools", [])
        item = {
            "call_id": call_id,
            "name": name,
            "arguments": deepcopy(arguments),
            "status": status,
            "result_preview": result_preview,
        }
        for index, existing in enumerate(tools):
            if existing.get("call_id") == call_id:
                tools[index] = item
                return
        tools.append(item)

    def discard_interrupt_snapshot(self, turn_id: str) -> None:
        """Turn 结束后幂等释放纯内存快照。"""

        self._interrupt_snapshots.pop(turn_id, None)

    async def _record_session_usage(
        self,
        session_key: str,
        turn_id: str,
        iteration: int,
        response: LLMResponse,
    ) -> None:
        usage = getattr(response, "usage", None)
        if not isinstance(usage, ProviderUsage):
            return
        await self._record_session_usage_values(session_key, turn_id, iteration, usage)

    async def _record_session_usage_values(
        self,
        session_key: str,
        turn_id: str,
        iteration: int,
        usage: ProviderUsage,
    ) -> None:
        """持久化一次模型调用的最新归一化 usage 样本。"""

        if self._session_usage_writer is None:
            return
        values = {
            "uncached_input_tokens": max(0, int(usage.uncached_input_tokens)),
            "cache_read_tokens": max(0, int(usage.cache_read_tokens)),
            "cache_write_tokens": max(0, int(usage.cache_write_tokens)),
            "output_tokens": max(0, int(usage.output_tokens)),
        }
        try:
            aggregate = await self._session_usage_writer(
                session_key, turn_id, iteration, values
            )
        except Exception as error:
            # 用量是观测面，持久化故障不能让已经成功的模型响应失败。
            logger.warning("写入会话模型用量失败 session=%s error=%s", session_key, error)
            return
        if not isinstance(aggregate, dict):
            return
        try:
            await self._events.emit(SessionUsageUpdated(
                session_key=session_key,
                turn_id=turn_id,
                total_uncached_input_tokens=int(aggregate.get("total_uncached_input_tokens") or 0),
                total_cache_read_tokens=int(aggregate.get("total_cache_read_tokens") or 0),
                total_cache_write_tokens=int(aggregate.get("total_cache_write_tokens") or 0),
                total_input_tokens=int(aggregate.get("total_input_tokens") or 0),
                cache_hit_rate=(
                    float(aggregate["cache_hit_rate"])
                    if aggregate.get("cache_hit_rate") is not None else None
                ),
                total_output_tokens=int(aggregate.get("total_output_tokens") or 0),
            ))
        except Exception:
            logger.exception("广播会话模型用量失败 session=%s", session_key)

    async def _load_context_measurement(self, session_key: str) -> dict[str, Any] | None:
        cached = self._context_measurements.get(session_key)
        if cached is not None:
            return dict(cached)
        if self._context_usage_loader is None:
            return None
        try:
            loaded = await self._context_usage_loader(session_key)
        except Exception as error:
            logger.warning("读取上下文计量快照失败 session=%s error=%s", session_key, error)
            return None
        if not isinstance(loaded, dict):
            return None
        snapshot = dict(loaded)
        self._context_measurements[session_key] = snapshot
        return dict(snapshot)

    async def _persist_context_measurement(
        self,
        session_key: str,
        snapshot: dict[str, Any],
    ) -> None:
        self._context_measurements[session_key] = dict(snapshot)
        if self._context_usage_writer is None:
            return
        try:
            await self._context_usage_writer(session_key, dict(snapshot))
        except Exception as error:
            # 计量快照是观察面，写入失败不能让当前模型请求失败；下次请求
            # 仍会用内存锚点继续投影，后台日志保留故障定位线索。
            logger.warning("写入上下文计量快照失败 session=%s error=%s", session_key, error)

    async def _summarize_incomplete_progress(
        self,
        base_messages: list[dict[str, Any]],
        react_messages: list[dict[str, Any]],
        *,
        reason: str,
        iteration: int,
        tools_used: list[str],
    ) -> str:
        """ReAct 达到上限时调 LLM 生成用户可读的阶段性进度总结。"""

        summary_prompt = (
            f"[收尾原因] {reason}\n"
            f"[已执行轮次] {iteration}\n"
            f"[已调用工具] {', '.join(tools_used[-8:]) if tools_used else '无'}\n\n"
            + _INCOMPLETE_SUMMARY_PROMPT
        )
        try:
            response = await self._provider.chat(
                [*base_messages, *react_messages,
                 {"role": "user", "content": summary_prompt}],
                tools=[],
                max_tokens=_SUMMARY_MAX_TOKENS,
                tool_choice="none",
            )
            text = str(response.content or "").strip()
            if text:
                return text
        except Exception as error:
            logger.warning("生成进度收尾总结失败: %s", error)
        # 模型收尾失败时返回固定兜底文案。
        tool_text = "、".join(tools_used[-8:]) if tools_used else "无"
        done = f"已尝试 {iteration} 轮，调用工具 {len(tools_used)} 次（{tool_text}）。"
        return (
            f"这次任务还没完全收束。{done}"
            "我先停在当前进度，后续会继续基于已有工具结果补齐缺失信息并给你最终结论。"
        )


def _log_prompt_cache_usage(
    *,
    session_key: str,
    iteration: int,
    prompt_tokens: int | None,
    hit_tokens: int | None,
) -> None:
    """记录供应商返回的 Prompt Cache 指标，不用缺失值伪造命中率。"""

    if prompt_tokens is None or hit_tokens is None:
        return
    rate = (hit_tokens / prompt_tokens * 100.0) if prompt_tokens > 0 else 0.0
    logger.info(
        "prompt_cache: session=%s iteration=%d hit=%d/%d rate=%.2f%%",
        session_key,
        iteration,
        hit_tokens,
        prompt_tokens,
        rate,
    )


def _provider_pressure_tokens(response: LLMResponse) -> int | None:
    """提取供应商 prompt-side usage；输出 token 不参与上下文压力。"""

    usage = getattr(response, "usage", None)
    if usage is not None:
        return max(0, int(usage.pressure_tokens))
    value = response.cache_prompt_tokens
    if value is None:
        return None
    return max(0, int(value))


def _drop_current_surface_suffix(
    history: list[dict[str, Any]],
    *,
    context_frame: str,
    user_content: object | None,
) -> list[dict[str, Any]]:
    """压缩重载 surface 后移除本轮已写入的尾部，避免组装器再次追加。"""

    current: list[dict[str, Any]] = []
    if context_frame:
        current.append({"role": "user", "content": context_frame})
    if user_content is not None:
        current.append({"role": "user", "content": deepcopy(user_content)})
    if not current or len(history) < len(current):
        return history
    if history[-len(current):] != current:
        return history
    return history[:-len(current)]


def _request_header_identity(
    provider: object,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    tool_choice: str,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """提取影响缓存域的 header；正文仍只通过消息哈希诊断。"""

    system = messages[0].get("content") if messages and messages[0].get("role") == "system" else ""
    configured_max_tokens = max_tokens
    if configured_max_tokens is None:
        configured_max_tokens = getattr(provider, "max_tokens", None)
    extra_body = getattr(provider, "_extra_body", {})
    return {
        "provider": str(getattr(provider, "provider_name", "") or ""),
        "model": str(getattr(provider, "model", "") or ""),
        "system": system,
        "tools": tools,
        "options": {
            "max_tokens": configured_max_tokens,
            "tool_choice": tool_choice,
            "extra_body": extra_body if isinstance(extra_body, dict) else {},
        },
    }


def _context_as_of_seq(message: InboundMessage) -> int | None:
    """从受控的当前用户 source_ref 推导快照所在消息序号。"""

    source_ref = str(message.metadata.get("current_user_source_ref") or "")
    try:
        return max(0, int(source_ref.rsplit(":", 1)[1]) - 1)
    except (IndexError, ValueError):
        return None


def _build_deferred_tools_hint(tools: Any, visible: set[str] | None = None) -> str:
    """构建未加载工具目录，让模型在调用 tool_search 前知道有哪些工具可用。

    对齐 Akashic 的 build_deferred_tools_hint() 行为：列出当前 Turn 不可见
    但已注册的工具名，并说明通过 tool_search 的加载方式。
    """

    if tools is None:
        return ""
    try:
        registered = getattr(tools, "get_registered_names", None)
        if callable(registered):
            all_names = set(registered())
        else:
            all_names = set(getattr(tools, "_tools", {}).keys())
    except Exception:
        return ""
    if not all_names:
        return ""
    visible_set = visible or set()
    deferred = [name for name in sorted(all_names) if name not in visible_set]
    if not deferred:
        return ""
    lines = ["【未加载工具目录（知道名字但 schema 未暴露）】"]
    lines.append(f"内置: {', '.join(deferred)}")
    lines.append(
        f"\n共 {len(deferred)} 个。优先使用当前活跃工具；只有当任务需要工具但当前活跃工具无法完成、且未加载目录中可能存在合适工具时，才调用 tool_search。\n"
        "如果当前工具已足够或任务无需工具，则不要调用 tool_search。\n"
        "加载方式：\n"
        "- 已知工具名 → tool_search(query=\"select:工具名\")，支持逗号分隔多个\n"
        "- 描述功能   → tool_search(query=\"关键词\") 搜索匹配"
    )
    return "\n".join(lines) + "\n\n"


__all__ = ["Pipeline"]
