"""MemoryEngine 的记、查、引用、忘与 Turn 归档集成测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.config_models import MemoryConfig
from agent.message_bus import InboundMessage
from memory.consolidator import ConsolidationDraft
from memory.contracts import (
    MemoryIngestRequest, MemoryMutation, MemoryQuery, MemoryQueryFilters,
    MemoryScope,
)
from memory.engine import MemoryEngine
from memory.implicit_extractor import ImplicitMemoryDraft
from session.store import NewMessage, SessionStore


class Embedder:
    async def embed(self, text: str) -> list[float]: return [1.0, 0.0]
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: return [[1.0, 0.0] for _ in texts]
    async def close(self) -> None: self.closed = True


class Provider:
    async def complete(self, messages, tools=None, **kwargs):
        if "长期记忆提取专家" in messages[0]["content"]:
            return SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(
                    name="submit_implicit_memory",
                    arguments={"profile": [], "preference": [], "procedure": []},
                )],
            )
        return type("Response", (), {"content": "<decision>RETRIEVE</decision><history_query>回答风格</history_query>"})()


class Extractor:
    async def extract(self, messages, previous_recent_context, *, recent_turns="", current_memory=""):
        return ConsolidationDraft(history_entries=[{"summary": "\u5b8c\u6210\u9879\u76ee", "emotional_weight": 1}], pending_items=[{"tag": "identity", "content": "\u7528\u6237\u662f\u5f00\u53d1\u8005"}], recent_context="# Recent Context\n- \u9879\u76ee\u5f00\u53d1")


@pytest.mark.asyncio
async def test_engine_remember_query_evidence_and_forget(tmp_path: Path) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config, consolidation_extractor=Extractor())
    try:
        written = await engine.mutate(MemoryMutation(kind="remember", summary="用户喜欢简洁回答", memory_kind="preference", source_ref="web:c:0", scope=MemoryScope(channel="web", chat_id="c")))
        recalled = await engine.query(MemoryQuery("回答风格"))
        forgotten = await engine.mutate(MemoryMutation(kind="forget", ids=(written.item_id,)))
        after = await engine.query(MemoryQuery("回答风格"))
    finally:
        await engine.close()
        sessions.close()

    assert recalled.records[0].evidence[0].source_ref == "web:c:0"
    assert forgotten.affected_ids == [written.item_id]
    assert after.records == []


@pytest.mark.asyncio
async def test_explicit_remember_does_not_persist_session_scope(tmp_path: Path) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config)
    try:
        written = await engine.mutate(MemoryMutation(
            kind="remember",
            summary="用户的名字是长豆角",
            memory_kind="profile",
            source_ref="web:session-a:0",
            scope=MemoryScope(
                session_key="web:session-a",
                channel="web",
                chat_id="session-a",
            ),
        ))
        item = engine._store.get_items_by_ids([written.item_id])[0]
    finally:
        await engine.close()
        sessions.close()

    # 对齐 Akashic：显式记忆属于 workspace，source_ref 仍保留来源证据。
    assert item["source_ref"] == "web:session-a:0"
    assert "scope_channel" not in item["extra_json"]
    assert "scope_chat_id" not in item["extra_json"]


@pytest.mark.asyncio
async def test_turn_context_retrieval_can_recall_event_from_another_session(
    tmp_path: Path,
) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config)
    try:
        engine._store.upsert_item(
            "event",
            "用户完成了 WebSocket 启动验证",
            [1.0, 0.0],
            "web:session-a@0-9",
            extra={"scope_channel": "web", "scope_chat_id": "session-a"},
        )

        block = await engine.retrieve_for_turn(SimpleNamespace(
            content="之前完成了什么验证",
            channel="web",
            chat_id="session-b",
        ))
    finally:
        await engine.close()
        sessions.close()

    assert "用户完成了 WebSocket 启动验证" in block


@pytest.mark.asyncio
async def test_turn_context_automatically_injects_tool_required_procedure(
    tmp_path: Path,
) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config)
    try:
        engine._store.upsert_item(
            "procedure",
            "发布前运行完整测试",
            [1.0, 0.0],
            "web:source:0",
            extra={"tool_requirement": "pytest"},
        )

        block = await engine.retrieve_for_turn(SimpleNamespace(
            content="帮我发布这个项目",
            channel="web",
            chat_id="new-chat",
        ))
    finally:
        await engine.close()
        sessions.close()

    assert "【强制约束】" in block
    assert "发布前运行完整测试" in block
    assert "必须调用工具：pytest" in block


@pytest.mark.asyncio
async def test_turn_retrieval_uses_only_current_message_without_llm_enhancement(
    tmp_path: Path,
) -> None:
    """普通 Turn 必须直接检索当前消息，不能等待历史改写或 HyDE。"""

    class ForbiddenRewriter:
        async def decide(self, user_msg: str, recent_history: str):
            raise AssertionError("普通 Turn 不应调用查询改写器")

    class CapturingRetriever:
        calls: list[tuple[str, list[str]]] = []

        async def retrieve(self, query: str, **kwargs):
            self.calls.append((query, list(kwargs.get("aux_queries") or [])))
            return []

    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config)
    retriever = CapturingRetriever()
    engine._rewriter = ForbiddenRewriter()
    engine._retriever = retriever
    try:
        await engine.retrieve_for_turn(InboundMessage(
            channel="web",
            sender="web",
            chat_id="c",
            content="你还记得我的设备吗",
        ))
    finally:
        await engine.close()
        sessions.close()

    assert retriever.calls[0] == (
        "你还记得我的设备吗",
        [],
    )


@pytest.mark.asyncio
async def test_answer_query_uses_recent_six_messages_and_hyde_after_empty_results(
    tmp_path: Path,
) -> None:
    class CapturingRewriter:
        recent_history = ""

        async def decide(self, user_msg: str, recent_history: str):
            self.recent_history = recent_history
            return SimpleNamespace(
                needs_episodic=True,
                episodic_query="用户使用的设备型号",
                procedure_query="",
            )

    class CapturingHyDE:
        context = ""

        async def augment(self, *, query, context, raw_items, retrieve_fn):
            self.context = context
            items = await retrieve_fn("用户使用 Fitbit Charge 6")
            return SimpleNamespace(
                items=items,
                used_hyde=True,
                hypothesis="用户使用 Fitbit Charge 6",
            )

    class Retriever:
        queries: list[str] = []
        aux_queries: list[str] = []

        async def retrieve(self, query: str, **kwargs):
            self.queries.append(query)
            self.aux_queries.extend(kwargs.get("aux_queries") or [])
            if query == "用户使用 Fitbit Charge 6":
                return [{
                    "id": "device",
                    "memory_type": "profile",
                    "summary": "用户使用 Fitbit Charge 6",
                    "score": 0.9,
                }]
            return []

    sessions = SessionStore(tmp_path / "sessions.db")
    for index in range(8):
        sessions.add_message(NewMessage(
            session_key="web:c",
            role="user" if index % 2 == 0 else "assistant",
            content=f"历史消息 {index}",
        ))
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config)
    rewriter = CapturingRewriter()
    hyde = CapturingHyDE()
    retriever = Retriever()
    engine._rewriter = rewriter
    engine._hyde = hyde
    engine._retriever = retriever
    try:
        result = await engine.query(MemoryQuery(
            "我的设备是什么",
            intent="answer",
            scope=MemoryScope(session_key="web:c", channel="web", chat_id="c"),
        ))
    finally:
        await engine.close()
        sessions.close()

    assert "历史消息 1" not in rewriter.recent_history
    assert "历史消息 2" in rewriter.recent_history
    assert "历史消息 7" in rewriter.recent_history
    assert hyde.context == rewriter.recent_history
    assert retriever.queries[0] == "我的设备是什么"
    assert retriever.aux_queries == ["用户使用的设备型号"]
    assert retriever.queries[-1] == "用户使用 Fitbit Charge 6"
    assert result.records[0].summary == "用户使用 Fitbit Charge 6"


@pytest.mark.asyncio
async def test_timeline_query_reads_structured_events_without_retriever(
    tmp_path: Path,
) -> None:
    class ForbiddenRetriever:
        async def retrieve(self, query: str, **kwargs):
            raise AssertionError("timeline 不应调用向量或关键词 Retriever")

    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config)
    engine._retriever = ForbiddenRetriever()
    try:
        engine._store.upsert_item(
            "event",
            "完成时间线实现",
            [1.0, 0.0],
            "web:a:0",
            happened_at="2026-07-19T09:00:00+00:00",
        )
        result = await engine.query(MemoryQuery(
            "最近完成了什么",
            intent="timeline",
            filters=MemoryQueryFilters(
                time_start=datetime(2026, 7, 19, tzinfo=timezone.utc),
                time_end=datetime(2026, 7, 20, tzinfo=timezone.utc),
            ),
        ))
    finally:
        await engine.close()
        sessions.close()

    assert [record.summary for record in result.records] == ["完成时间线实现"]
    assert result.trace["intent"] == "timeline"
    assert result.trace["hit_count"] == 1


@pytest.mark.asyncio
async def test_procedure_query_limits_types_without_llm_enhancement(
    tmp_path: Path,
) -> None:
    class ForbiddenRewriter:
        async def decide(self, user_msg: str, recent_history: str):
            raise AssertionError("procedure 不应调用查询改写器")

    class CapturingRetriever:
        kwargs: dict[str, object] = {}

        async def retrieve(self, query: str, **kwargs):
            self.kwargs = kwargs
            return [{
                "id": "procedure-1",
                "memory_type": "procedure",
                "summary": "发布前运行测试",
                "score": 0.9,
                "extra_json": {"tool_requirement": "pytest"},
            }]

    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config)
    retriever = CapturingRetriever()
    engine._rewriter = ForbiddenRewriter()
    engine._retriever = retriever
    try:
        result = await engine.query(MemoryQuery(
            "发布项目",
            intent="procedure",
            context={"aux_queries": ["项目发布流程", "项目发布流程"]},
        ))
    finally:
        await engine.close()
        sessions.close()

    assert retriever.kwargs["memory_types"] == ["procedure", "preference"]
    assert retriever.kwargs["aux_queries"] == ["项目发布流程"]
    assert result.records[0].kind == "procedure"
    assert result.records[0].signals["tool_requirement"] == "pytest"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent",
    [
        "answer",
        "context",
    ],
)
async def test_default_query_intents_share_workspace_memories_across_sessions(
    tmp_path: Path,
    intent: str,
) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config)
    try:
        for chat_id, summary in (
            ("session-a", "会话 A 的发布记录"),
            ("session-b", "会话 B 的发布记录"),
        ):
            engine._store.upsert_item(
                "event",
                summary,
                [1.0, 0.0],
                f"web:{chat_id}@0-1",
                extra={"scope_channel": "web", "scope_chat_id": chat_id},
            )
        result = await engine.query(MemoryQuery(
            "发布记录",
            intent=intent,
            scope=MemoryScope(channel="web", chat_id="session-b"),
        ))
    finally:
        await engine.close()
        sessions.close()

    assert {record.summary for record in result.records} == {
        "会话 A 的发布记录",
        "会话 B 的发布记录",
    }


@pytest.mark.asyncio
async def test_interest_query_only_reads_workspace_preferences_and_profile(
    tmp_path: Path,
) -> None:
    class ForbiddenRewriter:
        async def decide(self, user_msg: str, recent_history: str):
            raise AssertionError("interest 不应调用查询改写器")

    class CapturingRetriever:
        kwargs: dict[str, object] = {}

        async def retrieve(self, query: str, **kwargs):
            self.kwargs = kwargs
            return [{
                "id": "preference-1",
                "memory_type": "preference",
                "summary": "用户喜欢徒步",
                "score": 0.9,
            }]

    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config)
    retriever = CapturingRetriever()
    engine._rewriter = ForbiddenRewriter()
    engine._retriever = retriever
    try:
        result = await engine.query(MemoryQuery(
            "用户最近可能关心什么",
            intent="interest",
            scope=MemoryScope(channel="web", chat_id="session-b"),
            filters=MemoryQueryFilters(kinds=("event", "procedure")),
            context={"aux_queries": ["不应使用的扩展查询"]},
            limit=2,
        ))
    finally:
        await engine.close()
        sessions.close()

    assert retriever.kwargs["memory_types"] == ["preference", "profile"]
    assert retriever.kwargs["aux_queries"] == []
    assert retriever.kwargs["require_scope_match"] is False
    assert retriever.kwargs["top_k"] == 2
    assert [record.kind for record in result.records] == ["preference"]
    assert result.trace["intent"] == "interest"
    assert result.trace["memory_types"] == ["preference", "profile"]
    assert result.trace["read_only"] is True


@pytest.mark.asyncio
async def test_context_query_can_explicitly_require_session_scope(tmp_path: Path) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config)
    try:
        for chat_id in ("session-a", "session-b"):
            engine._store.upsert_item(
                "event",
                f"{chat_id} 的记录",
                [1.0, 0.0],
                f"web:{chat_id}@0-1",
                extra={"scope_channel": "web", "scope_chat_id": chat_id},
            )
        result = await engine.query(MemoryQuery(
            "记录",
            intent="context",
            scope=MemoryScope(channel="web", chat_id="session-b"),
            context={"require_scope_match": True},
        ))
    finally:
        await engine.close()
        sessions.close()

    assert [record.summary for record in result.records] == ["session-b 的记录"]


@pytest.mark.asyncio
async def test_turn_committed_consolidates_and_syncs_vector_event(tmp_path: Path) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    for index in range(6):
        sessions.add_message(NewMessage(session_key="web:c", role="user" if index % 2 == 0 else "assistant", content=f"消息 {index}"))
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config, consolidation_extractor=Extractor(), keep_count=2, consolidation_threshold=4)
    try:
        await engine.on_turn_committed(type("Event", (), {"session_key": "web:c", "channel": "web", "chat_id": "c", "input_message": "消息 4", "assistant_response": "消息 5", "tool_chain_raw": []})())
        await engine.drain()
        recalled = await engine.query(MemoryQuery("完成项目"))
        cursor = sessions.get_cursor("web:c")
    finally:
        await engine.close()
        sessions.close()

    assert cursor == 4
    assert any(record.summary == "完成项目" for record in recalled.records)
    assert engine.tool_profile().recall is not None
    assert engine.tool_profile().forget.parameters["required"] == ["ids"]


@pytest.mark.asyncio
async def test_turn_committed_only_enqueues_background_memory_work(tmp_path: Path) -> None:
    import asyncio

    class SlowExtractor:
        async def extract(self, messages, previous_recent_context, *, recent_turns="", current_memory=""):
            await asyncio.sleep(0.05)
            return ConsolidationDraft()

    sessions = SessionStore(tmp_path / "sessions.db")
    for index in range(4):
        sessions.add_message(NewMessage(session_key="web:c", role="user", content=f"消息 {index}"))
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config, consolidation_extractor=SlowExtractor(), keep_count=1, consolidation_threshold=3)
    try:
        await asyncio.wait_for(
            engine.on_turn_committed(type("Event", (), {"session_key": "web:c", "channel": "web", "chat_id": "c", "input_message": "u", "assistant_response": "a", "tool_chain_raw": []})()),
            timeout=0.01,
        )
        assert sessions.get_cursor("web:c") == 0
        await engine.drain()
        assert sessions.get_cursor("web:c") == 3
    finally:
        await engine.close()
        sessions.close()


@pytest.mark.asyncio
async def test_context_guard_skips_consolidation_below_threshold(tmp_path: Path) -> None:
    class ForbiddenExtractor:
        async def extract(self, messages, previous_recent_context):
            raise AssertionError("低于积压阈值时不应调用压缩模型")

    sessions = SessionStore(tmp_path / "sessions.db")
    for index in range(3):
        sessions.add_message(
            NewMessage(session_key="web:c", role="user", content=f"消息 {index}")
        )
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(
        tmp_path,
        Embedder(),
        Provider(),
        sessions,
        config=config,
        consolidation_extractor=ForbiddenExtractor(),
        keep_count=1,
        consolidation_threshold=3,
    )
    try:
        ready = await engine.ensure_context_ready("web:c")
    finally:
        await engine.close()
        sessions.close()

    assert ready is True


@pytest.mark.asyncio
async def test_default_context_guard_starts_at_36_pending_messages(tmp_path: Path) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    for index in range(35):
        sessions.add_message(
            NewMessage(session_key="web:c", role="user", content=f"消息 {index}")
        )
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config)
    try:
        assert engine.needs_context_preparation("web:c") is False
        sessions.add_message(
            NewMessage(session_key="web:c", role="user", content="第 36 条消息")
        )
        assert engine.needs_context_preparation("web:c") is True
    finally:
        await engine.close()
        sessions.close()


@pytest.mark.asyncio
async def test_context_guard_consolidates_and_requires_cursor_progress(
    tmp_path: Path,
) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    for index in range(4):
        sessions.add_message(
            NewMessage(session_key="web:c", role="user", content=f"消息 {index}")
        )
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(
        tmp_path,
        Embedder(),
        Provider(),
        sessions,
        config=config,
        consolidation_extractor=Extractor(),
        keep_count=1,
        consolidation_threshold=3,
    )
    try:
        ready = await engine.ensure_context_ready("web:c")
        cursor = sessions.get_cursor("web:c")
    finally:
        await engine.close()
        sessions.close()

    assert ready is True
    assert cursor == 3


@pytest.mark.asyncio
async def test_context_guard_blocks_when_consolidation_fails(tmp_path: Path) -> None:
    class FailingExtractor:
        async def extract(self, messages, previous_recent_context):
            raise RuntimeError("压缩失败")

    sessions = SessionStore(tmp_path / "sessions.db")
    for index in range(4):
        sessions.add_message(
            NewMessage(session_key="web:c", role="user", content=f"消息 {index}")
        )
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(
        tmp_path,
        Embedder(),
        Provider(),
        sessions,
        config=config,
        consolidation_extractor=FailingExtractor(),
        keep_count=1,
        consolidation_threshold=3,
    )
    try:
        ready = await engine.ensure_context_ready("web:c")
        cursor = sessions.get_cursor("web:c")
    finally:
        await engine.close()
        sessions.close()

    assert ready is False
    assert cursor == 0


@pytest.mark.asyncio
async def test_context_guard_does_not_advance_cursor_when_outbox_fails(
    tmp_path: Path,
) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    for index in range(4):
        sessions.add_message(
            NewMessage(session_key="web:c", role="user", content=f"消息 {index}")
        )
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(
        tmp_path,
        Embedder(),
        Provider(),
        sessions,
        config=config,
        consolidation_extractor=Extractor(),
        keep_count=1,
        consolidation_threshold=3,
    )

    def fail_outbox(source_ref, payload):
        raise RuntimeError("outbox 写入失败")

    engine._store.enqueue_consolidation = fail_outbox
    try:
        ready = await engine.ensure_context_ready("web:c")
        cursor = sessions.get_cursor("web:c")
    finally:
        await engine.close()
        sessions.close()

    assert ready is False
    assert cursor == 0


@pytest.mark.asyncio
async def test_ingest_rejects_unknown_source_kind(tmp_path: Path) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config)
    try:
        result = await engine.ingest(MemoryIngestRequest(source_kind="file", content={}))
    finally:
        await engine.close()
        sessions.close()

    assert result.accepted is False
    assert "file" in result.summary


@pytest.mark.asyncio
async def test_turn_snapshot_can_be_rebuilt_from_persisted_message_ids(tmp_path: Path) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    user = sessions.add_message(NewMessage(session_key="web:c", role="user", content="原始问题"))
    assistant = sessions.add_message(NewMessage(
        session_key="web:c",
        role="assistant",
        content="原始回答",
        tool_chain=[{"calls": [{"name": "read_file", "result": "ok"}]}],
    ))
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config)
    captured = []

    async def capture(event):
        captured.append(event)

    engine._post_response.handle = capture
    try:
        await engine.on_turn_committed({
            "session_key": "web:c",
            "channel": "web",
            "chat_id": "c",
            "user_message_id": user["id"],
            "assistant_message_id": assistant["id"],
        })
        await engine.drain()
    finally:
        await engine.close()
        sessions.close()

    assert captured[0].user_message == "原始问题"
    assert captured[0].assistant_response == "原始回答"
    assert captured[0].tool_chain == assistant["tool_chain"]
    assert captured[0].source_ref == f'{user["id"]},{assistant["id"]}'


@pytest.mark.asyncio
async def test_post_response_failure_does_not_block_consolidation(tmp_path: Path) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    for index in range(4):
        sessions.add_message(NewMessage(session_key="web:c", role="user", content=f"消息 {index}"))
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(
        tmp_path,
        Embedder(),
        Provider(),
        sessions,
        config=config,
        consolidation_extractor=Extractor(),
        keep_count=1,
        consolidation_threshold=3,
    )

    async def fail(_event):
        raise RuntimeError("模拟失效处理失败")

    engine._post_response.handle = fail
    try:
        await engine.on_turn_committed({"session_key": "web:c", "channel": "web", "chat_id": "c"})
        await engine.drain()
        assert sessions.get_cursor("web:c") == 3
    finally:
        await engine.close()
        sessions.close()


@pytest.mark.asyncio
async def test_close_drains_pending_work_and_is_idempotent(tmp_path: Path) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    for index in range(4):
        sessions.add_message(NewMessage(session_key="web:c", role="user", content=f"消息 {index}"))
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(
        tmp_path,
        Embedder(),
        Provider(),
        sessions,
        config=config,
        consolidation_extractor=Extractor(),
        keep_count=1,
        consolidation_threshold=3,
    )

    await engine.on_turn_committed({"session_key": "web:c", "channel": "web", "chat_id": "c"})
    await engine.close()
    await engine.close()

    assert sessions.get_cursor("web:c") == 3
    sessions.close()


@pytest.mark.asyncio
async def test_committed_turn_invalidates_old_preference_through_engine_queue(tmp_path: Path) -> None:
    class InvalidationProvider:
        async def complete(self, messages, tools=None):
            prompt = messages[0]["content"]
            if "受影响的行为主题" in prompt:
                return SimpleNamespace(content='["回答格式"]')
            if "候选规则" in prompt:
                # 候选 ID 由引擎生成，因此从确认提示中提取，模拟 LLM 精确选择旧规则。
                candidate_id = prompt.split("id=", 1)[1].split(" |", 1)[0]
                return SimpleNamespace(content=f'["{candidate_id}"]')
            return SimpleNamespace(content="[]")

    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), InvalidationProvider(), sessions, config=config)
    try:
        old = await engine.mutate(MemoryMutation(
            kind="remember",
            summary="回答必须使用表格格式",
            memory_kind="preference",
            source_ref="web:c:0",
            scope=MemoryScope(channel="web", chat_id="c"),
        ))
        await engine.on_turn_committed({
            "session_key": "web:c",
            "channel": "web",
            "chat_id": "c",
            "input_message": "之前的回答格式错了，不要再使用表格",
            "assistant_response": "明白",
            "source_ref": "web:c@turn-2",
        })
        await engine.drain()
        recalled = await engine.query(MemoryQuery("回答格式"))
    finally:
        await engine.close()
        sessions.close()

    assert old.item_id
    assert recalled.records == []


@pytest.mark.asyncio
async def test_consolidation_maintenance_runs_different_sessions_concurrently(tmp_path: Path) -> None:
    import asyncio

    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config)
    both_started = asyncio.Event()
    release = asyncio.Event()
    started: set[str] = set()

    async def slow(event):
        started.add(event.session_key)
        if len(started) == 2:
            both_started.set()
        await release.wait()

    engine._run_consolidation = slow
    try:
        await engine.on_turn_committed({"session_key": "web:a"})
        await engine.on_turn_committed({"session_key": "web:b"})
        await asyncio.wait_for(both_started.wait(), timeout=0.05)
        release.set()
        await engine.drain()
    finally:
        release.set()
        await engine.close()
        sessions.close()

    assert started == {"web:a", "web:b"}


@pytest.mark.asyncio
async def test_consolidation_maintenance_serializes_same_session(tmp_path: Path) -> None:
    import asyncio

    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config)
    active = 0
    max_active = 0

    async def observe(_event):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1

    engine._run_consolidation = observe
    try:
        await engine.on_turn_committed({"session_key": "web:a"})
        await engine.on_turn_committed({"session_key": "web:a"})
        await engine.drain()
    finally:
        await engine.close()
        sessions.close()

    assert max_active == 1


@pytest.mark.asyncio
async def test_implicit_memory_failure_keeps_outbox_for_recovery(tmp_path: Path) -> None:
    class FailingImplicitExtractor:
        async def extract(self, conversation, existing_profile=""):
            raise RuntimeError("隐式提取失败")

    sessions = SessionStore(tmp_path / "sessions.db")
    for index in range(4):
        sessions.add_message(NewMessage(session_key="web:c", role="user", content=f"消息 {index}"))
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(
        tmp_path, Embedder(), Provider(), sessions, config=config,
        consolidation_extractor=Extractor(), implicit_extractor=FailingImplicitExtractor(),
        keep_count=1, consolidation_threshold=3,
    )
    try:
        await engine.on_turn_committed({"session_key": "web:c", "channel": "web", "chat_id": "c"})
        await engine.drain()
        cursor = sessions.get_cursor("web:c")
        pending_before = engine._store.list_pending_consolidations()
        engine._implicit_extractor = type("Recovered", (), {
            "extract": lambda self, conversation, existing_profile="": _empty_implicit()
        })()
        await engine.replay_pending_consolidations()
        pending_after = engine._store.list_pending_consolidations()
    finally:
        await engine.close()
        sessions.close()

    assert cursor == 3
    assert len(pending_before) == 1
    assert pending_after == []


@pytest.mark.asyncio
async def test_event_write_and_implicit_extraction_run_concurrently(tmp_path: Path) -> None:
    import asyncio

    from memory.events import ConsolidationCommitted

    both_started = asyncio.Event()
    release = asyncio.Event()
    started: set[str] = set()

    async def wait_for_peer(name: str) -> None:
        started.add(name)
        if len(started) == 2:
            both_started.set()
        await release.wait()

    class CoordinatedMemorizer:
        async def save_from_consolidation(self, *args, **kwargs):
            await wait_for_peer("event_write")

    class CoordinatedImplicitExtractor:
        async def extract(self, conversation, existing_profile=""):
            await wait_for_peer("implicit_extract")
            return ImplicitMemoryDraft()

    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(
        tmp_path,
        Embedder(),
        Provider(),
        sessions,
        config=config,
        implicit_extractor=CoordinatedImplicitExtractor(),
    )
    engine._memorizer = CoordinatedMemorizer()
    event = ConsolidationCommitted(
        history_entry_payloads=[("[2026-08-03 20:00] 用户测试并发归档", 0)],
        source_ref="web:c@0-9",
        scope_channel="web",
        scope_chat_id="c",
        conversation="[user] 测试并发归档",
    )
    engine._store.enqueue_consolidation(event.source_ref, {"source_ref": event.source_ref})
    processing = asyncio.create_task(engine.on_consolidation_committed(event))
    pending_after = None
    try:
        await asyncio.wait_for(both_started.wait(), timeout=0.5)
        release.set()
        await processing
        pending_after = engine._store.list_pending_consolidations()
    finally:
        release.set()
        await asyncio.gather(processing, return_exceptions=True)
        await engine.close()
        sessions.close()

    assert started == {"event_write", "implicit_extract"}
    assert pending_after == []


async def _empty_implicit():
    return ImplicitMemoryDraft()


@pytest.mark.asyncio
async def test_consolidation_saves_all_implicit_memory_types(tmp_path: Path) -> None:
    class ImplicitExtractor:
        async def extract(self, conversation, existing_profile=""):
            return ImplicitMemoryDraft(
                profile=[{"summary": "用户是后端开发者", "category": "personal_fact"}],
                preference=[{"summary": "用户偏好中文回答"}],
                procedure=[{"summary": "修改后运行测试", "tool_requirement": "shell", "steps": ["运行测试"]}],
            )

    sessions = SessionStore(tmp_path / "sessions.db")
    for index in range(4):
        sessions.add_message(NewMessage(session_key="web:c", role="user", content=f"消息 {index}"))
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(
        tmp_path, Embedder(), Provider(), sessions, config=config,
        consolidation_extractor=Extractor(), implicit_extractor=ImplicitExtractor(),
        keep_count=1, consolidation_threshold=3,
    )
    try:
        await engine.on_turn_committed({"session_key": "web:c", "channel": "web", "chat_id": "c"})
        await engine.drain()
        items = engine._store.get_items_by_ids([str(row["id"]) for row in engine._store._active_rows(None)])
        cursor = sessions.get_cursor("web:c")
    finally:
        await engine.close()
        sessions.close()

    assert {item["memory_type"] for item in items} == {"event", "profile", "preference", "procedure"}
    procedure = next(item for item in items if item["memory_type"] == "procedure")
    assert procedure["extra_json"]["tool_requirement"] == "shell"
    assert procedure["extra_json"]["steps"] == ["运行测试"]
    assert cursor == 3
