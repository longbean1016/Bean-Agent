"""模型侧历史投影的跨 Turn 连续性测试。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent.agent_loop import AgentLoop
from agent.event_bus import EventBus
from agent.message_bus import InboundMessage, MessageBus
from agent.pipeline import Pipeline
from agent.prompt_assembler import MessageEnvelopeBuilder, PromptAssembler
from agent.prompt_block import SectionCache, SystemPromptBuilder, default_prompt_blocks
from agent.provider import LLMResponse, ToolCall
from session.manager import SessionManager
from tools.base import Tool
from tools.registry import ToolRegistry


class EchoTool(Tool):
    name = "echo"
    description = "回显文本"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, text: str, **kwargs: Any) -> str:
        return text


class CapturingProvider:
    model = "cache-test"
    provider_name = "fake"
    context_window = 100_000
    max_tokens = 100

    def __init__(self) -> None:
        self.messages: list[list[dict[str, Any]]] = []

    async def chat(self, messages, tools=None, **kwargs):
        self.messages.append(deepcopy(messages))
        return LLMResponse("回答")


class ToolProvider(CapturingProvider):
    async def chat(self, messages, tools=None, **kwargs):
        self.messages.append(deepcopy(messages))
        if len(self.messages) == 1:
            return LLMResponse(
                None,
                [ToolCall("call-1", "echo", {"text": "工具输入"})],
            )
        return LLMResponse("工具后回答")


@pytest.mark.asyncio
async def test_next_turn_reuses_saved_frame_and_model_user_prefix(
    tmp_path: Path,
) -> None:
    sessions = SessionManager(tmp_path)
    provider = CapturingProvider()
    tools = ToolRegistry()
    tools.register(EchoTool())
    pipeline = Pipeline(
        provider,
        tools,
        EventBus(),
        PromptAssembler(
            SystemPromptBuilder(default_prompt_blocks(), SectionCache()),
            MessageEnvelopeBuilder(),
        ),
        workspace=str(tmp_path),
        history_loader=sessions.load_history,
    )
    bus = MessageBus()
    loop = AgentLoop(bus, EventBus(), pipeline, sessions)

    await bus.publish_inbound(InboundMessage("web", "u", "c", "第一轮"))
    await loop.run_once()
    first_request = provider.messages[0]

    await bus.publish_inbound(InboundMessage("web", "u", "c", "第二轮"))
    await loop.run_once()
    second_request = provider.messages[1]

    assert second_request[: len(first_request)] == first_request
    assert [item["role"] for item in second_request] == [
        "system",
        "user",
        "user",
        "assistant",
        "user",
        "user",
    ]
    assert second_request[1] == first_request[-2]
    assert second_request[2] == first_request[-1]
    assert second_request[3] == {"role": "assistant", "content": "回答"}
    rows = sessions.store.fetch_session_messages("web:c")
    assert [row["role"] for row in rows] == ["user", "assistant", "user", "assistant"]
    assert rows[0]["llm_context_frame"].startswith("<system-reminder")
    assert rows[0]["llm_user_content"] == first_request[-1]["content"]
    assert rows[0]["llm_surface_messages"] == [
        *first_request[1:],
        {"role": "assistant", "content": "回答"},
    ]
    await sessions.close()


@pytest.mark.asyncio
async def test_react_surface_replays_tool_messages_without_semantic_duplication(
    tmp_path: Path,
) -> None:
    sessions = SessionManager(tmp_path)
    provider = ToolProvider()
    tools = ToolRegistry()
    tools.register(EchoTool())
    pipeline = Pipeline(
        provider,
        tools,
        EventBus(),
        PromptAssembler(
            SystemPromptBuilder(default_prompt_blocks(), SectionCache()),
            MessageEnvelopeBuilder(),
        ),
        workspace=str(tmp_path),
        history_loader=sessions.load_history,
    )
    bus = MessageBus()
    loop = AgentLoop(bus, EventBus(), pipeline, sessions)

    await bus.publish_inbound(InboundMessage("web", "u", "c", "调用工具"))
    await loop.run_once()
    first_turn_final_request = provider.messages[1]
    rows = sessions.store.fetch_session_messages("web:c")
    assert len(rows[0]["llm_surface_messages"]) == 5

    await bus.publish_inbound(InboundMessage("web", "u", "c", "继续"))
    await loop.run_once()
    second_turn_request = provider.messages[2]

    assert second_turn_request[: len(first_turn_final_request) + 1] == [
        *first_turn_final_request,
        {"role": "assistant", "content": "工具后回答"},
    ]
    assert [row["role"] for row in sessions.store.fetch_session_messages("web:c")] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    await sessions.close()
