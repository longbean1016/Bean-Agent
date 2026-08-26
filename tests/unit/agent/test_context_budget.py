"""上下文退避计划的纯函数测试。"""

from __future__ import annotations

from agent.context_budget import (
    DEFAULT_CONTEXT_RETRY_PLANS,
    estimate_payload_tokens,
    hard_input_limit,
    should_compact,
    slice_complete_turns,
    soft_limit_tokens,
)


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


def test_token_gate_uses_soft_or_hard_boundary() -> None:
    assert soft_limit_tokens(1000) == 740
    assert hard_input_limit(1000, 200) == 800
    assert should_compact(740, context_window=1000, max_output_tokens=200)
    assert should_compact(800, context_window=1000, max_output_tokens=200)
    assert not should_compact(739, context_window=1000, max_output_tokens=200)


def test_unknown_context_window_skips_local_gate() -> None:
    assert not should_compact(10_000, context_window=0, max_output_tokens=200)


def test_payload_estimate_includes_tools_and_protocol_overhead() -> None:
    base = estimate_payload_tokens([{"role": "user", "content": "问题"}])
    with_tool = estimate_payload_tokens(
        [{"role": "user", "content": "问题"}],
        [{"type": "function", "function": {"name": "read_file"}}],
    )
    assert with_tool > base
