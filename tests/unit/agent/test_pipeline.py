"""Pipeline 的记忆检索、ReAct 工具续轮和事件测试。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from agent.event_bus import EventBus, StreamDeltaReady, ToolCallCompleted, ToolCallStarted
from agent.message_bus import InboundMessage
from agent.pipeline import Pipeline
from agent.prompt_assembler import MessageEnvelopeBuilder, PromptAssembler
from agent.prompt_block import SectionCache, SystemPromptBuilder, default_prompt_blocks
from agent.provider import LLMResponse, ToolCall
from tools.base import Tool
from tools.registry import ToolRegistry


class EchoTool(Tool):
    name = "echo"
    description = "回显文本"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    async def execute(self, text: str, **kwargs): return f"echo:{text}:{kwargs['session_key']}"


class Provider:
    def __init__(self) -> None:
        self.calls = 0
        self.messages = []

    async def chat(self, messages, tools=None, on_content_delta=None, **kwargs):
        self.calls += 1
        self.messages.append(list(messages))
        if self.calls == 1:
            return LLMResponse(None, [ToolCall("call-1", "echo", {"text": "hi"})], provider_fields={"reasoning_content": "想一想"})
        if on_content_delta:
            await on_content_delta({"content_delta": "完成"})
        return LLMResponse("完成", thinking="推理")


class Memory:
    async def retrieve_for_turn(self, message): return "用户偏好简洁"
    def read_self(self): return ""
    def get_memory_context(self): return ""
    def read_recent_context(self): return ""


@pytest.mark.asyncio
async def test_pipeline_runs_tool_loop_and_emits_lifecycle_events() -> None:
    tools = ToolRegistry()
    tools.register(EchoTool())
    events = EventBus()
    seen = []
    for event_type in (ToolCallStarted, ToolCallCompleted, StreamDeltaReady):
        events.on(event_type, lambda event: seen.append(event))
    provider = Provider()
    assembler = PromptAssembler(SystemPromptBuilder(default_prompt_blocks(), SectionCache()), MessageEnvelopeBuilder())
    pipeline = Pipeline(provider, tools, events, assembler, workspace="D:/workspace", memory=Memory(), history_loader=lambda key, limit: _history())

    result = await pipeline.process(InboundMessage(channel="web", sender="u", chat_id="c", content="执行"), turn_id="t1")

    assert result.content == "完成"
    assert result.thinking == "推理"
    assert result.tools_used == ["echo"]
    assert result.tool_chain[0]["calls"][0]["result"] == "echo:hi:web:c"
    assert any(isinstance(event, ToolCallStarted) for event in seen)
    assert any(isinstance(event, ToolCallCompleted) for event in seen)
    assert any(isinstance(event, StreamDeltaReady) for event in seen)
    assert provider.messages[1][-1]["role"] == "tool"


async def _history():
    return [{"role": "assistant", "content": "旧回答"}]


@pytest.mark.asyncio
async def test_pipeline_includes_text_and_image_attachments_in_current_turn(
    tmp_path: Path,
) -> None:
    class FinalProvider:
        def __init__(self) -> None:
            self.messages = []

        async def chat(self, messages, tools=None, **kwargs):
            self.messages = messages
            return LLMResponse("已读取")

    text_path = tmp_path / "notes.txt"
    text_path.write_text("附件中的关键内容", encoding="utf-8")
    image_path = tmp_path / "sample.png"
    buffer = BytesIO()
    Image.new("RGB", (3, 3), "#0f766e").save(buffer, format="PNG")
    image_path.write_bytes(buffer.getvalue())
    provider = FinalProvider()
    pipeline = Pipeline(
        provider,
        ToolRegistry(),
        EventBus(),
        PromptAssembler(
            SystemPromptBuilder(default_prompt_blocks(), SectionCache()),
            MessageEnvelopeBuilder(),
        ),
        workspace=str(tmp_path),
    )

    await pipeline.process(
        InboundMessage(
            channel="web",
            sender="u",
            chat_id="c",
            content="分析附件",
            media=[str(text_path), str(image_path)],
        ),
        turn_id="t-attachment",
    )

    current = provider.messages[-1]["content"]
    assert isinstance(current, list)
    assert current[0]["type"] == "image_url"
    assert current[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "附件中的关键内容" in current[-1]["text"]
    assert "分析附件" in current[-1]["text"]
