"""记忆向量存储的幂等、scope、检索和软删除测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from memory.store import MemoryStore2


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore2:
    value = MemoryStore2(tmp_path / "memory2.db", vec_dim=3)
    yield value
    value.close()


def test_upsert_reinforces_duplicate_and_preserves_single_item(store: MemoryStore2) -> None:
    first = store.upsert_item("preference", "喜欢简洁回答", [1.0, 0.0, 0.0], "web:c:0")
    second = store.upsert_item("preference", "喜欢简洁回答", [1.0, 0.0, 0.0], "web:c:1")

    assert first.startswith("new:")
    assert second == f"reinforced:{first.split(':', 1)[1]}"
    item = store.get_items_by_ids([first.split(":", 1)[1]])[0]
    assert item["reinforcement"] == 2


def test_vector_and_keyword_search_enforce_scope(store: MemoryStore2) -> None:
    store.upsert_item("event", "上海旅行计划", [1.0, 0.0, 0.0], "web:a:0", extra={"scope_channel": "web", "scope_chat_id": "a"})
    store.upsert_item("event", "上海天气记录", [0.9, 0.1, 0.0], "web:b:0", extra={"scope_channel": "web", "scope_chat_id": "b"})

    vector = store.vector_search([1.0, 0.0, 0.0], top_k=5, scope_channel="web", scope_chat_id="a", require_scope_match=True)
    keyword = store.keyword_search_summary(["上海"], scope_channel="web", scope_chat_id="b", require_scope_match=True)

    assert [item["source_ref"] for item in vector] == ["web:a:0"]
    assert [item["source_ref"] for item in keyword] == ["web:b:0"]


def test_soft_delete_removes_item_from_search_but_keeps_audit_row(store: MemoryStore2) -> None:
    result = store.upsert_item("profile", "用户是开发者", [0.0, 1.0, 0.0], "web:c:2")
    item_id = result.split(":", 1)[1]

    affected, missing = store.mark_superseded_batch([item_id, "missing"])

    assert affected == [item_id]
    assert missing == ["missing"]
    assert store.vector_search([0.0, 1.0, 0.0]) == []
    assert store.get_items_by_ids([item_id])[0]["status"] == "superseded"


def test_consolidation_source_ref_is_idempotent(store: MemoryStore2) -> None:
    first = store.upsert_consolidation_event("web:c:turn-1", "完成发布", [0.0, 0.0, 1.0])
    second = store.upsert_consolidation_event("web:c:turn-1", "完成发布", [0.0, 0.0, 1.0])

    assert first.startswith("saved:")
    assert second == "skipped:web:c:turn-1"
    assert store.has_consolidation_source_ref("web:c:turn-1") is True
