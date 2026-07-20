"""记忆检索、Prompt 组装、LLM ReAct 与工具执行流水线。"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from agent.attachment_content import build_current_user_content
from agent.context_budget import DEFAULT_CONTEXT_RETRY_PLANS, slice_complete_turns
from agent.event_bus import EventBus, StreamDeltaReady, ToolCallCompleted, ToolCallStarted
from agent.message_bus import InboundMessage, PipelineResult
from agent.prompt_assembler import PromptAssembler
from agent.prompt_block import TurnContext
from agent.prompt_cache_log import PromptCacheLogWriter
from agent.provider import ContextLengthError, LLMResponse
from agent.skills import SkillsLoader, collect_skill_mentions
from agent.tool_runtime import ToolRuntimeView
from tools.base import normalize_tool_result
from tools.registry import ToolRegistry
from tools.runtime import append_tool_result


class ProviderApi(Protocol):
    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> LLMResponse: ...


HistoryLoader = Callable[[str, int], Awaitable[list[dict[str, Any]]]]

logger = logging.getLogger(__name__)

_LOCAL_TZ = ZoneInfo("Asia/Shanghai")


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
        history_limit: int = 40,
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
        self._history_loader = history_loader
        self._history_limit = max(0, int(history_limit))
        self._max_iterations = max(1, int(max_iterations))
        # 图片能力在应用组装时确定，Turn 内只读取这份稳定快照。纯文本主模型不能
        # 接收 image_url；独立 VL 可用时则由现有 ReAct 工具链负责真正读图。
        self._multimodal = bool(multimodal)
        self._vl_available = bool(vl_available)
        # 中断快照只服务于当前进程内的停止/续跑语义，不进入 Session 或长期记忆。
        self._interrupt_snapshots: dict[str, dict[str, Any]] = {}

    async def process(self, message: InboundMessage, *, turn_id: str) -> PipelineResult:
        self._interrupt_snapshots[turn_id] = {
            "partial_reply": "",
            "partial_thinking": "",
            "tools_used": [],
            "tool_chain_partial": [],
        }
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
        history = (
            await self._history_loader(message.session_key, self._history_limit)
            if self._history_loader and not skip_history
            else []
        )
        retrieved = await self._memory.retrieve_for_turn(message) if self._memory and not skip_memory else ""
        names = list(tool_view.visible_order)
        available_skills = (
            [record.name for record in self._skills.list_skill_records()]
            if self._skills
            else []
        )
        active_skills = collect_skill_mentions(message.content, available_skills)
        context = TurnContext(
            workspace=self._workspace,
            channel=message.channel,
            chat_id=message.chat_id,
            memory=None if skip_memory else self._memory,
            retrieved_memory_block=retrieved,
            active_tool_names=names,
            skills=self._skills,
            active_skill_names=active_skills,
        )
        current_content = await build_current_user_content(
            message.content,
            message.media,
            multimodal=self._multimodal,
            vl_available=self._vl_available,
        )
        message_timestamp = datetime.now(_LOCAL_TZ)
        retry_plan_index = 0
        context_retry: dict[str, Any] = {
            "attempts": [],
            "selected_plan": None,
            "history_messages": len(history),
            "disabled_sections": [],
        }
        react_messages: list[dict[str, Any]] = []
        tool_chain: list[dict[str, Any]] = []
        tools_used: list[str] = []

        async def on_delta(delta: dict[str, str]) -> None:
            snapshot = self._interrupt_snapshots[turn_id]
            snapshot["partial_reply"] += str(delta.get("content_delta") or "")
            snapshot["partial_thinking"] += str(delta.get("thinking_delta") or "")
            if suppress_stream:
                return
            await self._events.emit(StreamDeltaReady(
                session_key=message.session_key,
                turn_id=turn_id,
                content_delta=str(delta.get("content_delta") or ""),
                thinking_delta=str(delta.get("thinking_delta") or ""),
            ))

        async def chat_with_context_retry() -> LLMResponse:
            nonlocal retry_plan_index
            while True:
                plan = DEFAULT_CONTEXT_RETRY_PLANS[retry_plan_index]
                history_for_attempt = slice_complete_turns(history, plan.history_ratio)
                disabled_sections = set(plan.drop_sections)
                context_retry["attempts"].append(
                    {
                        "name": plan.name,
                        "history_messages": len(history_for_attempt),
                        "disabled_sections": sorted(disabled_sections),
                    }
                )
                assembled = self._assembler.assemble(
                    turn_ctx=context,
                    history=history_for_attempt,
                    current_message=current_content,
                    message_timestamp=message_timestamp,
                    disabled_sections=disabled_sections,
                )
                # ReAct 后缀可能包含已经执行过的工具结果。超限重试只重建基础
                # Prompt，原样保留该后缀，避免重复执行有副作用的工具。
                model_messages = [*assembled.messages, *react_messages]
                try:
                    response = await self._provider.chat(
                        model_messages,
                        self._tools.get_schemas(
                            visible_names=tool_view.visible_names,
                            visible_order=tool_view.visible_order,
                        ),
                        tool_choice="auto",
                        on_content_delta=on_delta,
                    )
                    context_retry["selected_plan"] = plan.name
                    context_retry["history_messages"] = len(history_for_attempt)
                    context_retry["disabled_sections"] = sorted(disabled_sections)
                    return response
                except ContextLengthError:
                    if retry_plan_index >= len(DEFAULT_CONTEXT_RETRY_PLANS) - 1:
                        raise
                    retry_plan_index += 1
                    next_plan = DEFAULT_CONTEXT_RETRY_PLANS[retry_plan_index]
                    logger.info(
                        "上下文超限，切换退避计划: session=%s plan=%s",
                        message.session_key,
                        next_plan.name,
                    )

        for iteration in range(1, self._max_iterations + 1):
            response = await chat_with_context_retry()
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
                    )
                except OSError as error:
                    # 缓存观测是辅助能力，磁盘只读或空间不足不能中断用户对话。
                    logger.warning(
                        "Prompt Cache 日志写入失败: session=%s error=%s",
                        message.session_key,
                        error,
                    )
            if not response.tool_calls:
                return PipelineResult(
                    content=str(response.content or ""),
                    thinking=str(response.thinking or ""),
                    tool_chain=tool_chain,
                    tools_used=list(dict.fromkeys(tools_used)),
                    context_retry=context_retry,
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
            group: dict[str, Any] = {"iteration": iteration, "text": response.content or "", "calls": [], "provider_fields": dict(response.provider_fields)}
            for call in response.tool_calls:
                if call.name not in tool_view.visible_names:
                    result = normalize_tool_result(f"工具 '{call.name}' 未被当前执行上下文授权")
                    append_tool_result(react_messages, tool_call_id=call.id, content=result, tool_name=call.name)
                    group["calls"].append({"call_id": call.id, "name": call.name, "arguments": dict(call.arguments), "result": result.text, "status": "error"})
                    continue
                if not suppress_stream:
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
                append_tool_result(react_messages, tool_call_id=call.id, content=result, tool_name=call.name)
                if not suppress_stream:
                    await self._events.emit(ToolCallCompleted(message.session_key, turn_id, call.id, call.name, status, result.preview()[:500]))
                group["calls"].append({"call_id": call.id, "name": call.name, "arguments": dict(call.arguments), "result": result.text, "status": status})
                tools_used.append(call.name)
                # 一组内后续工具也可能阻塞或被取消；每完成一个调用就刷新快照，避免丢失已完成结果。
                snapshot = self._interrupt_snapshots[turn_id]
                snapshot["tools_used"] = list(dict.fromkeys(tools_used))
                snapshot["tool_chain_partial"] = deepcopy([*tool_chain, group])
            tool_chain.append(group)
        raise RuntimeError(f"ReAct 超过最大迭代次数: {self._max_iterations}")

    def snapshot_interrupt_state(self, turn_id: str) -> dict[str, Any]:
        """返回当前 Turn 的隔离副本，避免取消后的清理影响中断状态。"""

        return deepcopy(self._interrupt_snapshots.get(turn_id, {}))

    def discard_interrupt_snapshot(self, turn_id: str) -> None:
        """Turn 结束后幂等释放纯内存快照。"""

        self._interrupt_snapshots.pop(turn_id, None)


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


__all__ = ["Pipeline"]
