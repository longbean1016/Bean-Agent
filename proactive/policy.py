"""把 Web 易懂设置转换为主动算法参数，并执行确定性门禁。"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from proactive.models import ProactiveState, SessionProactiveSettings


@dataclass(frozen=True, slots=True)
class ProactivePolicy:
    """内部 akashic 风格策略；这些数值不直接暴露给 Web 用户。"""

    tick_interval_s0: int
    tick_interval_s1: int
    tick_jitter: float
    judge_send_threshold: float
    probability_min: float
    probability_max: float
    idle_scale_minutes: float
    context_probability: float
    context_min_interval_hours: int


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reason: str


_POLICIES = {
    "restrained": ProactivePolicy(1800, 900, .30, .75, .05, .30, 120, .03, 24),
    "balanced": ProactivePolicy(900, 450, .25, .65, .10, .50, 60, .08, 12),
    "active": ProactivePolicy(480, 240, .20, .55, .20, .70, 30, .15, 6),
}


def resolve_policy(settings: SessionProactiveSettings) -> ProactivePolicy:
    """根据用户选择返回稳定策略，未知值 fail-fast 而不是静默放宽。"""

    try:
        return _POLICIES[settings.activity_level]
    except KeyError as error:
        raise ValueError(f"未知主动程度: {settings.activity_level}") from error


def check_conversation_gate(
    settings: SessionProactiveSettings,
    state: ProactiveState,
    *,
    now: datetime,
    passive_busy: bool,
) -> GateDecision:
    """先执行用户明确边界，再允许概率和 LLM 判断介入。"""

    if not settings.conversation_enabled:
        return GateDecision(False, "disabled")
    if passive_busy:
        return GateDecision(False, "busy")
    local_now = now.astimezone(ZoneInfo(settings.timezone))
    if settings.quiet_hours_enabled and _in_quiet_hours(local_now, settings.quiet_start, settings.quiet_end):
        return GateDecision(False, "quiet_hours")
    if state.daily_date == local_now.date().isoformat() and state.daily_count >= settings.daily_conversation_limit:
        return GateDecision(False, "daily_limit")
    if state.last_delivered_at:
        last = datetime.fromisoformat(state.last_delivered_at)
        if now.astimezone(last.tzinfo) - last < timedelta(hours=settings.min_conversation_interval_hours):
            return GateDecision(False, "cooldown")
    return GateDecision(True, "allowed")


def admission_probability(policy: ProactivePolicy, idle_minutes: float) -> float:
    """复用 akashic 的空闲增长思路，在上下限之间平滑提高准入概率。"""

    idle_factor = 1.0 - pow(2.718281828, -max(0.0, idle_minutes) / max(policy.idle_scale_minutes, 1.0))
    return policy.probability_min + (policy.probability_max - policy.probability_min) * idle_factor


def next_tick_seconds(policy: ProactivePolicy, rng: random.Random) -> int:
    """在两个基础间隔之间取值并加入 jitter，避免所有会话同时唤醒。"""

    base = rng.randint(policy.tick_interval_s1, policy.tick_interval_s0)
    factor = 1.0 + rng.uniform(-policy.tick_jitter, policy.tick_jitter)
    return max(1, int(base * factor))


def _in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    start_time = datetime.strptime(start, "%H:%M").time()
    end_time = datetime.strptime(end, "%H:%M").time()
    current = now.time().replace(second=0, microsecond=0)
    if start_time == end_time:
        return True
    if start_time < end_time:
        return start_time <= current < end_time
    return current >= start_time or current < end_time


__all__ = [
    "GateDecision",
    "ProactivePolicy",
    "admission_probability",
    "check_conversation_gate",
    "next_tick_seconds",
    "resolve_policy",
]
