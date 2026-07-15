"""记忆热度、时间衰减与语义分数融合。"""

from __future__ import annotations

import math
from datetime import datetime, timezone


def hotness_score(
    reinforcement: int,
    updated_at: datetime,
    *,
    now: datetime | None = None,
    half_life_days: float = 14.0,
    emotional_weight: int = 0,
) -> float:
    """计算参考实现使用的频度乘时间衰减热度分。"""

    current = now or datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    # 情感权重只延长半衰期，不直接抬高当前分数，避免新记忆被重复加权。
    weight = max(0, min(int(emotional_weight), 10))
    effective_half_life = max(
        float(half_life_days) * (1.0 + 0.5 * weight / 10.0),
        0.1,
    )
    frequency = 1.0 / (
        1.0 + math.exp(-math.log1p(max(0, int(reinforcement))))
    )
    age_days = max((current - updated_at).total_seconds() / 86400.0, 0.0)
    recency = math.exp(-math.log(2) / effective_half_life * age_days)
    return frequency * recency


def combine_scores(semantic: float, hotness: float, *, alpha: float = 0.20) -> float:
    """按配置权重融合语义相关性与热度。"""

    safe_alpha = max(0.0, min(float(alpha), 1.0))
    return (1.0 - safe_alpha) * float(semantic) + safe_alpha * float(hotness)


__all__ = ["combine_scores", "hotness_score"]
