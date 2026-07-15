"""MemoryEngine 的记、查、引用、忘与 Turn 归档集成测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.config_models import MemoryConfig
from memory.consolidator import ConsolidationDraft
from memory.contracts import MemoryMutation, MemoryQuery, MemoryScope
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
        result = await engine.on_turn_committed(type("Event", (), {"session_key": "web:c", "channel": "web", "chat_id": "c"})())
        recalled = await engine.query(MemoryQuery("完成项目"))
        cursor = sessions.get_cursor("web:c")
    finally:
        await engine.close()
        sessions.close()

    assert result is not None
    assert cursor == 4
    assert any(record.summary == "完成项目" for record in recalled.records)
    assert engine.tool_profile().recall is not None
    assert engine.tool_profile().forget.parameters["required"] == ["ids"]
