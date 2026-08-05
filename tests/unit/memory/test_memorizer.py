"""记忆写入器的强化、合并、替代和事件去重测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from memory.memorizer import Memorizer
from memory.store import MemoryStore2
from memory.write_decider import _decision_prompt


class Embedder:
    async def embed(self, text: str) -> list[float]:
        return [1.0, 0.0]


class BatchEmbedder(Embedder):
    def __init__(self) -> None:
        self.batch_calls = 0

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls += 1
        return [[1.0, 0.0] for _ in texts]


class DecisionProvider:
    def __init__(self, actions: list[dict[str, object]]) -> None:
        self.actions = list(actions)

    async def complete(self, messages, tools=None, **kwargs):
        action = self.actions.pop(0)
        return SimpleNamespace(tool_calls=[SimpleNamespace(
            name="submit_memory_write_decision",
            arguments=action,
        )])


@pytest.mark.parametrize("memory_type", ["event", "profile", "preference", "procedure"])
def test_write_decision_prompt_defines_actions_and_type_boundaries(memory_type: str) -> None:
    prompt = _decision_prompt(
        memory_type,
        "新记忆",
        {"subject": "用户"},
        [{
            "id": "old-1",
            "summary": "旧记忆",
            "happened_at": "2026-08-03 10:00",
            "extra_json": {"subject": "用户"},
            "vector_score": 0.93,
        }],
    )

    for action in ("create", "reinforce", "merge", "supersede", "no_change"):
        assert f"`{action}`" in prompt
    for criterion in ("主体", "属性或场景", "时间、次数或阶段", "能否同时成立", "新增有效信息"):
        assert criterion in prompt
    assert "相似度只用于召回候选" in prompt
    assert "不确定" in prompt and "`create`" in prompt
    assert "target_id" in prompt and "merged_summary" in prompt
    assert "正反例" in prompt


def test_write_decision_prompt_contains_critical_cross_type_examples() -> None:
    prompts = {
        memory_type: _decision_prompt(memory_type, "新记忆", {}, [{"id": "old-1", "summary": "旧记忆"}])
        for memory_type in ("event", "profile", "preference", "procedure")
    }

    assert "第一次部署" in prompts["event"] and "第二次部署" in prompts["event"]
    assert "不同事件" in prompts["event"] and "禁止 `merge`" in prompts["event"]
    assert "不同主体" in prompts["profile"] and "搬到上海" in prompts["profile"]
    assert "同义复述" in prompts["preference"] and "明确反转" in prompts["preference"]
    assert "适用场景" in prompts["procedure"] and "工具要求" in prompts["procedure"]


@pytest.mark.asyncio
async def test_same_summary_reinforces_existing_item(tmp_path: Path) -> None:
    store = MemoryStore2(tmp_path / "memory.db", vec_dim=2)
    memorizer = Memorizer(store, Embedder(), provider=DecisionProvider([]))
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
    provider = DecisionProvider([{"action": "merge", "target_id": "", "merged_summary": "提交前跑测试；测试后检查差异"}])
    memorizer = Memorizer(store, Embedder(), provider=provider)
    try:
        first = await memorizer.save_item("提交前跑测试", "procedure", {"tool_requirement": "pytest", "steps": ["跑测试"]}, "web:c:0")
        provider.actions[0]["target_id"] = first.split(":", 1)[1]
        merged = await memorizer.save_item_with_supersede("测试后检查差异", "procedure", {"tool_requirement": "pytest", "steps": ["检查差异"]}, "web:c:1")
        item = store.get_items_by_ids([first.split(":", 1)[1]])[0]
    finally:
        store.close()

    assert merged.startswith("merged:")
    assert item["summary"] == "提交前跑测试；测试后检查差异"
    assert item["extra_json"]["steps"] == ["跑测试", "检查差异"]
    assert item["extra_json"]["rule_schema"]["required_tools"] == ["pytest"]


@pytest.mark.asyncio
async def test_consolidation_semantic_duplicate_reinforces_instead_of_inserting(tmp_path: Path) -> None:
    store = MemoryStore2(tmp_path / "memory.db", vec_dim=2)
    provider = DecisionProvider([{"action": "reinforce", "target_id": "", "merged_summary": ""}])
    memorizer = Memorizer(store, Embedder(), provider=provider)
    try:
        await memorizer.save_from_consolidation("[2026-01-01] 完成发布", [], "web:c:t1", "web", "c")
        first = store.keyword_search_summary(["完成"])[0]
        provider.actions[0]["target_id"] = first["id"]
        await memorizer.save_from_consolidation("[2026-01-01] 已完成发布", [], "web:c:t2", "web", "c")
        events = store.keyword_search_summary(["完成"])
    finally:
        store.close()

    assert len(events) == 1
    assert events[0]["reinforcement"] == 2


@pytest.mark.asyncio
async def test_distinct_timed_events_use_semantic_decision_and_remain_active(tmp_path: Path) -> None:
    store = MemoryStore2(tmp_path / "memory.db", vec_dim=2)
    provider = DecisionProvider([{"action": "create", "target_id": "", "merged_summary": ""}])
    memorizer = Memorizer(store, Embedder(), provider=provider)
    try:
        await memorizer.save_from_consolidation(
            "[2026-08-03 10:00] 用户完成第一次部署", [], "web:c:t1", "web", "c"
        )
        await memorizer.save_from_consolidation(
            "[2026-08-07 16:00] 用户完成第二次部署", [], "web:c:t2", "web", "c"
        )
        events = store.vector_search([1.0, 0.0], memory_types=["event"])
    finally:
        store.close()

    assert len(events) == 2
    assert all(item["status"] == "active" for item in events)


@pytest.mark.asyncio
async def test_conflicting_procedure_cannot_string_merge(tmp_path: Path) -> None:
    store = MemoryStore2(tmp_path / "memory.db", vec_dim=2)
    provider = DecisionProvider([{"action": "supersede", "target_id": "", "merged_summary": ""}])
    memorizer = Memorizer(store, Embedder(), provider=provider)
    try:
        first = await memorizer.save_item(
            "部署时必须使用 shell", "procedure",
            {"tool_requirement": "shell", "rule_schema": {"required_tools": ["shell"], "forbidden_tools": [], "mentioned_tools": ["shell"]}},
            "web:c:0",
        )
        provider.actions[0]["target_id"] = first.split(":", 1)[1]
        result = await memorizer.save_item_with_supersede(
            "部署时禁止使用 shell", "procedure",
            {"rule_schema": {"required_tools": [], "forbidden_tools": ["shell"], "mentioned_tools": ["shell"]}},
            "web:c:1",
        )
        rows = store.get_items_by_ids([str(row["id"]) for row in store._db.execute("SELECT id FROM memory_items")])
    finally:
        store.close()

    assert result.startswith("new:")
    assert {item["status"] for item in rows} == {"active", "superseded"}
    assert all("；" not in item["summary"] for item in rows)


@pytest.mark.asyncio
async def test_same_type_batch_uses_one_embedding_request(tmp_path: Path) -> None:
    store = MemoryStore2(tmp_path / "memory.db", vec_dim=2)
    embedder = BatchEmbedder()
    memorizer = Memorizer(
        store, embedder,
        provider=DecisionProvider([{"action": "create", "target_id": "", "merged_summary": ""}]),
    )
    try:
        results = await memorizer.save_items_batch([
            ("用户喜欢简洁回答", "preference", {}, "web:c:0", None, 0),
            ("用户喜欢代码示例", "preference", {}, "web:c:1", None, 0),
        ])
    finally:
        store.close()

    assert embedder.batch_calls == 1
    assert len(results) == 2


@pytest.mark.asyncio
async def test_preference_semantic_reinforce_keeps_one_active_item(tmp_path: Path) -> None:
    store = MemoryStore2(tmp_path / "memory.db", vec_dim=2)
    first = store.upsert_item("preference", "用户喜欢简洁回答", [1.0, 0.0], "web:c:0")
    item_id = first.split(":", 1)[1]
    memorizer = Memorizer(store, Embedder(), provider=DecisionProvider([
        {"action": "reinforce", "target_id": item_id, "merged_summary": ""}
    ]))
    try:
        result = await memorizer.save_item_with_supersede(
            "用户偏好简明回复", "preference", {}, "web:c:1"
        )
        rows = store.vector_search([1.0, 0.0], memory_types=["preference"])
    finally:
        store.close()

    assert result == f"reinforced:{item_id}"
    assert len(rows) == 1
    assert rows[0]["reinforcement"] == 2


@pytest.mark.asyncio
async def test_profile_semantic_supersede_changes_single_value_fact(tmp_path: Path) -> None:
    store = MemoryStore2(tmp_path / "memory.db", vec_dim=2)
    first = store.upsert_item(
        "profile", "用户居住在上海", [1.0, 0.0], "web:c:0", extra={"category": "status"}
    )
    item_id = first.split(":", 1)[1]
    memorizer = Memorizer(store, Embedder(), provider=DecisionProvider([
        {"action": "supersede", "target_id": item_id, "merged_summary": ""}
    ]))
    try:
        result = await memorizer.save_item_with_supersede(
            "用户现居杭州", "profile", {"category": "status"}, "web:c:1"
        )
        rows = store.get_items_by_ids([
            str(row["id"]) for row in store._db.execute("SELECT id FROM memory_items")
        ])
    finally:
        store.close()

    assert result.startswith("new:")
    assert sorted(item["status"] for item in rows) == ["active", "superseded"]


@pytest.mark.asyncio
async def test_subset_preference_is_no_change_even_if_model_requests_merge(tmp_path: Path) -> None:
    store = MemoryStore2(tmp_path / "memory.db", vec_dim=2)
    first = store.upsert_item(
        "preference", "用户喜欢简洁回答并附带必要代码示例", [1.0, 0.0], "web:c:0"
    )
    item_id = first.split(":", 1)[1]
    memorizer = Memorizer(store, Embedder(), provider=DecisionProvider([
        {"action": "merge", "target_id": item_id, "merged_summary": "用户喜欢简洁回答并附带必要代码示例"}
    ]))
    try:
        result = await memorizer.save_item_with_supersede(
            "用户喜欢简洁回答", "preference", {}, "web:c:1"
        )
        item = store.get_items_by_ids([item_id])[0]
    finally:
        store.close()

    assert result == f"unchanged:{item_id}"
    assert item["reinforcement"] == 1
