"""记忆向量存储的幂等、scope、检索和软删除测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from memory.store import MemoryStore2
from memory.md_store import DEFAULT_BEAN_MD, MarkdownMemoryStore


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


def test_keyword_search_filters_and_scores_in_sql(
    store: MemoryStore2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stronger = store.upsert_item(
        "profile",
        "用户使用 Fitbit Charge-6 跑步",
        [1.0, 0.0, 0.0],
        "web:a:0",
        extra={"scope_channel": "web", "scope_chat_id": "a"},
    ).split(":", 1)[1]
    weaker = store.upsert_item(
        "profile",
        "用户使用 Fitbit 记录运动",
        [0.0, 1.0, 0.0],
        "web:a:1",
        extra={"scope_channel": "web", "scope_chat_id": "a"},
    ).split(":", 1)[1]
    store.upsert_item(
        "event",
        "购买 Fitbit Charge-6",
        [0.0, 0.0, 1.0],
        "web:a:2",
        extra={"scope_channel": "web", "scope_chat_id": "a"},
    )

    def fail_full_scan(*args: object, **kwargs: object) -> list[object]:
        raise AssertionError("关键词查询不应加载全部 active rows")

    monkeypatch.setattr(store, "_active_rows", fail_full_scan)

    items = store.keyword_search_summary(
        ["Fitbit", "Charge-6"],
        memory_types=["profile"],
        scope_channel="web",
        scope_chat_id="a",
        require_scope_match=True,
    )

    assert [item["id"] for item in items] == [stronger, weaker]
    assert [item["keyword_score"] for item in items] == [1.0, 0.5]
    assert items[0]["extra_json"]["scope_chat_id"] == "a"


def test_soft_delete_removes_item_from_search_but_keeps_audit_row(store: MemoryStore2) -> None:
    result = store.upsert_item("profile", "用户是开发者", [0.0, 1.0, 0.0], "web:c:2")
    item_id = result.split(":", 1)[1]

    affected, missing = store.mark_superseded_batch([item_id, "missing"])

    assert affected == [item_id]
    assert missing == ["missing"]
    assert store.vector_search([0.0, 1.0, 0.0]) == []
    assert store.get_items_by_ids([item_id])[0]["status"] == "superseded"


def test_atomic_replace_rolls_back_when_new_vector_write_fails(
    store: MemoryStore2,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_id = store.upsert_item(
        "preference", "用户喜欢简洁回答", [1.0, 0.0, 0.0], "web:c:0"
    ).split(":", 1)[1]

    def fail_vector_write(rowid: int, vector: list[float]) -> None:
        raise RuntimeError("simulated vector failure")

    monkeypatch.setattr(store, "_vec_insert", fail_vector_write)
    with pytest.raises(RuntimeError, match="simulated vector failure"):
        store.replace_item_atomic(
            old_id, "preference", "用户喜欢详细回答", [0.0, 1.0, 0.0], "web:c:1"
        )

    old = store.get_items_by_ids([old_id])[0]
    count = store._db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
    assert old["status"] == "active"
    assert count == 1


def test_consolidation_source_ref_is_idempotent(store: MemoryStore2) -> None:
    first = store.upsert_consolidation_event("web:c:turn-1", "完成发布", [0.0, 0.0, 1.0])
    second = store.upsert_consolidation_event("web:c:turn-1", "完成发布", [0.0, 0.0, 1.0])

    assert first.startswith("saved:")
    assert second == "skipped:web:c:turn-1"
    assert store.has_consolidation_source_ref("web:c:turn-1") is True


def test_markdown_store_initializes_and_reads_bean_persona(tmp_path: Path) -> None:
    markdown = MarkdownMemoryStore(tmp_path)
    try:
        assert (tmp_path / "memory" / "BEAN.md").read_text(encoding="utf-8") == DEFAULT_BEAN_MD
        assert markdown.read_bean() == DEFAULT_BEAN_MD
    finally:
        markdown.close()


def test_markdown_store_does_not_overwrite_custom_bean_persona(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    custom = "# Bean\n\n自定义人格。\n"
    (memory_dir / "BEAN.md").write_text(custom, encoding="utf-8")
    markdown = MarkdownMemoryStore(tmp_path)
    try:
        assert markdown.read_bean() == custom
    finally:
        markdown.close()


def test_list_events_by_time_range_uses_happened_at_and_orders_ascending(
    store: MemoryStore2,
) -> None:
    older = store.upsert_item(
        "event", "完成需求分析", [1.0, 0.0, 0.0], "web:a:0",
        happened_at="2026-07-18T09:00:00+00:00",
    ).split(":", 1)[1]
    newer = store.upsert_item(
        "event", "完成代码实现", [1.0, 0.0, 0.0], "web:b:0",
        happened_at="2026-07-19T10:00:00+00:00",
    ).split(":", 1)[1]
    superseded = store.upsert_item(
        "event", "已经过期的计划", [1.0, 0.0, 0.0], "web:c:0",
        happened_at="2026-07-19T12:00:00+00:00",
    ).split(":", 1)[1]
    store.mark_superseded_batch([superseded])
    store.upsert_item(
        "preference", "偏好简洁回答", [1.0, 0.0, 0.0], "web:d:0",
        happened_at="2026-07-19T11:00:00+00:00",
    )
    store.upsert_item(
        "event", "没有事件时间", [1.0, 0.0, 0.0], "web:e:0",
    )

    items = store.list_events_by_time_range(
        datetime(2026, 7, 18, tzinfo=timezone.utc),
        datetime(2026, 7, 20, tzinfo=timezone.utc),
    )

    assert [item["id"] for item in items] == [older, newer]
    assert [item["score"] for item in items] == [1.0, 1.0]
