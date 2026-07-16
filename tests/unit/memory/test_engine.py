"""MemoryEngine 的记、查、引用、忘与 Turn 归档集成测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.config_models import MemoryConfig
from memory.consolidator import ConsolidationDraft
from memory.contracts import MemoryIngestRequest, MemoryMutation, MemoryQuery, MemoryScope
from memory.engine import MemoryEngine
from session.store import NewMessage, SessionStore


class Embedder:
    async def embed(self, text: str) -> list[float]: return [1.0, 0.0]
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: return [[1.0, 0.0] for _ in texts]
    async def close(self) -> None: self.closed = True


class Provider:
    async def complete(self, messages, tools=None):
        return type("Response", (), {"content": "<decision>RETRIEVE</decision><history_query>回答风格</history_query>"})()


class Extractor:
    async def extract(self, messages, previous_recent_context):
        return ConsolidationDraft(history_entries=[{"summary": "完成项目", "emotional_weight": 1}], pending_items=[{"tag": "identity", "content": "用户是开发者"}], recent_context="# Recent Context\n- 项目开发")


@pytest.mark.asyncio
async def test_engine_remember_query_evidence_and_forget(tmp_path: Path) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    config = MemoryConfig(enabled=True)
    config.embedding.dimensions = 2
    engine = MemoryEngine(tmp_path, Embedder(), Provider(), sessions, config=config, consolidation_extractor=Extractor())
    try:
        written = await engine.mutate(MemoryMutation(kind="remember", summary="用户喜欢简洁回答", memory_kind="preference", source_ref="web:c:0", scope=MemoryScope(channel="web", chat_id="c")))
        recalled = await engine.query(MemoryQuery("回答风格", scope=MemoryScope(channel="web", chat_id="c")))
        forgotten = await engine.mutate(MemoryMutation(kind="forget", ids=(written.item_id,)))
        after = await engine.query(MemoryQuery("回答风格", scope=MemoryScope(channel="web", chat_id="c")))
    finally:
        await engine.close()
        sessions.close()

    assert recalled.records[0].evidence[0].source_ref == "web:c:0"
    assert forgotten.affected_ids == [written.item_id]
    assert after.records == []


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
        async def extract(self, messages, previous_recent_context):
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
