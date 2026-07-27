"""主动策略 profile 与会话门禁测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from proactive.models import ProactiveState, SessionProactiveSettings
from proactive.policy import check_conversation_gate, resolve_policy


def test_activity_profiles_map_to_akashic_style_values() -> None:
    assert resolve_policy(SessionProactiveSettings("web:a")).judge_send_threshold == 0.75
    assert resolve_policy(SessionProactiveSettings("web:a", activity_level="balanced")).tick_interval_s0 == 900
    assert resolve_policy(SessionProactiveSettings("web:a", activity_level="active")).probability_max == 0.70


def test_dev_verify_profile_uses_akashic_testing_values() -> None:
    policy = resolve_policy(SessionProactiveSettings("web:a", activity_level="dev_verify"))

    assert policy.tick_interval_s0 == 60
    assert policy.tick_interval_s1 == 30
    assert policy.tick_jitter == 0
    assert policy.judge_send_threshold == 0.28
    assert policy.probability_min == 0.75
    assert policy.probability_max == 0.98
    assert policy.idle_scale_minutes == 15


def test_gate_checks_busy_quiet_and_daily_limit() -> None:
    now = datetime(2026, 7, 20, 23, 30, tzinfo=timezone.utc)
    settings = SessionProactiveSettings("web:a", conversation_enabled=True, timezone="UTC")
    state = ProactiveState("web:a")
    assert check_conversation_gate(settings, state, now=now, passive_busy=True).reason == "busy"
    assert check_conversation_gate(settings, state, now=now, passive_busy=False).reason == "quiet_hours"
