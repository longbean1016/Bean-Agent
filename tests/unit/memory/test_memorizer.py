"""记忆写入器的强化、合并、替代和事件去重测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from memory.memorizer import Memorizer
from memory.store import MemoryStore2


class Embedder:
    async def embed(self, text: str) -> list[float]:
        return [1.0, 0.0]


@pytest.mark.asyncio
async def test_same_summary_reinforces_existing_item(tmp_path: Path) -> None:
    store = MemoryStore2(tmp_path / "memory.db", vec_dim=2)
    memorizer = Memorizer(store, Embedder())
    try:
        first = await memorizer.save_item("偏好简洁", "preference", {}, "web:c:0")
        second = await memorizer.save_item("偏好简洁", "preference", {}, "web:c:1")
        item = store.get_items_by_ids([first.split(":", 1)[1]])[0]
    finally:
        store.close()

    assert second.startswith("reinforced:")
    assert item["reinforcement"] == 2


@pytest.mark.asyncio
async def test_explicit_procedure_merges_same_tool_requirement(tmp_path: Path) -> None:
    store = MemoryStore2(tmp_path / "memory.db", vec_dim=2)
    memorizer = Memorizer(store, Embedder())
    try:
        first = await memorizer.save_item("提交前跑测试", "procedure", {"tool_requirement": "pytest", "steps": ["跑测试"]}, "web:c:0")
        merged = await memorizer.save_item_with_supersede("测试后检查差异", "procedure", {"tool_requirement": "pytest", "steps": ["检查差异"]}, "web:c:1", merge_threshold=0.7)
        item = store.get_items_by_ids([first.split(":", 1)[1]])[0]
    finally:
        store.close()

    assert merged.startswith("merged:")
    assert item["summary"] == "提交前跑测试；测试后检查差异"
    assert item["extra_json"]["steps"] == ["跑测试", "检查差异"]


@pytest.mark.asyncio
async def test_consolidation_semantic_duplicate_reinforces_instead_of_inserting(tmp_path: Path) -> None:
    store = MemoryStore2(tmp_path / "memory.db", vec_dim=2)
    memorizer = Memorizer(store, Embedder())
    try:
        await memorizer.save_from_consolidation("[2026-01-01] 完成发布", [], "web:c:t1", "web", "c")
        await memorizer.save_from_consolidation("[2026-01-02] 完成另一次发布", [], "web:c:t2", "web", "c")
        events = store.keyword_search_summary(["完成"])
    finally:
        store.close()

    assert len(events) == 1
    assert events[0]["reinforcement"] == 2
