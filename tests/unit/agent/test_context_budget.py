"""上下文退避计划的纯函数测试。"""

from __future__ import annotations

from agent.context_budget import DEFAULT_CONTEXT_RETRY_PLANS, slice_complete_turns


def test_default_context_retry_plans_follow_documented_order() -> None:
    assert [plan.name for plan in DEFAULT_CONTEXT_RETRY_PLANS] == [
        "full",
        "trim_skills_catalog",
        "trim_auxiliary_context",
        "trim_retrieved_memory",
        "trim_long_term_memory",
        "half_history",
        "no_history",
    ]
    assert DEFAULT_CONTEXT_RETRY_PLANS[-2].history_ratio == 0.5
    assert DEFAULT_CONTEXT_RETRY_PLANS[-1].history_ratio == 0.0


def test_slice_complete_turns_keeps_latest_tool_turn_intact() -> None:
    history = [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
        {"role": "assistant", "content": "第二答"},
        {"role": "user", "content": "最近问题"},
        {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "结果"},
        {"role": "assistant", "content": "最近回答"},
    ]

    sliced = slice_complete_turns(history, 0.5)

    assert [item["role"] for item in sliced] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert sliced[0]["content"] == "最近问题"


def test_slice_complete_turns_drops_orphan_chain_and_supports_zero_history() -> None:
    orphaned = [
        {"role": "assistant", "content": "孤立回答"},
        {"role": "tool", "content": "孤立结果"},
    ]

    assert slice_complete_turns(orphaned, 0.5) == []
    assert slice_complete_turns([{"role": "user", "content": "问题"}], 0.0) == []
