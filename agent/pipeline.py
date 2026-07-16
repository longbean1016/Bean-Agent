"""记忆检索、Prompt 组装、LLM ReAct 与工具执行流水线。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from agent.event_bus import EventBus, StreamDeltaReady, ToolCallCompleted, ToolCallStarted
from agent.message_bus import InboundMessage, PipelineResult
from agent.prompt_assembler import PromptAssembler
from agent.prompt_block import TurnContext
from agent.provider import LLMResponse
from tools.base import normalize_tool_result
from tools.registry import ToolRegistry
from tools.runtime import append_tool_result


class ProviderApi(Protocol):
    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> LLMResponse: ...


HistoryLoader = Callable[[str, int], Awaitable[list[dict[str, Any]]]]


class Pipeline:
    """执行单个 Turn 的纯推理部分，不负责 Session 持久化或最终出站。"""

    def __init__(self, provider: ProviderApi, tools: ToolRegistry, event_bus: EventBus, assembler: PromptAssembler, *, workspace: str, memory: Any | None = None, history_loader: HistoryLoader | None = None, history_limit: int = 40, max_iterations: int = 10) -> None:
        self._provider = provider
        self._tools = tools
        self._events = event_bus
        self._assembler = assembler
        self._workspace = workspace
        self._memory = memory
        self._history_loader = history_loader
        self._history_limit = max(0, int(history_limit))
        self._max_iterations = max(1, int(max_iterations))

    async def process(self, message: InboundMessage, *, turn_id: str) -> PipelineResult:
        self._tools.set_context(channel=message.channel, chat_id=message.chat_id, session_key=message.session_key)
        history = await self._history_loader(message.session_key, self._history_limit) if self._history_loader else []
        retrieved = await self._memory.retrieve_for_turn(message) if self._memory else ""
        names = sorted(self._tools.get_registered_names())
        summary = "\n".join(
            f"- {name}: {tool.description}"
            for name in names
            if (tool := self._tools.get_tool(name)) is not None
        )
        context = TurnContext(self._workspace, message.channel, message.chat_id, self._memory, retrieved, summary, names)
        assembled = self._assembler.assemble(turn_ctx=context, history=history, current_message=message.content)
        model_messages = list(assembled.messages)
        tool_chain: list[dict[str, Any]] = []
        tools_used: list[str] = []

        async def on_delta(delta: dict[str, str]) -> None:
            await self._events.emit(StreamDeltaReady(
                session_key=message.session_key,
                turn_id=turn_id,
                content_delta=str(delta.get("content_delta") or ""),
                thinking_delta=str(delta.get("thinking_delta") or ""),
            ))

        for iteration in range(1, self._max_iterations + 1):
            response = await self._provider.chat(
                model_messages,
                self._tools.get_schemas(),
                tool_choice="auto",
                on_content_delta=on_delta,
            )
            if not response.tool_calls:
                return PipelineResult(
                    content=str(response.content or ""),
                    thinking=str(response.thinking or ""),
                    tool_chain=tool_chain,
                    tools_used=list(dict.fromkeys(tools_used)),
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
            model_messages.append(assistant_message)
            group: dict[str, Any] = {"iteration": iteration, "text": response.content or "", "calls": [], "provider_fields": dict(response.provider_fields)}
            for call in response.tool_calls:
                await self._events.emit(ToolCallStarted(message.session_key, turn_id, call.id, call.name, dict(call.arguments)))
                raw_result = await self._tools.execute(call.name, call.arguments)
                result = normalize_tool_result(raw_result)
                status = "error" if result.text.startswith("工具执行出错:") or result.text.startswith("工具 '") else "ok"
                append_tool_result(model_messages, tool_call_id=call.id, content=result, tool_name=call.name)
                await self._events.emit(ToolCallCompleted(message.session_key, turn_id, call.id, call.name, status, result.preview()[:500]))
                group["calls"].append({"call_id": call.id, "name": call.name, "arguments": dict(call.arguments), "result": result.text, "status": status})
                tools_used.append(call.name)
            tool_chain.append(group)
        raise RuntimeError(f"ReAct 超过最大迭代次数: {self._max_iterations}")


__all__ = ["Pipeline"]
