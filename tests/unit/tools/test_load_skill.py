"""按名称读取完整 SKILL.md 的工具测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.skills import SkillsLoader
from tools.load_skill import LoadSkillTool
from tools.registration import register_all
from tools.registry import ToolRegistry


def _write_skill(root: Path, name: str, body: str = "执行正文") -> Path:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 描述\n---\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.mark.asyncio
async def test_load_skill_returns_body_and_base_directory(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "memory", "先读取记忆，再回答。")
    tool = LoadSkillTool(SkillsLoader(tmp_path))

    result = await tool.execute(skill="memory")

    assert "# Skill: memory" in result
    assert f"Base directory: {skill_dir.resolve()}" in result
    assert "先读取记忆，再回答。" in result
    assert "description:" not in result


@pytest.mark.asyncio
async def test_load_skill_returns_stable_unknown_error(tmp_path: Path) -> None:
    _write_skill(tmp_path, "memory")
    tool = LoadSkillTool(SkillsLoader(tmp_path))

    result = await tool.execute(skill="missing")

    assert result == "错误：未找到 Skill：missing。\n已发现 Skill：memory"


def test_register_all_adds_load_skill_only_when_loader_is_available(
    tmp_path: Path,
) -> None:
    without_skills = register_all(
        ToolRegistry(),
        allowed_dir=tmp_path,
        multimodal=False,
        allow_shell_network=False,
    )
    with_skills = register_all(
        ToolRegistry(),
        allowed_dir=tmp_path,
        multimodal=False,
        allow_shell_network=False,
        skills=SkillsLoader(tmp_path),
    )

    assert "load_skill" not in without_skills.get_registered_names()
    assert "load_skill" in with_skills.get_registered_names()
