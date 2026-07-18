"""记忆注入按类型阈值和分组配额进行稳定筛选。"""

from __future__ import annotations

from memory.injection_planner import InjectionPlanner


def test_planner_applies_type_thresholds_and_group_quotas_in_order() -> None:
    planner = InjectionPlanner(
        thresholds={"procedure": 0.66, "preference": 0.5, "event": 0.5, "profile": 0.5},
        max_procedure_preference=2,
        max_event_profile=1,
    )
    items = [
        {"id": "p-low", "memory_type": "procedure", "score": 0.65},
        {"id": "p1", "memory_type": "procedure", "score": 0.90},
        {"id": "pref", "memory_type": "preference", "score": 0.80},
        {"id": "p2", "memory_type": "procedure", "score": 0.75},
        {"id": "event", "memory_type": "event", "score": 0.70},
        {"id": "profile", "memory_type": "profile", "score": 0.65},
    ]

    result = planner.plan(items)

    assert [item["id"] for item in result.items] == ["p1", "pref", "event"]
    assert result.rejected == {
        "p-low": "below_type_threshold",
        "p2": "procedure_preference_quota",
        "profile": "event_profile_quota",
    }
