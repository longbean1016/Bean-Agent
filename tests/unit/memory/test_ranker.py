"""记忆热度、时间衰减和语义融合排序测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memory.ranker import combine_scores, hotness_score


def test_hotness_matches_reference_formula_at_half_life() -> None:
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    updated = now - timedelta(days=14)

    score = hotness_score(1, updated, now=now, half_life_days=14, emotional_weight=0)
    frequency = 1.0 / (1.0 + __import__("math").exp(-__import__("math").log1p(1)))

    assert score == pytest.approx(frequency * 0.5)


def test_emotional_weight_extends_effective_half_life() -> None:
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    updated = now - timedelta(days=14)

    normal = hotness_score(1, updated, now=now, emotional_weight=0)
    emotional = hotness_score(1, updated, now=now, emotional_weight=10)

    assert emotional > normal


def test_combine_scores_clamps_alpha() -> None:
    assert combine_scores(0.8, 0.2, alpha=0.25) == pytest.approx(0.65)
    assert combine_scores(0.8, 0.2, alpha=2.0) == pytest.approx(0.2)
