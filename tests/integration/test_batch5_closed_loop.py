"""Batch 5 离线闭环：两轮历史、工具、记忆、事件与关闭。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.agent_loop import AgentLoop
from agent.channel import WebChannel
from agent.config_models import MemoryConfig
from agent.event_bus import EventBus
from agent.message_bus import InboundMessage, MessageBus
from agent.pipeline import Pipeline
from agent.prompt_assembler import MessageEnvelopeBuilder, PromptAssembler
from agent.prompt_block import SectionCache, SystemPromptBuilder, default_prompt_blocks
from agent.provider import LLMResponse, ToolCall
from memory.contracts import MemoryMutation, MemoryScope
from memory.engine import MemoryEngine
from session.manager import SessionManager
from tools.base import Tool
from tools.registry import ToolRegistry


class EchoTool(Tool):
    name = "echo"
    description = "回显文本"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    async def execute(self, text: str, **kwargs): return f"echo:{text}"


class Embedder:
    async def embed(self, text): return [1.0, 0.0]
    async def embed_batch(self, texts): return [[1.0, 0.0] for _ in texts]
    async def close(self): self.closed = True


class Provider:
    def __init__(self):
        self.chat_calls = 0
        self.pipeline_messages = []

    async def complete(self, messages, tools=None, **kwargs):
        prompt = messages[0]["content"]
        if "记忆检索决策器" in prompt:
            decision = "RETRIEVE" if "偏好" in prompt else "NO_RETRIEVE"
            return SimpleNamespace(content=f"<decision>{decision}</decision><history_query>回答偏好</history_query>")
        if "只输出一行 preference/procedure" in prompt:
            return SimpleNamespace(content="回答偏好")
        return SimpleNamespace(content="[]")

    async def chat(self, messages, tools=None, on_content_delta=None, **kwargs):
        self.chat_calls += 1
        self.pipeline_messages.append(list(messages))
        if self.chat_calls == 1:
            return LLMResponse(None, [ToolCall("call-1", "echo", {"text": "第一轮"})])
        content = "第一轮完成" if self.chat_calls == 2 else "记得你偏好中文"
        if on_content_delta:
            await on_content_delta({"content_delta": content})
        return LLMResponse(content)


class WebSocket:
    def __init__(self): self.frames = []
    async def send_json(self, frame): self.frames.append(frame)


@pytest.mark.asyncio
async def test_two_turn_closed_loop_restores_history_and_memory(tmp_path: Path) -> None:
    bus = MessageBus()
    events = EventBus()
    sessions = SessionManager(tmp_path)
    provider = Provider()
    embedder = Embedder()
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    memory = MemoryEngine(tmp_path, embedder, provider, sessions.store, config=config, consolidation_threshold=20)
    memory.bind_events(events)
    tools = ToolRegistry()
    tools.register(EchoTool())
    assembler = PromptAssembler(SystemPromptBuilder(default_prompt_blocks(), SectionCache()), MessageEnvelopeBuilder())
    pipeline = Pipeline(provider, tools, events, assembler, workspace=str(tmp_path), memory=memory, history_loader=sessions.load_history)
    loop = AgentLoop(bus, events, pipeline, sessions)
    channel = WebChannel(bus, events, loop)
    websocket = WebSocket()

    try:
        await channel.handle_frame(websocket, {"type": "message.send", "request_id": "r1", "session_id": "web:c", "text": "执行第一轮"})
        await loop.run_once()
        await memory.drain()
        await memory.mutate(MemoryMutation(
            kind="remember", summary="用户偏好中文回答", memory_kind="preference",
            source_ref="web:c:0", scope=MemoryScope(channel="web", chat_id="c"),
        ))
        await channel.handle_frame(websocket, {"type": "message.send", "request_id": "r2", "session_id": "web:c", "text": "你记得我的回答偏好吗"})
        await loop.run_once()
        await memory.drain()

        rows = sessions.store.fetch_session_messages("web:c")
        second_prompt = provider.pipeline_messages[-1]
        assert [row["role"] for row in rows] == ["user", "assistant", "user", "assistant"]
        assert rows[1]["tool_chain"][0]["calls"][0]["name"] == "echo"
        assert any(item.get("role") == "tool" and "echo:第一轮" in item.get("content", "") for item in second_prompt)
        assert any("用户偏好中文回答" in str(item.get("content")) for item in second_prompt)
        assert any(frame["type"] == "react.tool.completed" for frame in websocket.frames)
        assert [frame["request_id"] for frame in websocket.frames if frame["type"] == "message.final"] == ["r1", "r2"]
    finally:
        await channel.close()
        await memory.close()
        await sessions.close()

    assert embedder.closed is True
