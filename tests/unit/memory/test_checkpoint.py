"""Checkpoint source plan 的完整 Turn 边界测试。"""

from memory.checkpoint import (
    group_logical_units,
    select_compaction_units,
)


def _messages() -> list[dict[str, object]]:
    return [
        {"id": "s:0", "seq": 0, "role": "user", "content": "第一问", "turn_id": "t1"},
        {"id": "s:1", "seq": 1, "role": "assistant", "content": "第一答", "turn_id": "t1", "tool_chain": [{"calls": ["x"]}]},
        {"id": "s:2", "seq": 2, "role": "user", "content": "第二问", "turn_id": "t2"},
        {"id": "s:3", "seq": 3, "role": "assistant", "content": "第二答", "turn_id": "t2"},
    ]


def test_group_keeps_user_and_assistant_tool_batch_together() -> None:
    units = group_logical_units(_messages())

    assert [[item["id"] for item in unit.messages] for unit in units] == [
        ["s:0", "s:1"],
        ["s:2", "s:3"],
    ]


def test_group_splits_legacy_messages_without_turn_id_by_user_boundary() -> None:
    units = group_logical_units([
        {"id": "s:0", "seq": 0, "role": "user", "content": "第一问"},
        {"id": "s:1", "seq": 1, "role": "assistant", "content": "第一答"},
        {"id": "s:2", "seq": 2, "role": "user", "content": "第二问"},
        {"id": "s:3", "seq": 3, "role": "assistant", "content": "第二答"},
    ])

    assert [[item["id"] for item in unit.messages] for unit in units] == [
        ["s:0", "s:1"],
        ["s:2", "s:3"],
    ]


def test_selection_returns_complete_prefix_and_tail() -> None:
    units = group_logical_units(_messages())

    selected, retained = select_compaction_units(units, keep_recent_tokens=1)

    assert [item["id"] for unit in selected for item in unit.messages] == ["s:0", "s:1"]
    assert [item["id"] for unit in retained for item in unit.messages] == ["s:2", "s:3"]
