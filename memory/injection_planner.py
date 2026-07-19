"""在混合召回后按类型阈值和注入配额选择记忆。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class InjectionPlan:
    """注入结果及未选中条目的确定性原因。"""

    items: list[dict[str, object]] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)


class InjectionPlanner:
    """保持 RRF 顺序，并为两组记忆分别执行阈值和配额约束。"""

    def __init__(
        self,
        *,
        thresholds: dict[str, float],
        max_forced_procedures: int = 3,
        max_procedure_preference: int = 4,
        max_event_profile: int = 4,
    ) -> None:
        self._thresholds = {key: float(value) for key, value in thresholds.items()}
        self._max_forced = max(0, int(max_forced_procedures))
        self._max_rules = max(0, int(max_procedure_preference))
        self._max_facts = max(0, int(max_event_profile))

    def plan(self, items: list[dict[str, object]]) -> InjectionPlan:
        """筛选活动候选；未知类型不占用已知类型的配额。"""

        selected: list[dict[str, object]] = []
        rejected: dict[str, str] = {}
        forced_count = 0
        rule_count = 0
        fact_count = 0
        for item in items:
            item_id = str(item.get("id") or "")
            memory_type = str(item.get("memory_type") or "")
            score = float(item.get("score", 0.0) or 0.0)
            extra = item.get("extra_json")
            values = extra if isinstance(extra, dict) else {}
            if memory_type == "procedure" and values.get("tool_requirement"):
                if forced_count >= self._max_forced:
                    rejected[item_id] = "forced_procedure_quota"
                    continue
                forced_count += 1
                item["forced"] = True
                selected.append(item)
                continue
            if score < self._thresholds.get(memory_type, 0.0):
                rejected[item_id] = "below_type_threshold"
                continue
            if memory_type in {"procedure", "preference"}:
                if rule_count >= self._max_rules:
                    rejected[item_id] = "procedure_preference_quota"
                    continue
                rule_count += 1
            elif memory_type in {"event", "profile"}:
                if fact_count >= self._max_facts:
                    rejected[item_id] = "event_profile_quota"
                    continue
                fact_count += 1
            selected.append(item)
        return InjectionPlan(selected, rejected)


__all__ = ["InjectionPlan", "InjectionPlanner"]
