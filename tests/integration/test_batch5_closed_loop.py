"""Batch 5 离线闭环：两轮历史、工具、记忆、事件与关闭。"""

from __future__ import annotations

import asyncio
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
from agent.skills import SkillsLoader
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
async def test_web_concurrent_queue_cancel_and_promotion_closed_loop(
    tmp_path: Path,
) -> None:
    """真实总线和 WebChannel 必须保持排队位置、取消与晋升事件顺序。"""

    class BlockingPipeline:
        def __init__(self) -> None:
            self.started: dict[str, asyncio.Event] = {}
            self.release: dict[str, asyncio.Event] = {}

        def started_event(self, chat_id: str) -> asyncio.Event:
            return self.started.setdefault(chat_id, asyncio.Event())

        def release_event(self, chat_id: str) -> asyncio.Event:
            return self.release.setdefault(chat_id, asyncio.Event())

        async def process(self, message, *, turn_id):
            self.started_event(message.chat_id).set()
            await self.release_event(message.chat_id).wait()
            return SimpleNamespace(
                content=f"完成-{message.chat_id}",
                thinking="",
                # 与 PipelineResult 字段对齐：终答轮思考留空，agent_loop 持久化时写入空串。
                final_reasoning="",
                media=[],
                tool_chain=[],
                tools_used=[],
            )

    bus = MessageBus()
    events = EventBus()
    sessions = SessionManager(tmp_path)
    pipeline = BlockingPipeline()
    loop = AgentLoop(
        bus,
        events,
        pipeline,
        sessions,
        max_concurrent_turns=1,
        max_queued_turns=2,
    )
    channel = WebChannel(bus, events, loop)
    websocket = WebSocket()
    runner = asyncio.create_task(loop.run())
    try:
        for chat_id in ("a", "b", "c"):
            await channel.handle_frame(websocket, {
                "type": "message.send",
                "request_id": f"r-{chat_id}",
                "session_id": f"web:{chat_id}",
                "text": f"问题-{chat_id}",
            })
        await asyncio.wait_for(pipeline.started_event("a").wait(), timeout=1)
        await asyncio.sleep(0)

        queued = [frame for frame in websocket.frames if frame["type"] == "turn.queued"]
        assert [(frame["session_id"], frame["position"]) for frame in queued[-2:]] == [
            ("web:b", 1),
            ("web:c", 2),
        ]

        await channel.handle_frame(websocket, {
            "type": "turn.stop",
            "request_id": "stop-b",
            "session_id": "web:b",
        })
        assert websocket.frames[-1]["status"] == "cancelled"
        assert any(
            frame["type"] == "turn.queued"
            and frame["session_id"] == "web:c"
            and frame["position"] == 1
            for frame in websocket.frames
        )

        pipeline.release_event("a").set()
        await asyncio.wait_for(pipeline.started_event("c").wait(), timeout=1)
        pipeline.release_event("c").set()
        await asyncio.sleep(0.05)

        assert any(
            frame["type"] == "turn.started" and frame["session_id"] == "web:c"
            for frame in websocket.frames
        )
        assert any(
            frame["type"] == "message.final"
            and frame["session_id"] == "web:c"
            and frame["content"] == "完成-c"
            for frame in websocket.frames
        )
    finally:
        for event in pipeline.release.values():
            event.set()
        await loop.close()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await channel.close()
        await sessions.close()


@pytest.mark.asyncio
async def test_web_message_explicit_builtin_skill_uses_dynamic_context(
    tmp_path: Path,
) -> None:
    """Web 消息显式命中 Skill 时，正文只进入本轮动态上下文。"""

    class SkillProvider:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []

        async def chat(self, messages, tools=None, on_content_delta=None, **kwargs):
            self.messages = list(messages)
            return LLMResponse("天气查询已准备")

    bus = MessageBus()
    events = EventBus()
    sessions = SessionManager(tmp_path)
    provider = SkillProvider()
    pipeline = Pipeline(
        provider,
        ToolRegistry(),
        events,
        PromptAssembler(
            SystemPromptBuilder(default_prompt_blocks(), SectionCache()),
            MessageEnvelopeBuilder(),
        ),
        workspace=str(tmp_path),
        skills=SkillsLoader(tmp_path),
        history_loader=sessions.load_history,
    )
    loop = AgentLoop(bus, events, pipeline, sessions)
    channel = WebChannel(bus, events, loop)
    websocket = WebSocket()

    try:
        await channel.handle_frame(
            websocket,
            {
                "type": "message.send",
                "request_id": "skill-r1",
                "session_id": "web:skills",
                "text": "$weather 查询上海天气",
            },
        )
        await loop.run_once()

        system_prompt = str(provider.messages[0]["content"])
        dynamic_frame = str(provider.messages[-2]["content"])
        assert '<skill name="weather"' in system_prompt
        assert "Get current weather and short-term forecasts" in system_prompt
        assert "wttr.in（首选）" not in system_prompt
        assert dynamic_frame.startswith(
            '<system-reminder data-system-context-frame="true">'
        )
        assert "### Skill: weather" in dynamic_frame
        assert "wttr.in（首选）" in dynamic_frame
        assert provider.messages[-1]["role"] == "user"
        assert str(provider.messages[-1]["content"]).endswith("$weather 查询上海天气")
        assert any(
            frame["type"] == "message.final"
            and frame["request_id"] == "skill-r1"
            for frame in websocket.frames
        )
    finally:
        await channel.close()
        await sessions.close()


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
        listed, _ = sessions.store.list_chat_sessions(channel="web")
        second_prompt = provider.pipeline_messages[-1]
        assert [row["role"] for row in rows] == ["user", "assistant", "user", "assistant"]
        assert listed[0]["title"] == "执行第一轮"
        assert rows[1]["tool_chain"][0]["calls"][0]["name"] == "echo"
        assert any(item.get("role") == "tool" and "echo:第一轮" in item.get("content", "") for item in second_prompt)
        assert any("用户偏好中文回答" in str(item.get("content")) for item in second_prompt)
        frame_index = next(
            index
            for index, item in enumerate(second_prompt)
            if str(item.get("content", "")).startswith(
                '<system-reminder data-system-context-frame="true">'
            )
        )
        tool_index = next(
            index for index, item in enumerate(second_prompt)
            if item.get("role") == "tool"
        )
        assert second_prompt[0]["role"] == "system"
        assert tool_index < frame_index == len(second_prompt) - 2
        assert second_prompt[-1]["role"] == "user"
        assert str(second_prompt[-1]["content"]).endswith("\n你记得我的回答偏好吗")
        assert any(frame["type"] == "react.tool.completed" for frame in websocket.frames)
        assert [frame["request_id"] for frame in websocket.frames if frame["type"] == "message.final"] == ["r1", "r2"]
    finally:
        await channel.close()
        await memory.close()
        await sessions.close()

    assert embedder.closed is True


@pytest.mark.asyncio
async def test_interrupted_turn_is_expanded_into_next_provider_messages(
    tmp_path: Path,
) -> None:
    class InterruptProvider:
        def __init__(self) -> None:
            self.calls = 0
            self.waiting = asyncio.Event()
            self.messages: list[list[dict[str, object]]] = []

        async def chat(self, messages, tools=None, on_content_delta=None, **kwargs):
            self.calls += 1
            self.messages.append(list(messages))
            if self.calls == 1:
                return LLMResponse(
                    None,
                    [ToolCall("interrupted-call", "echo", {"text": "已读取内容"})],
                )
            if self.calls == 2:
                self.waiting.set()
                await asyncio.Event().wait()
            return LLMResponse("继续后的完整回答")

    bus = MessageBus()
    events = EventBus()
    sessions = SessionManager(tmp_path)
    provider = InterruptProvider()
    tools = ToolRegistry()
    tools.register(EchoTool())
    pipeline = Pipeline(
        provider,
        tools,
        events,
        PromptAssembler(
            SystemPromptBuilder(default_prompt_blocks(), SectionCache()),
            MessageEnvelopeBuilder(),
        ),
        workspace=str(tmp_path),
        history_loader=sessions.load_history,
    )
    loop = AgentLoop(bus, events, pipeline, sessions)

    try:
        await bus.publish_inbound(
            InboundMessage("web", "u", "c", "读取文件并分析")
        )
        interrupted_turn = asyncio.create_task(loop.run_once())
        await provider.waiting.wait()
        assert (await loop.request_interrupt("web:c")).status == "interrupted"
        await interrupted_turn

        # 中断完成后立即落库，刷新或进程退出不应丢失这一轮。
        interrupted_rows = sessions.store.fetch_session_messages("web:c")
        assert [row["content"] for row in interrupted_rows] == ["读取文件并分析", "[用户已停止生成]"]
        await bus.publish_inbound(InboundMessage("web", "u", "c", "继续"))
        await loop.run_once()

        model_messages = provider.messages[-1]
        history = [item for item in model_messages if item.get("role") != "system"]
        assert history[0] == {"role": "user", "content": "读取文件并分析"}
        assert history[1]["role"] == "assistant"
        assert history[1]["tool_calls"][0]["function"]["name"] == "echo"
        assert history[2] == {
            "role": "tool",
            "tool_call_id": "interrupted-call",
            "content": "echo:已读取内容",
        }
        assert history[3]["role"] == "assistant"
        assert history[3]["content"] == "[用户已停止生成]"
        assert history[-1]["role"] == "user"
        assert str(history[-1]["content"]).endswith("\n继续")
    finally:
        await sessions.close()
