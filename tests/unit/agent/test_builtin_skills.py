"""仓库内置 Skill 的名称、依赖和关键执行边界。"""

from __future__ import annotations

from pathlib import Path
import re

from agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader
from tools.shell import _validate_command


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
    assert "web_fetch" in bodies["weather"]
    assert "bins:" not in bodies["weather"]
    assert "wttr.in" in bodies["weather"]
    assert "Open-Meteo" in bodies["weather"]


def test_weather_commands_match_network_guard_and_keep_errors() -> None:
    body = (BUILTIN_SKILLS_DIR / "weather" / "SKILL.md").read_text(encoding="utf-8")
    examples = re.findall(r"^curl .+$", body, re.MULTILINE)
    assert examples
    for example in examples:
        assert "-sS" in example and "--max-time" in example
        assert _validate_command(example, allow_network=True, restricted_dir=None) is None
        assert _validate_command(example.replace("curl ", "curl.exe ", 1), allow_network=True, restricted_dir=None) is None
