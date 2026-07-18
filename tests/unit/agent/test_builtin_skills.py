"""仓库内置 Skill 的名称、依赖和关键执行边界。"""

from __future__ import annotations

from pathlib import Path

from agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader


def test_builtin_skill_catalog_contains_expected_three_skills(tmp_path: Path) -> None:
    loader = SkillsLoader(tmp_path)
    records = loader.list_skill_records(filter_unavailable=False)

    assert [(record.name, record.source, record.always) for record in records] == [
        ("skill-creator", "builtin", False),
        ("summarize", "builtin", False),
        ("weather", "builtin", False),
    ]


def test_builtin_skills_keep_akashic_behavior_boundaries() -> None:
    bodies = {
        name: (BUILTIN_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        for name in ("skill-creator", "summarize", "weather")
    }

    assert "workspace/skills" in bodies["skill-creator"]
    assert "下一轮" in bodies["skill-creator"]
    assert 'bins: ["summarize"]' in bodies["summarize"]
    assert "--extract-only" in bodies["summarize"]
    assert 'bins: ["curl"]' in bodies["weather"]
    assert "wttr.in" in bodies["weather"]
    assert "Open-Meteo" in bodies["weather"]
