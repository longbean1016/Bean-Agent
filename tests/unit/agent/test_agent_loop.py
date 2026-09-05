"""AgentLoop 的 Turn 持久化、事件和错误语义测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.agent_loop import AgentLoop, TurnInterruptState, _repair_interrupted_surface
from agent.event_bus import EventBus, SessionUpdated, TurnCommitted, TurnStarted
from agent.message_bus import InboundMessage, MessageBus, PipelineResult
from agent.config_models import MemoryConfig
from memory.consolidator import ConsolidationDraft
from memory.engine import MemoryEngine
from session.manager import SessionManager
from session.store import NewSessionEvent, NewSurfaceEvent


class Pipeline:
    async def process(self, message, *, turn_id):
        return PipelineResult("回答", thinking="思考", tool_chain=[{"iteration": 1, "calls": []}], tools_used=["echo"])


class BlockingContextGuard:
    def __init__(self, ready: bool) -> None:
        self.ready = ready
        self.calls: list[str] = []

    async def ensure_context_ready(self, session_key: str) -> bool:
        self.calls.append(session_key)
        return self.ready


class PreparingContextGuard(BlockingContextGuard):
    def needs_context_preparation(self, session_key: str) -> bool:
        return True


@pytest.mark.asyncio
async def test_durable_surface_repair_adds_unknown_tool_result_without_semantic_projection(
    tmp_path: Path,
) -> None:
    sessions = SessionManager(tmp_path)
    loop = AgentLoop(MessageBus(), EventBus(), Pipeline(), sessions)
    try:
        await sessions.append_surface(NewSurfaceEvent(
            session_key="web:repair",
            epoch_id="epoch-a",
            turn_id="turn-1",
            iteration=1,
            role="user",
            content={"role": "user", "content": "查询"},
            source_kind="user_message",
            operation_key="turn-1:1:user",
        ))
        await sessions.append_surface(NewSurfaceEvent(
            session_key="web:repair",
            epoch_id="epoch-a",
            turn_id="turn-1",
            iteration=1,
            role="assistant",
            content={
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-pending",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }],
            },
            source_kind="assistant_tool_call",
            operation_key="turn-1:1:assistant",
        ))
        await loop._repair_durable_surface(
            TurnInterruptState(
                session_key="web:repair",
                original_user_message="查询",
                llm_epoch_id="epoch-a",
                llm_surface_persisted=True,
                iteration=1,
            ),
            "turn-1",
        )

        surface = await sessions.load_surface("web:repair")
        assert surface[-1]["role"] == "tool"
        assert surface[-1]["tool_call_id"] == "call-pending"
        assert "结果未知" in surface[-1]["content"]
        assert (await sessions.get_or_create("web:repair")).messages == []
    finally:
        await sessions.close()


@pytest.mark.asyncio
async def test_durable_surface_repair_recovers_snapshot_when_append_was_not_observed(
    tmp_path: Path,
) -> None:
    sessions = SessionManager(tmp_path)
    loop = AgentLoop(MessageBus(), EventBus(), Pipeline(), sessions)
    try:
        await loop._repair_durable_surface(
            TurnInterruptState(
                session_key="web:repair-missing",
                original_user_message="查询",
                llm_epoch_id="epoch-a",
                llm_surface_persisted=True,
                llm_surface_messages=[{
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-missing",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }],
                }],
                iteration=1,
            ),
            "turn-missing",
        )

        surface = await sessions.load_surface("web:repair-missing")
        assert surface[0]["role"] == "assistant"
        assert surface[1]["tool_call_id"] == "call-missing"
    finally:
        await sessions.close()


@pytest.mark.asyncio
async def test_context_guard_is_not_called_before_pipeline(tmp_path: Path) -> None:
    class OrderedGuard(PreparingContextGuard):
        def __init__(self) -> None:
            super().__init__(True)
            self.order: list[str] = []

        def needs_context_preparation(self, session_key: str) -> bool:
            self.order.append("preflight")
            return True

        async def ensure_context_ready(self, session_key: str) -> bool:
            self.order.append("ensure")
            return await super().ensure_context_ready(session_key)

    bus = MessageBus()
    events = EventBus()
    sessions = SessionManager(tmp_path)
    guard = OrderedGuard()
    received: list[object] = []
    events.on(TurnStarted, lambda event: received.append(event))
    loop = AgentLoop(bus, events, Pipeline(), sessions, context_guard=guard)
    await bus.publish_inbound(InboundMessage("web", "u", "c", "question", metadata={"request_id": "r1"}))

    await loop.run_once()

    assert [type(event) for event in received] == [TurnStarted]
    assert guard.order == []
    await sessions.close()


@pytest.mark.asyncio
async def test_run_once_persists_complete_turn_before_committed_and_outbound(tmp_path: Path) -> None:
    bus = MessageBus()
    events = EventBus()
    sessions = SessionManager(tmp_path)
    committed = []
    updated = []
    events.on(TurnCommitted, lambda event: committed.append(event))
    events.on(SessionUpdated, lambda event: updated.append(event))
    loop = AgentLoop(bus, events, Pipeline(), sessions)
    await bus.publish_inbound(InboundMessage(channel="web", sender="u", chat_id="c", content="问题", metadata={"request_id": "r1"}))

    await loop.run_once()

    rows = sessions.store.fetch_session_messages("web:c")
    outbound = await bus.consume_outbound()
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[1]["tool_chain"][0]["iteration"] == 1
    assert updated[0].session_key == "web:c"
    assert updated[0].session["title"] == "问题"
    assert committed[0].user_message_id == rows[0]["id"]
    assert committed[0].assistant_message_id == rows[1]["id"]
    assert outbound.content == "回答"
    await sessions.close()


@pytest.mark.asyncio
async def test_agent_loop_injects_user_source_ref_before_pipeline(tmp_path: Path) -> None:
    class CapturingPipeline:
        source_ref = ""

        async def process(self, message, *, turn_id):
            self.source_ref = str(message.metadata.get("current_user_source_ref") or "")
            return PipelineResult("回答")

    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    pipeline = CapturingPipeline()
    loop = AgentLoop(bus, EventBus(), pipeline, sessions)
    await bus.publish_inbound(
        InboundMessage(channel="web", sender="u", chat_id="c", content="记住这句话")
    )

    await loop.run_once()

    rows = sessions.store.fetch_session_messages("web:c")
    assert pipeline.source_ref == rows[0]["id"] == "web:c:0"
    await sessions.close()


@pytest.mark.asyncio
async def test_title_is_persisted_before_pipeline_finishes(tmp_path: Path) -> None:
    class BlockingPipeline:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def process(self, message, *, turn_id):
            self.started.set()
            await self.release.wait()
            return PipelineResult("回答")

    bus = MessageBus()
    events = EventBus()
    sessions = SessionManager(tmp_path)
    pipeline = BlockingPipeline()
    updated = []
    events.on(SessionUpdated, lambda event: updated.append(event))
    loop = AgentLoop(bus, events, pipeline, sessions)
    await bus.publish_inbound(
        InboundMessage(channel="web", sender="u", chat_id="c", content="运行中的问题")
    )
    running = asyncio.create_task(loop.run_once())
    await pipeline.started.wait()

    summary = sessions.store.list_chat_sessions(channel="web")[0][0]
    assert summary["title"] == "运行中的问题"
    assert summary["message_count"] == 0
    assert updated[0].session["title"] == "运行中的问题"

    pipeline.release.set()
    await running
    await sessions.close()


@pytest.mark.asyncio
async def test_pipeline_failure_persists_error_turn_without_committed(tmp_path: Path) -> None:
    class FailingPipeline:
        async def process(self, message, *, turn_id):
            raise RuntimeError("模型失败")

    bus = MessageBus()
    events = EventBus()
    sessions = SessionManager(tmp_path)
    committed = []
    events.on(TurnCommitted, lambda event: committed.append(event))
    loop = AgentLoop(bus, events, FailingPipeline(), sessions)
    await bus.publish_inbound(InboundMessage(
        channel="web",
        sender="u",
        chat_id="c",
        content="问题",
        metadata={"model_route": {
            "connection_id": "company",
            "connection_name": "公司 API",
            "model_id": "model-a",
            "model_display_name": "Model A",
            "adapter": "generic_openai",
        }},
    ))

    await loop.run_once()

    rows = sessions.store.fetch_session_messages("web:c")
    outbound = await bus.consume_outbound()
    assert rows[1]["status"] == "error"
    assert rows[1]["metadata"]["model_route"]["connection_name"] == "公司 API"
    assert rows[1]["metadata"]["model_route"]["model_id"] == "model-a"
    assert committed == []
    assert "模型失败" in outbound.content
    assert outbound.metadata["model_route"]["model_display_name"] == "Model A"
    await sessions.close()


@pytest.mark.asyncio
async def test_complete_turn_persists_duration_in_message_and_outbound(tmp_path: Path) -> None:
    class TimedPipeline:
        async def process(self, message, *, turn_id):
            started_at = "2026-09-02T10:00:00+08:00"
            ended_at = "2026-09-02T10:00:02.500000+08:00"
            await sessions.append_session_event(NewSessionEvent(
                session_key=message.session_key,
                event_type="turn/start",
                turn_id=turn_id,
                step=0,
                data={"started_at": started_at},
                operation_key=f"{turn_id}:turn-start",
            ))
            await sessions.append_session_event(NewSessionEvent(
                session_key=message.session_key,
                event_type="turn/end",
                turn_id=turn_id,
                step=1,
                data={"started_at": started_at, "ended_at": ended_at, "status": "completed"},
                operation_key=f"{turn_id}:turn-end",
            ))
            return PipelineResult(
                "回答",
                duration_ms=2500,
                turn_started_at=started_at,
                turn_ended_at=ended_at,
            )

    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    loop = AgentLoop(bus, EventBus(), TimedPipeline(), sessions)
    await bus.publish_inbound(InboundMessage("web", "u", "c", "问题"))

    await loop.run_once()

    rows = sessions.store.fetch_session_messages("web:c")
    outbound = await bus.consume_outbound()
    assert rows[1]["metadata"]["duration_ms"] == 2500
    assert rows[1]["timestamp"] == "2026-09-02T10:00:02.500000+08:00"
    assert outbound.metadata["duration_ms"] == 2500
    assert outbound.metadata["generated_at"] == "2026-09-02T10:00:02.500000+08:00"
    await sessions.close()


@pytest.mark.asyncio
async def test_pipeline_failure_persists_model_surface_for_replay(tmp_path: Path) -> None:
    class FailingPipeline:
        async def process(self, message, *, turn_id):
            raise RuntimeError("模型失败")

        def snapshot_interrupt_state(self, turn_id: str):
            frame = '<system-reminder data-system-context-frame="true">frame</system-reminder>'
            wrapped = "[当前消息时间: 2026-09-01T10:00:00+08:00]\n问题"
            return {
                "llm_context_frame": frame,
                "llm_user_content": wrapped,
                "llm_message_timestamp": "2026-09-01T10:00:00+08:00",
                "llm_epoch_id": "epoch-1",
                "llm_surface_messages": [
                    {"role": "user", "content": frame},
                    {"role": "user", "content": wrapped},
                ],
            }

    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    loop = AgentLoop(bus, EventBus(), FailingPipeline(), sessions)
    await bus.publish_inbound(InboundMessage(channel="web", sender="u", chat_id="c", content="问题"))

    await loop.run_once()

    row = sessions.store.fetch_session_messages("web:c")[0]
    assert row["llm_epoch_id"] == "epoch-1"
    assert row["llm_surface_messages"][-1]["content"] == "[当前消息时间: 2026-09-01T10:00:00+08:00]\n问题"
    history = await sessions.load_history("web:c")
    assert history == row["llm_surface_messages"]
    await sessions.close()


@pytest.mark.asyncio
async def test_pipeline_failure_repairs_persisted_surface_tool_call(tmp_path: Path) -> None:
    sessions = SessionManager(tmp_path)

    class FailingSurfacePipeline:
        async def process(self, message, *, turn_id):
            await sessions.append_surface(NewSurfaceEvent(
                session_key=message.session_key,
                epoch_id="epoch-error",
                turn_id=turn_id,
                iteration=1,
                role="assistant",
                content={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-error",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }],
                },
                source_kind="assistant_tool_call",
                operation_key=f"{turn_id}:assistant",
            ))
            raise RuntimeError("模型失败")

        def snapshot_interrupt_state(self, turn_id: str):
            return {
                "llm_epoch_id": "epoch-error",
                "llm_surface_persisted": True,
                "iteration": 1,
            }

    bus = MessageBus()
    loop = AgentLoop(bus, EventBus(), FailingSurfacePipeline(), sessions)
    await bus.publish_inbound(InboundMessage("web", "u", "error-surface", "问题"))

    await loop.run_once()

    surface = sessions.store.load_surface("web:error-surface")
    assert surface[-1]["role"] == "tool"
    assert surface[-1]["tool_call_id"] == "call-error"
    assert "结果未知" in surface[-1]["content"]
    await sessions.close()


@pytest.mark.asyncio
async def test_agent_loop_persists_and_dispatches_context_retry_trace(
    tmp_path: Path,
) -> None:
    class RetriedPipeline:
        async def process(self, message, *, turn_id):
            return PipelineResult(
                "回答",
                context_retry={
                    "selected_plan": "half_history",
                    "history_messages": 4,
                    "disabled_sections": ["long_term_memory"],
                    "attempts": [{"name": "full"}, {"name": "half_history"}],
                },
            )

    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    loop = AgentLoop(bus, EventBus(), RetriedPipeline(), sessions)
    await bus.publish_inbound(
        InboundMessage(channel="web", sender="u", chat_id="c", content="问题")
    )

    await loop.run_once()

    rows = sessions.store.fetch_session_messages("web:c")
    outbound = await bus.consume_outbound()
    assert rows[1]["metadata"]["context_retry"]["selected_plan"] == "half_history"
    assert outbound.metadata["context_retry"]["history_messages"] == 4
    await sessions.close()


@pytest.mark.asyncio
async def test_context_guard_does_not_block_pipeline(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    guard = BlockingContextGuard(False)
    loop = AgentLoop(
        bus,
        EventBus(),
        Pipeline(),
        sessions,
        context_guard=guard,
    )
    await bus.publish_inbound(
        InboundMessage(channel="web", sender="u", chat_id="c", content="问题")
    )

    await loop.run_once()

    outbound = await bus.consume_outbound()
    assert guard.calls == []
    assert outbound.content == "回答"
    assert [row["role"] for row in sessions.store.fetch_session_messages("web:c")] == [
        "user", "assistant"
    ]
    await sessions.close()


@pytest.mark.asyncio
async def test_context_guard_can_be_explicitly_skipped_for_internal_turn(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    guard = BlockingContextGuard(False)
    loop = AgentLoop(bus, EventBus(), Pipeline(), sessions, context_guard=guard)
    await bus.publish_inbound(
        InboundMessage(
            channel="scheduler",
            sender="system",
            chat_id="job",
            content="内部任务",
            metadata={"skip_memory_context_guard": True},
        )
    )

    await loop.run_once()

    outbound = await bus.consume_outbound()
    assert outbound.content == "回答"
    assert guard.calls == []
    await sessions.close()


@pytest.mark.asyncio
async def test_interrupt_immediately_persists_marker_and_completed_tools(tmp_path: Path) -> None:
    class BlockingPipeline:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def process(self, message, *, turn_id):
            self.started.set()
            await asyncio.Event().wait()

        def snapshot_interrupt_state(self, turn_id: str):
            return {
                "partial_reply": "未完成回答",
                "partial_thinking": "未完成思考",
                "tools_used": ["read_file"],
                "tools": [{
                    "call_id": "call-2",
                    "name": "long_running_tool",
                    "arguments": {},
                    "result_preview": "部分输出",
                    "status": "running",
                }],
                "tool_chain_partial": [
                    {
                        "iteration": 1,
                        "text": "",
                        "provider_fields": {"reasoning_content": "先读取文件"},
                        "calls": [
                            {
                                "call_id": "call-1",
                                "name": "read_file",
                                "arguments": {"path": "a.txt"},
                                "result": "文件内容",
                                "status": "ok",
                            },
                        ],
                    }
                ],
            }

        def discard_interrupt_snapshot(self, turn_id: str) -> None:
            pass

    bus = MessageBus()
    events = EventBus()
    sessions = SessionManager(tmp_path)
    pipeline = BlockingPipeline()
    loop = AgentLoop(bus, events, pipeline, sessions)
    first = InboundMessage(channel="web", sender="u", chat_id="c", content="读取文件")
    await bus.publish_inbound(first)
    running = asyncio.create_task(loop.run_once())
    await pipeline.started.wait()

    result = await loop.request_interrupt("web:c")
    await running

    assert result.status == "interrupted"
    rows = sessions.store.fetch_session_messages("web:c")
    assert [row["content"] for row in rows] == ["读取文件", "[用户已停止生成]"]
    assert rows[0]["turn_id"] == rows[1]["turn_id"] == result.turn_id
    assert rows[1]["status"] == "interrupted"
    assert rows[1]["reasoning_content"] == ""
    assert rows[1]["interrupted_display_content"] == "未完成回答"
    assert rows[1]["interrupted_display_reasoning"] == "未完成思考"
    assert rows[1]["interrupted_thinking_status"] == "interrupted"
    assert rows[1]["tools_used"] == ["read_file", "long_running_tool"]
    assert rows[1]["tool_chain"][0]["calls"][0]["result"] == "文件内容"
    assert rows[1]["tool_chain"][0]["calls"][0]["status"] == "ok"
    assert rows[1]["tool_chain"][0]["calls"][1]["status"] == "interrupted"
    assert rows[1]["tool_chain"][0]["calls"][1]["result"] == "部分输出"
    assert len(rows[1]["tool_chain"][0]["calls"]) == 2
    history = (await sessions.get_or_create("web:c")).get_history(max_messages=20)
    assert history == [
        {"role": "user", "content": "读取文件"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
            }, {
                "id": "call-2",
                "type": "function",
                "function": {"name": "long_running_tool", "arguments": "{}"},
            }],
            "reasoning_content": "先读取文件",
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "文件内容"},
        {
            "role": "tool",
            "tool_call_id": "call-2",
            "content": "工具调用在中断前已经发出，但没有记录完整结果；结果未知。请根据工具语义决定是否重试：只有只读或幂等操作可以重试；可能产生副作用时先核验外部状态或询问用户，不要盲目重试。",
        },
        {"role": "assistant", "content": "[用户已停止生成]"},
    ]
    await sessions.close()


@pytest.mark.asyncio
async def test_interrupt_persists_duration_from_turn_boundaries(tmp_path: Path) -> None:
    class TimedBlockingPipeline:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def process(self, message, *, turn_id):
            await sessions.append_session_event(NewSessionEvent(
                session_key=message.session_key,
                event_type="turn/start",
                turn_id=turn_id,
                step=0,
                data={"started_at": "2026-09-02T10:00:00+08:00"},
                operation_key=f"{turn_id}:turn-start",
            ))
            self.started.set()
            await asyncio.Event().wait()

        def snapshot_interrupt_state(self, turn_id: str):
            return {"partial_reply": "半截", "turn_started_at": "2026-09-02T10:00:00+08:00"}

        def discard_interrupt_snapshot(self, turn_id: str) -> None:
            pass

    sessions = SessionManager(tmp_path)
    pipeline = TimedBlockingPipeline()
    loop = AgentLoop(MessageBus(), EventBus(), pipeline, sessions)
    await loop._bus.publish_inbound(InboundMessage("web", "u", "timed", "问题"))
    running = asyncio.create_task(loop.run_once())
    await pipeline.started.wait()

    result = await loop.request_interrupt("web:timed")
    await running

    rows = sessions.store.fetch_session_messages("web:timed")
    events = sessions.store.fetch_session_events("web:timed")
    ends = [event for event in events if event["event_type"] == "turn/end"]
    assert result.duration_ms is not None and result.duration_ms >= 0
    assert rows[1]["metadata"]["duration_ms"] == result.duration_ms
    assert ends[0]["data"]["status"] == "interrupted"
    assert ends[0]["data"]["duration_ms"] == result.duration_ms
    await sessions.close()


@pytest.mark.asyncio
async def test_interrupt_repairs_model_surface_for_next_question(tmp_path: Path) -> None:
    class BlockingPipeline:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def process(self, message, *, turn_id):
            self.started.set()
            await asyncio.Event().wait()

        def snapshot_interrupt_state(self, turn_id: str):
            frame = '<system-reminder data-system-context-frame="true">frame</system-reminder>'
            wrapped = "[当前消息时间: 2026-09-01T10:00:00+08:00]\n问题"
            return {
                "llm_context_frame": frame,
                "llm_user_content": wrapped,
                "llm_surface_messages": [
                    {"role": "user", "content": frame},
                    {"role": "user", "content": wrapped},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "complete-call",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"},
                            },
                            {
                                "id": "pending-call",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": "{}"},
                            },
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "complete-call",
                        "content": "完整文件内容",
                    },
                ],
                "tool_chain_partial": [
                    {
                        "iteration": 1,
                        "calls": [
                            {
                                "call_id": "complete-call",
                                "name": "read_file",
                                "arguments": {},
                                "result": "完整文件内容",
                                "status": "ok",
                            }
                        ],
                    }
                ],
                "tools": [
                    {
                        "call_id": "pending-call",
                        "name": "write_file",
                        "arguments": {},
                        "result_preview": "部分写入",
                        "status": "running",
                    }
                ],
            }

        def discard_interrupt_snapshot(self, turn_id: str) -> None:
            pass

    sessions = SessionManager(tmp_path)
    blocking = BlockingPipeline()
    loop = AgentLoop(MessageBus(), EventBus(), blocking, sessions)
    await loop._bus.publish_inbound(
        InboundMessage(channel="web", sender="u", chat_id="c", content="问题")
    )
    running = asyncio.create_task(loop.run_once())
    await blocking.started.wait()

    result = await loop.request_interrupt("web:c")
    await running

    rows = sessions.store.fetch_session_messages("web:c")
    surface = rows[0]["llm_surface_messages"]
    assert [item.get("role") for item in surface] == [
        "user", "user", "assistant", "tool", "tool"
    ]
    assert surface[3]["content"] == "完整文件内容"
    assert surface[4]["tool_call_id"] == "pending-call"
    assert "结果未知" in surface[4]["content"]
    assert rows[1]["content"] == "[用户已停止生成]"

    history = await sessions.load_history("web:c")
    assert history == surface

    captured: list[list[dict[str, Any]]] = []

    class ContinuationPipeline:
        async def process(self, message, *, turn_id):
            captured.append(await sessions.load_history(message.session_key, None))
            return PipelineResult("继续回答")

    loop._pipeline = ContinuationPipeline()
    await loop._bus.publish_inbound(
        InboundMessage(channel="web", sender="u", chat_id="c", content="继续")
    )
    await loop.run_once()

    assert captured == [surface]
    await sessions.close()


def test_repair_interrupted_surface_is_idempotent() -> None:
    surface = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "pending-call",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": "{}"},
                }
            ],
        }
    ]
    _repair_interrupted_surface(surface)
    _repair_interrupted_surface(surface)

    assert len(surface) == 2
    assert surface[1]["tool_call_id"] == "pending-call"


def test_interrupted_surface_keeps_partial_assistant_separate_from_semantic_marker() -> None:
    surface = [
        {"role": "user", "content": "当前问题"},
        {"role": "assistant", "content": "已经输出的一半"},
    ]

    _repair_interrupted_surface(surface)

    # 模型侧继续看到已发送内容；[用户已停止生成] 只由语义 assistant 展示。
    assert surface == [
        {"role": "user", "content": "当前问题"},
        {"role": "assistant", "content": "已经输出的一半"},
    ]


@pytest.mark.asyncio
async def test_active_turn_snapshot_exports_running_state_for_resubscribe(tmp_path: Path) -> None:
    class BlockingPipeline:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def process(self, message, *, turn_id):
            self.started.set()
            await asyncio.Event().wait()

        def snapshot_interrupt_state(self, turn_id: str):
            return {
                "partial_reply": "partial answer",
                "partial_thinking": "partial thinking",
                "tools": [
                    {
                        "call_id": "call-1",
                        "name": "read_file",
                        "arguments": {"path": "README.md"},
                        "status": "completed",
                        "result_preview": "project docs",
                    }
                ],
            }

        def discard_interrupt_snapshot(self, turn_id: str) -> None:
            pass

    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    pipeline = BlockingPipeline()
    loop = AgentLoop(bus, EventBus(), pipeline, sessions)
    await bus.publish_inbound(
        InboundMessage(
            channel="web",
            sender="u",
            chat_id="c",
            content="read it",
            media=["D:/tmp/a.png"],
            metadata={"request_id": "r1"},
        )
    )
    running = asyncio.create_task(loop.run_once())
    await pipeline.started.wait()

    try:
        snapshot = loop.get_active_turn_snapshot("web:c")
        assert snapshot is not None
        assert snapshot["session_id"] == "web:c"
        assert snapshot["turn_id"]
        assert snapshot["request_id"] == "r1"
        assert snapshot["user_message"] == "read it"
        assert snapshot["user_media"] == ["D:/tmp/a.png"]
        assert snapshot["content"] == "partial answer"
        assert snapshot["thinking"] == "partial thinking"
        assert snapshot["tools"][0]["status"] == "completed"
        assert snapshot["status"] == "running"
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await sessions.close()

@pytest.mark.asyncio
async def test_turn_committed_reaches_memory_worker_with_persisted_snapshot(tmp_path: Path) -> None:
    class Embedder:
        async def embed(self, text): return [1.0, 0.0]
        async def embed_batch(self, texts): return [[1.0, 0.0] for _ in texts]
        async def close(self): pass

    class Provider:
        async def complete(self, messages, tools=None): return type("R", (), {"content": "[]"})()

    class Extractor:
        async def extract(self, messages, previous_recent_context): return ConsolidationDraft()

    bus = MessageBus()
    events = EventBus()
    sessions = SessionManager(tmp_path)
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    memory = MemoryEngine(tmp_path, Embedder(), Provider(), sessions.store, config=config, consolidation_extractor=Extractor())
    captured = []
    memory._post_response.handle = lambda event: _capture(captured, event)
    memory.bind_events(events)
    loop = AgentLoop(bus, events, Pipeline(), sessions)
    await bus.publish_inbound(InboundMessage(channel="web", sender="u", chat_id="c", content="问题"))
    try:
        await loop.run_once()
        await memory.drain()
    finally:
        await memory.close()
        await sessions.close()

    assert captured[0].user_message == "问题"
    assert captured[0].assistant_response == "回答"
    assert captured[0].tool_chain[0]["iteration"] == 1
    assert captured[0].channel == "web"
    assert captured[0].chat_id == "c"


async def _capture(target, event):
    target.append(event)
