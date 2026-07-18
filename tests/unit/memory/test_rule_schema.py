"""Procedure 规则 schema 的构建和冲突判断基线。"""

from __future__ import annotations

from memory.rule_schema import build_procedure_rule_schema, procedure_rules_conflict


def test_schema_infers_required_and_forbidden_tools() -> None:
    schema = build_procedure_rule_schema(
        "处理网页时必须先使用 web_fetch，不要使用 shell",
        steps=["优先用 web_fetch 读取正文"],
    )

    assert schema["required_tools"] == ["web_fetch"]
    assert schema["forbidden_tools"] == ["shell"]
    assert set(schema["mentioned_tools"]) == {"shell", "web_fetch"}


def test_rule_conflict_requires_shared_tool_with_opposite_direction() -> None:
    required = build_procedure_rule_schema("必须使用 web_fetch")
    forbidden = build_procedure_rule_schema("不要使用 web_fetch")
    unrelated = build_procedure_rule_schema("必须使用 shell")

    assert procedure_rules_conflict(required, forbidden) is True
    assert procedure_rules_conflict(required, unrelated) is False
