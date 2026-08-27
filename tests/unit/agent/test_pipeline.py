"""Pipeline 的记忆检索、ReAct 工具续轮和事件测试。"""

from __future__ import annotations

import asyncio
import json
import logging
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from agent.attachment_content import build_current_user_content
from agent.event_bus import (
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ContextCompactionStarted,
    EventBus,
    StreamDeltaReady,
    ToolCallCompleted,
    ToolCallStarted,
)
from agent.message_bus import InboundMessage
from agent.pipeline import Pipeline
from agent.prompt_cache_log import PromptCacheLogWriter
from agent.prompt_assembler import MessageEnvelopeBuilder, PromptAssembler
from agent.prompt_block import SectionCache, SystemPromptBuilder, default_prompt_blocks
from agent.provider import ContextLengthError, LLMResponse, ToolCall
from agent.skills import SkillsLoader
from tools.base import Tool
from tools.registry import ToolRegistry
from tools.tool_search import ToolSearchTool


class EchoTool(Tool):
    name = "echo"
    description = "回显文本"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    async def execute(self, text: str, **kwargs): return f"echo:{text}:{kwargs['session_key']}"


class BlockingEchoTool(Tool):
    name = "blocking_echo"
    description = "blocking echo"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def execute(self, text: str, **kwargs):
        await self.release.wait()
        return f"blocked:{text}:{kwargs['session_key']}"


class BlockingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(None, [ToolCall("call-1", "blocking_echo", {"text": "hi"})])
        return LLMResponse("done")


class Provider:
    def __init__(self) -> None:
        self.calls = 0
        self.messages = []

    async def chat(self, messages, tools=None, on_content_delta=None, **kwargs):
        self.calls += 1
        self.messages.append(list(messages))
        if self.calls == 1:
            return LLMResponse(
                None,
                [ToolCall("call-1", "echo", {"text": "hi"})],
                thinking="工具前思考",
                provider_fields={"reasoning_content": "工具前思考"},
            )
        if on_content_delta:
            await on_content_delta({"content_delta": "完成"})
        return LLMResponse("完成", thinking="推理")


class Memory:
    async def retrieve_for_turn(self, message): return "用户偏好简洁"
    def read_self(self): return ""
    def get_memory_context(self): return ""
    def read_recent_context(self): return ""


@pytest.mark.asyncio
async def test_tool_search_unlocks_only_the_current_turn_schema() -> None:
    class HiddenTool(Tool):
        name = "mcp_demo__lookup"
        description = "查询演示记录"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return "record"

    class SearchProvider:
        def __init__(self) -> None:
            self.schema_names: list[list[str]] = []
            self.calls = 0

        async def chat(self, messages, tools=None, **kwargs):
            self.schema_names.append(
                [item["function"]["name"] for item in tools or []]
            )
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    None,
                    [ToolCall("search", "tool_search", {"query": "演示"})],
                )
            if self.calls == 2:
                return LLMResponse(
                    None,
                    [ToolCall("lookup", "mcp_demo__lookup", {})],
                )
            return LLMResponse("完成")

    class FinalProvider:
        def __init__(self) -> None:
            self.schema_names: list[str] = []

        async def chat(self, messages, tools=None, **kwargs):
            self.schema_names = [item["function"]["name"] for item in tools or []]
            return LLMResponse("完成")

    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry), always_on=True)
    registry.register(
        HiddenTool(),
        always_on=False,
        risk="external-side-effect",
        source_type="mcp",
        source_name="demo",
    )
    assembler = PromptAssembler(
        SystemPromptBuilder(default_prompt_blocks(), SectionCache()),
        MessageEnvelopeBuilder(),
    )
    first_provider = SearchProvider()
    first = Pipeline(
        first_provider,
        registry,
        EventBus(),
        assembler,
        workspace="D:/workspace",
    )

    result = await first.process(
        InboundMessage("web", "u", "a", "查询演示记录"),
        turn_id="turn-a",
    )

    assert result.content == "完成"
    assert first_provider.schema_names == [
        ["tool_search"],
        ["tool_search", "mcp_demo__lookup"],
        ["tool_search", "mcp_demo__lookup"],
    ]

    second_provider = FinalProvider()
    second = Pipeline(
        second_provider,
        registry,
        EventBus(),
        assembler,
        workspace="D:/workspace",
    )
    await second.process(
        InboundMessage("web", "u", "b", "新会话"),
        turn_id="turn-b",
    )
    assert second_provider.schema_names == ["tool_search"]


@pytest.mark.asyncio
async def test_allowed_tools_do_not_change_system_prompt(tmp_path: Path) -> None:
    class CapturingProvider:
        def __init__(self) -> None:
            self.messages: list[list[dict[str, object]]] = []
            self.schema_names: list[list[str]] = []

        async def chat(self, messages, tools=None, **kwargs):
            self.messages.append(messages)
            self.schema_names.append(
                [item["function"]["name"] for item in (tools or [])]
            )
            return LLMResponse("完成")

    tools = ToolRegistry()
    tools.register(EchoTool())
    tools.register(ToolSearchTool(tools), always_on=True)
    provider = CapturingProvider()
    pipeline = Pipeline(
        provider,
        tools,
        EventBus(),
        _assembler(tmp_path),
        workspace=str(tmp_path),
    )

    await pipeline.process(
        InboundMessage(
            channel="web",
            sender="u",
            chat_id="c",
            content="第一次",
            metadata={"allowed_tools": ["tool_search"]},
        ),
        turn_id="tools-first",
    )
    await pipeline.process(
        InboundMessage(
            channel="web",
            sender="u",
            chat_id="c",
            content="第二次",
            metadata={"allowed_tools": ["echo"]},
        ),
        turn_id="tools-second",
    )

    assert provider.messages[0][0]["content"] == provider.messages[1][0]["content"]
    assert provider.schema_names == [["tool_search"], ["echo"]]
    assert "tool_search" in str(provider.messages[0][-2]["content"])
    assert "echo" in str(provider.messages[1][-2]["content"])


@pytest.mark.asyncio
async def test_pipeline_injects_explicit_skill_mention_into_dynamic_frame(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review\ndescription: 审查代码\n---\n检查行为回归。\n",
        encoding="utf-8",
    )

    class FinalProvider:
        def __init__(self) -> None:
            self.messages = []

        async def chat(self, messages, tools=None, **kwargs):
            self.messages = messages
            return LLMResponse("完成")

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
        skills=SkillsLoader(tmp_path),
    )

    await pipeline.process(
        InboundMessage("web", "u", "c", "$review 请检查"),
        turn_id="skill-turn",
    )

    assert "审查代码" in str(provider.messages[0]["content"])
    assert "检查行为回归" not in str(provider.messages[0]["content"])
    assert "检查行为回归" in str(provider.messages[-2]["content"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt_tokens", "hit_tokens", "expected"),
    [
        (120, 90, "prompt_cache: session=web:c iteration=1 hit=90/120 rate=75.00%"),
        (None, None, ""),
    ],
)
async def test_pipeline_logs_cache_usage_only_when_provider_reports_it(
    caplog: pytest.LogCaptureFixture,
    prompt_tokens: int | None,
    hit_tokens: int | None,
    expected: str,
) -> None:
    class CacheProvider:
        async def chat(self, messages, tools=None, **kwargs):
            return LLMResponse(
                "完成",
                cache_prompt_tokens=prompt_tokens,
                cache_hit_tokens=hit_tokens,
            )

    pipeline = Pipeline(
        CacheProvider(),
        ToolRegistry(),
        EventBus(),
        PromptAssembler(
            SystemPromptBuilder(default_prompt_blocks(), SectionCache()),
            MessageEnvelopeBuilder(),
        ),
        workspace="D:/workspace",
    )

    with caplog.at_level(logging.INFO, logger="agent.pipeline"):
        await pipeline.process(
            InboundMessage("web", "u", "c", "测试缓存"),
            turn_id="cache-usage",
        )

    if expected:
        assert expected in caplog.text
    else:
        assert "prompt_cache:" not in caplog.text


@pytest.mark.asyncio
async def test_pipeline_persists_each_provider_cache_result_for_session(
    tmp_path: Path,
) -> None:
    class CacheProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    None,
                    [ToolCall("cache-call", "echo", {"text": "x"})],
                    cache_prompt_tokens=100,
                    cache_hit_tokens=40,
                )
            return LLMResponse(
                "完成",
                cache_prompt_tokens=120,
                cache_hit_tokens=80,
            )

    tools = ToolRegistry()
    tools.register(EchoTool())
    pipeline = Pipeline(
        CacheProvider(),
        tools,
        EventBus(),
        PromptAssembler(
            SystemPromptBuilder(default_prompt_blocks(), SectionCache()),
            MessageEnvelopeBuilder(),
        ),
        workspace=str(tmp_path),
        prompt_cache_log=PromptCacheLogWriter(tmp_path),
    )

    await pipeline.process(
        InboundMessage("web", "u", "c", "测试缓存日志"),
        turn_id="cache-log-turn",
    )

    files = list((tmp_path / "logs" / "prompt-cache").glob("*.log"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert [(row["iteration"], row["hit_tokens"]) for row in rows] == [
        (1, 40),
        (2, 80),
    ]


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
    # result.thinking 是整个 Turn 拼接版（前端展示用），含两轮。
    assert result.thinking == "工具前思考\n\n推理"
    # result.final_reasoning 只对应终答轮那一截思考，供下次历史重建使用，
    # 不携带工具决策思考，避免历史里工具思考重复占 token。
    assert result.final_reasoning == "推理"
    assert result.tools_used == ["echo"]
    assert result.tool_chain[0]["calls"][0]["result"] == "echo:hi:web:c"
    assert any(isinstance(event, ToolCallStarted) for event in seen)
    assert any(isinstance(event, ToolCallCompleted) for event in seen)
    assert any(isinstance(event, StreamDeltaReady) for event in seen)
    assert provider.messages[1][-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_pipeline_snapshot_tracks_running_and_completed_tool_state() -> None:
    tool = BlockingEchoTool()
    tools = ToolRegistry()
    tools.register(tool)
    events = EventBus()
    pipeline = Pipeline(
        BlockingProvider(),
        tools,
        events,
        PromptAssembler(SystemPromptBuilder(default_prompt_blocks(), SectionCache()), MessageEnvelopeBuilder()),
        workspace="D:/workspace",
    )
    started_snapshot: list[dict] = []
    completed_snapshot: list[dict] = []

    async def on_started(event: ToolCallStarted) -> None:
        started_snapshot.extend(pipeline.snapshot_interrupt_state("turn-running-tool").get("tools", []))
        tool.release.set()

    async def on_completed(event: ToolCallCompleted) -> None:
        completed_snapshot.extend(pipeline.snapshot_interrupt_state("turn-running-tool").get("tools", []))

    events.on(ToolCallStarted, on_started)
    events.on(ToolCallCompleted, on_completed)

    result = await pipeline.process(
        InboundMessage(channel="web", sender="u", chat_id="c", content="run"),
        turn_id="turn-running-tool",
    )

    assert result.content == "done"
    assert started_snapshot == [{
        "call_id": "call-1",
        "name": "blocking_echo",
        "arguments": {"text": "hi"},
        "status": "running",
        "result_preview": "",
    }]
    assert completed_snapshot == [{
        "call_id": "call-1",
        "name": "blocking_echo",
        "arguments": {"text": "hi"},
        "status": "completed",
        "result_preview": "blocked:hi:web:c",
    }]


@pytest.mark.asyncio
async def test_pipeline_passes_current_user_source_ref_to_tools() -> None:
    class SourceTool(Tool):
        name = "source"
        description = "返回当前用户消息来源"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, current_user_source_ref: str = "", **kwargs):
            return current_user_source_ref

    class SourceProvider:
        calls = 0

        async def chat(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(None, [ToolCall("source-1", "source", {})])
            return LLMResponse("完成")

    tools = ToolRegistry()
    tools.register(SourceTool())
    pipeline = Pipeline(
        SourceProvider(),
        tools,
        EventBus(),
        PromptAssembler(
            SystemPromptBuilder(default_prompt_blocks(), SectionCache()),
            MessageEnvelopeBuilder(),
        ),
        workspace="D:/workspace",
    )
    message = InboundMessage("web", "u", "c", "记住", metadata={
        "current_user_source_ref": "web:c:4",
    })

    result = await pipeline.process(message, turn_id="source-turn")

    assert result.tool_chain[0]["calls"][0]["result"] == "web:c:4"


@pytest.mark.asyncio
async def test_pipeline_keeps_full_tool_chain_while_model_receives_budgeted_result() -> None:
    full_result = "r" * 10_500

    class LongTool(Tool):
        name = "long_result"
        description = "返回长结果"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return full_result

    class LongProvider:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, object]]] = []

        async def chat(self, messages, tools=None, **kwargs):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return LLMResponse(None, [ToolCall("long-1", "long_result", {})])
            return LLMResponse("完成")

    tools = ToolRegistry()
    tools.register(LongTool())
    provider = LongProvider()
    pipeline = Pipeline(
        provider,
        tools,
        EventBus(),
        PromptAssembler(
            SystemPromptBuilder(default_prompt_blocks(), SectionCache()),
            MessageEnvelopeBuilder(),
        ),
        workspace="D:/workspace",
    )

    result = await pipeline.process(
        InboundMessage("web", "u", "c", "读取长结果"),
        turn_id="long-result",
    )

    assert result.tool_chain[0]["calls"][0]["result"] == full_result
    model_tool_result = str(provider.calls[1][-1]["content"])
    assert len(model_tool_result) <= 10_000
    assert "已截断工具结果，省略" in model_tool_result


@pytest.mark.asyncio
async def test_interrupt_snapshot_keeps_completed_calls_in_current_tool_group() -> None:
    class TwoCallProvider:
        async def chat(self, messages, tools=None, **kwargs):
            return LLMResponse(
                None,
                [
                    ToolCall("call-1", "echo", {"text": "first"}),
                    ToolCall("call-2", "echo", {"text": "block"}),
                ],
            )

    class BlockingSecondTool(EchoTool):
        def __init__(self) -> None:
            self.blocked = asyncio.Event()

        async def execute(self, text: str, **kwargs):
            if text == "block":
                self.blocked.set()
                await asyncio.Event().wait()
            return await super().execute(text, **kwargs)

    tool = BlockingSecondTool()
    tools = ToolRegistry()
    tools.register(tool)
    pipeline = Pipeline(
        TwoCallProvider(),
        tools,
        EventBus(),
        PromptAssembler(SystemPromptBuilder(default_prompt_blocks(), SectionCache()), MessageEnvelopeBuilder()),
        workspace="D:/workspace",
    )
    running = asyncio.create_task(
        pipeline.process(
            InboundMessage(channel="web", sender="u", chat_id="c", content="执行"),
            turn_id="interrupt-tools",
        )
    )
    await tool.blocked.wait()

    snapshot = pipeline.snapshot_interrupt_state("interrupt-tools")
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert snapshot["tools_used"] == ["echo"]
    assert snapshot["tool_chain_partial"][0]["calls"][0]["call_id"] == "call-1"


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
    assert str(text_path) in current[-1]["text"]
    assert "附件中的关键内容" not in current[-1]["text"]
    assert "分析附件" in current[-1]["text"]


@pytest.mark.asyncio
async def test_attachment_content_routes_local_image_to_independent_vl(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "sample.png"
    Image.new("RGB", (3, 3), "#0f766e").save(image_path)

    content = await build_current_user_content(
        "这是什么？",
        [str(image_path)],
        multimodal=False,
        vl_available=True,
    )

    assert isinstance(content, str)
    assert str(image_path) in content
    assert "read_image_vision" in content
    assert "当前主模型不能直接接收图片内容" in content
    assert "image_url" not in content


@pytest.mark.asyncio
async def test_attachment_content_references_html_for_text_only_main_model(
    tmp_path: Path,
) -> None:
    html_path = tmp_path / "page.html"
    html_path.write_text("<main>附件正文</main>", encoding="utf-8")

    content = await build_current_user_content(
        "请分析这个页面",
        [str(html_path)],
        multimodal=False,
        vl_available=False,
    )

    assert isinstance(content, str)
    assert "文件路径" in content
    assert str(html_path) in content
    assert "<main>附件正文</main>" not in content


@pytest.mark.asyncio
async def test_attachment_content_reports_missing_image_capability(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "sample.png"
    Image.new("RGB", (3, 3), "#0f766e").save(image_path)

    content = await build_current_user_content(
        "这是什么？",
        [str(image_path)],
        multimodal=False,
        vl_available=False,
    )

    assert isinstance(content, str)
    assert str(image_path) in content
    assert "未配置 VL 视觉模型" in content
    assert "read_image_vision" not in content
    assert "image_url" not in content


@pytest.mark.asyncio
async def test_pipeline_routes_image_to_independent_vl_tool_hint(
    tmp_path: Path,
) -> None:
    class FinalProvider:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []

        async def chat(self, messages, tools=None, **kwargs):
            self.messages = messages
            return LLMResponse("已识别")

    image_path = tmp_path / "sample.png"
    Image.new("RGB", (3, 3), "#0f766e").save(image_path)
    provider = FinalProvider()
    pipeline = Pipeline(
        provider,
        ToolRegistry(),
        EventBus(),
        _assembler(tmp_path),
        workspace=str(tmp_path),
        multimodal=False,
        vl_available=True,
    )

    await pipeline.process(
        InboundMessage("web", "u", "c", "这是什么？", [str(image_path)]),
        turn_id="t-vl",
    )

    current = provider.messages[-1]["content"]
    assert isinstance(current, str)
    assert str(image_path) in current
    assert "read_image_vision" in current
    assert "image_url" not in current


def _assembler(tmp_path: Path) -> PromptAssembler:
    return PromptAssembler(
        SystemPromptBuilder(default_prompt_blocks(), SectionCache()),
        MessageEnvelopeBuilder(),
    )


@pytest.mark.asyncio
async def test_pipeline_emits_compaction_lifecycle_events(tmp_path: Path) -> None:
    class GateProvider:
        context_window = 100
        max_tokens = 10

        def __init__(self) -> None:
            self.compacted = False

        def estimate_context_tokens(self, messages, tools):
            return 10 if self.compacted else 80

        async def chat(self, messages, tools=None, **kwargs):
            return LLMResponse("完成")

    provider = GateProvider()
    event_bus = EventBus()
    lifecycle: list[str] = []
    event_bus.on(ContextCompactionStarted, lambda _event: lifecycle.append("started"))
    event_bus.on(ContextCompactionCompleted, lambda _event: lifecycle.append("completed"))

    async def compact(_session_key: str, **kwargs) -> bool:
        provider.compacted = True
        return True

    async def history(_session_key: str, _limit: int | None) -> list[dict[str, object]]:
        return []

    pipeline = Pipeline(
        provider,
        ToolRegistry(),
        event_bus,
        _assembler(tmp_path),
        workspace=str(tmp_path),
        history_loader=history,
        context_compactor=compact,
    )

    result = await pipeline.process(
        InboundMessage("web", "u", "c", "当前问题"),
        turn_id="compaction-events",
    )

    assert result.content == "完成"
    assert lifecycle == ["started", "completed"]


@pytest.mark.asyncio
async def test_pipeline_emits_compaction_failed_event(tmp_path: Path) -> None:
    class GateProvider:
        context_window = 100
        max_tokens = 10

        def estimate_context_tokens(self, messages, tools):
            return 80

        async def chat(self, messages, tools=None, **kwargs):
            raise AssertionError("压缩失败后不应调用业务模型")

    event_bus = EventBus()
    errors: list[str] = []
    event_bus.on(ContextCompactionFailed, lambda event: errors.append(event.error))

    async def compact(_session_key: str, **kwargs) -> bool:
        raise RuntimeError("checkpoint 失败")

    pipeline = Pipeline(
        GateProvider(),
        ToolRegistry(),
        event_bus,
        _assembler(tmp_path),
        workspace=str(tmp_path),
        context_compactor=compact,
    )

    with pytest.raises(RuntimeError, match="checkpoint 失败"):
        await pipeline.process(
            InboundMessage("web", "u", "c", "当前问题"),
            turn_id="compaction-failed",
        )

    assert errors == ["checkpoint 失败"]


@pytest.mark.asyncio
async def test_pipeline_does_not_trim_history_after_prompt_overflow(
    tmp_path: Path,
) -> None:
    class OverflowProvider:
        def __init__(self) -> None:
            self.messages: list[list[dict[str, object]]] = []

        async def chat(self, messages, tools=None, **kwargs):
            self.messages.append(messages)
            if len(self.messages) < 6:
                raise ContextLengthError("maximum context length exceeded")
            return LLMResponse("裁剪成功")

    class RichMemory(Memory):
        def get_memory_context(self): return "必须保留的长期记忆"

    async def history(_key: str, _limit: int) -> list[dict[str, object]]:
        return [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "第二问"},
            {"role": "assistant", "content": "第二答"},
            {"role": "user", "content": "最近问题"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "history-call", "type": "function", "function": {"name": "echo", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "history-call", "content": "历史工具结果"},
            {"role": "assistant", "content": "最近回答"},
        ]

    provider = OverflowProvider()
    pipeline = Pipeline(
        provider,
        ToolRegistry(),
        EventBus(),
        _assembler(tmp_path),
        workspace=str(tmp_path),
        memory=RichMemory(),
        history_loader=history,
    )

    with pytest.raises(ContextLengthError, match="maximum context length exceeded"):
        await pipeline.process(
            InboundMessage(channel="web", sender="u", chat_id="c", content="当前问题"),
            turn_id="trim-history",
        )

    assert len(provider.messages) == 1
    assert "历史工具结果" in str(provider.messages[0])
    assert "当前问题" in str(provider.messages[0])


@pytest.mark.asyncio
async def test_pipeline_does_not_disable_prompt_sections_after_overflow(
    tmp_path: Path,
) -> None:
    class SectionProvider:
        def __init__(self) -> None:
            self.messages: list[list[dict[str, object]]] = []

        async def chat(self, messages, tools=None, **kwargs):
            self.messages.append(messages)
            if len(self.messages) < 4:
                raise ContextLengthError("too many tokens")
            return LLMResponse("降级成功")

    class SectionMemory(Memory):
        def read_self(self): return "可裁剪的自我认知"
        def get_memory_context(self): return "关键长期记忆"
        def read_recent_context(self): return "可裁剪的近期上下文"

    async def history(_key: str, _limit: int) -> list[dict[str, object]]:
        return [
            {"role": "user", "content": "最近问题"},
            {"role": "assistant", "content": "最近回答"},
        ]

    provider = SectionProvider()
    tools = ToolRegistry()
    tools.register(EchoTool())
    pipeline = Pipeline(
        provider,
        tools,
        EventBus(),
        _assembler(tmp_path),
        workspace=str(tmp_path),
        memory=SectionMemory(),
        history_loader=history,
    )

    with pytest.raises(ContextLengthError, match="too many tokens"):
        await pipeline.process(
            InboundMessage(channel="web", sender="u", chat_id="c", content="当前问题"),
            turn_id="trim-sections",
        )

    assert len(provider.messages) == 1
    final_payload = str(provider.messages[0])
    assert "关键长期记忆" in final_payload
    assert "可裁剪的自我认知" in final_payload


@pytest.mark.asyncio
async def test_pipeline_context_trimming_is_finite_and_reraises_original_error(
    tmp_path: Path,
) -> None:
    class AlwaysOverflowProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, tools=None, **kwargs):
            self.calls += 1
            raise ContextLengthError("original overflow")

    provider = AlwaysOverflowProvider()
    pipeline = Pipeline(
        provider,
        ToolRegistry(),
        EventBus(),
        _assembler(tmp_path),
        workspace=str(tmp_path),
    )

    with pytest.raises(ContextLengthError, match="original overflow"):
        await pipeline.process(
            InboundMessage(channel="web", sender="u", chat_id="c", content="当前问题"),
            turn_id="trim-bottom",
        )

    assert provider.calls == 1


@pytest.mark.asyncio
async def test_pipeline_context_retry_after_tool_does_not_execute_tool_twice(
    tmp_path: Path,
) -> None:
    class ToolThenOverflowProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(None, [ToolCall("call-once", "echo", {"text": "once"})])
            if self.calls == 2:
                raise ContextLengthError("tool continuation overflow")
            return LLMResponse("工具续轮成功")

    class CountingEchoTool(EchoTool):
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, text: str, **kwargs):
            self.calls += 1
            return await super().execute(text, **kwargs)

    provider = ToolThenOverflowProvider()
    tool = CountingEchoTool()
    tools = ToolRegistry()
    tools.register(tool)
    pipeline = Pipeline(
        provider,
        tools,
        EventBus(),
        _assembler(tmp_path),
        workspace=str(tmp_path),
    )

    with pytest.raises(ContextLengthError, match="tool continuation overflow"):
        await pipeline.process(
            InboundMessage(channel="web", sender="u", chat_id="c", content="调用一次工具"),
            turn_id="trim-tool",
        )

    assert tool.calls == 1
    assert provider.calls == 2
