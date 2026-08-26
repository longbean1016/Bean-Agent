"""上下文 token gate 的纯函数测试。"""

from __future__ import annotations

from agent.context_budget import (
    estimate_payload_tokens,
    hard_input_limit,
    should_compact,
    soft_limit_tokens,
)

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
