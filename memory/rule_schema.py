"""从 Procedure 摘要和步骤构建工具约束，并判断规则方向冲突。"""

from __future__ import annotations

import re
from typing import Any

_ALIAS_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_NEGATIVE = ("不能使用", "不能用", "不要使用", "不要用", "别使用", "别用", "禁止使用", "禁止用")
_POSITIVE = ("必须先使用", "必须先用", "必须使用", "必须用", "先使用", "先用", "优先使用", "优先用", "应先使用", "应先用", "应该使用", "应该用", "直接使用", "直接用")


def build_procedure_rule_schema(
    summary: str,
    tool_requirement: str | None = None,
    steps: list[str] | None = None,
    rule_schema: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """优先保留显式 schema，并从自然语言补齐缺失的工具方向约束。"""

    payload = rule_schema or {}
    required = set(_normalize_list(payload.get("required_tools")))
    forbidden = set(_normalize_list(payload.get("forbidden_tools")))
    mentioned = set(_normalize_list(payload.get("mentioned_tools")))
    for text in [summary, *(steps or [])]:
        mentioned.update(_aliases(text))
    if not required or not forbidden:
        inferred_required, inferred_forbidden = _infer_constraints(summary, steps)
        if not required:
            required.update(inferred_required)
        if not forbidden:
            forbidden.update(inferred_forbidden)
    tool = str(tool_requirement or "").strip().lower()
    if tool:
        required.add(tool)
        mentioned.add(tool)
    # 显式必需工具优先，避免同一 schema 同时要求并禁止一个工具。
    forbidden.difference_update(required)
    return {
        "required_tools": sorted(required),
        "forbidden_tools": sorted(forbidden),
        "mentioned_tools": sorted(mentioned),
    }


def resolve_procedure_rule_schema(
    summary: str,
    extra: dict[str, Any] | None,
) -> dict[str, list[str]]:
    """从记忆条目的 extra_json 解析完整 Procedure schema。"""

    payload = extra or {}
    return build_procedure_rule_schema(
        summary,
        tool_requirement=payload.get("tool_requirement"),
        steps=payload.get("steps") or [],
        rule_schema=payload.get("rule_schema"),
    )


def procedure_rules_conflict(
    new_schema: dict[str, list[str]],
    old_schema: dict[str, list[str]],
) -> bool:
    """只有共享工具在两条规则中方向相反时才视为直接冲突。"""

    new_required = set(new_schema.get("required_tools") or [])
    new_forbidden = set(new_schema.get("forbidden_tools") or [])
    old_required = set(old_schema.get("required_tools") or [])
    old_forbidden = set(old_schema.get("forbidden_tools") or [])
    return bool((new_required & old_forbidden) or (new_forbidden & old_required))


def _normalize_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip().lower() for item in value if isinstance(item, str) and item.strip()})


def _aliases(text: str) -> set[str]:
    """提取工具风格 ASCII 标识，并合并由空白分隔的连续名称。"""

    matches = list(_ALIAS_RE.finditer(text or ""))
    values = {match.group(0).lower() for match in matches if len(match.group(0)) >= 2}
    for left, right in zip(matches, matches[1:]):
        if not text[left.end():right.start()].strip():
            values.add(f"{left.group(0).lower()}_{right.group(0).lower()}")
    return values


def _infer_constraints(summary: str, steps: list[str] | None) -> tuple[set[str], set[str]]:
    required: set[str] = set()
    forbidden: set[str] = set()
    for text in [summary, *(steps or [])]:
        for clause in re.split(r"[，。！？；;\n]", text or ""):
            for match in _ALIAS_RE.finditer(clause):
                alias = match.group(0).lower()
                prefix = re.sub(r"\s+", "", clause[max(0, match.start() - 12):match.start()])
                if any(prefix.endswith(cue) for cue in _NEGATIVE):
                    forbidden.add(alias)
                elif any(prefix.endswith(cue) for cue in _POSITIVE):
                    required.add(alias)
    return required, forbidden


__all__ = ["build_procedure_rule_schema", "procedure_rules_conflict", "resolve_procedure_rule_schema"]
