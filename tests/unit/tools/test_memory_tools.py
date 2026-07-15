"""三个记忆工具与 MemoryEngine 协议之间的数据映射测试。"""

from __future__ import annotations

import json

import pytest

from memory.contracts import (
    EvidenceRef,
    MemoryMutationResult,
    MemoryQueryResult,
    MemoryRecord,
    MemoryToolSpec,
)
from tools.forget_memory import ForgetMemoryTool
from tools.memorize import MemorizeTool
from tools.recall_memory import RecallMemoryTool


class FakeMemory:
    def __init__(self) -> None:
        self.query_request = None
        self.mutations = []

    async def query(self, request):
        self.query_request = request
        return MemoryQueryResult(
            records=[
                MemoryRecord(
                    id="mem-1",
                    kind="preference",
                    summary="用户喜欢简洁回答",
                    score=0.92345,
                    evidence=[EvidenceRef(kind="message", refs=["web:c:0"], source_ref="web:c:0")],
                    signals={"reinforcement": 2},
                )
            ],
            trace={"route": "hybrid"},
        )

    async def mutate(self, mutation):
        self.mutations.append(mutation)
        if mutation.kind == "forget":
            return MemoryMutationResult(
                affected_ids=["mem-1"], missing_ids=["missing"], items=[{"id": "mem-1"}]
            )
        return MemoryMutationResult(item_id="mem-2", status="new", actual_kind="procedure")


def spec(name: str) -> MemoryToolSpec:
    return MemoryToolSpec(
        description=f"动态 {name} 描述",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}},
    )


@pytest.mark.asyncio
async def test_recall_uses_dynamic_spec_scope_and_citation_payload() -> None:
    memory = FakeMemory()
    tool = RecallMemoryTool(memory, spec("检索"))

    result = json.loads(
        await tool.execute(
            query="回答风格",
            intent="answer",
            memory_kind="preference",
            time_filter="recent_7d",
            channel="web",
            chat_id="c",
        )
    )

    assert tool.description == "动态 检索 描述"
    assert memory.query_request.scope.session_key == "web:c"
    assert memory.query_request.filters.kinds == ("preference",)
    assert memory.query_request.filters.time_start is not None
    assert result["items"][0]["source_ref"] == "web:c:0"
    assert result["citation_required"] is True


@pytest.mark.asyncio
async def test_memorize_maps_source_scope_and_procedure_metadata() -> None:
    memory = FakeMemory()
    tool = MemorizeTool(memory, spec("写入"))

    result = await tool.execute(
        summary="发布前运行测试",
        memory_kind="procedure",
        tool_requirement="pytest",
        steps=["运行测试", "检查结果"],
        current_user_source_ref="web:c:8",
        channel="web",
        chat_id="c",
    )

    mutation = memory.mutations[0]
    assert mutation.scope.session_key == "web:c"
    assert mutation.source_ref == "web:c:8"
    assert mutation.metadata["tool_requirement"] == "pytest"
    assert "item_id=mem-2" in result


@pytest.mark.asyncio
async def test_forget_deduplicates_ids_and_returns_soft_delete_details() -> None:
    memory = FakeMemory()
    tool = ForgetMemoryTool(memory, spec("遗忘"))

    result = json.loads(await tool.execute(ids=[" mem-1 ", "mem-1", "missing", ""]))

    assert memory.mutations[0].ids == ("mem-1", "missing")
    assert result["superseded_ids"] == ["mem-1"]
    assert result["missing_ids"] == ["missing"]
