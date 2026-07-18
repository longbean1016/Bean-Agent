"""工作区 Skill 索引、依赖检查与正文加载测试。"""

from __future__ import annotations

from pathlib import Path

from agent.skills import SkillsLoader


def _write_skill(
    root: Path,
    directory: str,
    *,
    name: str | None = None,
    description: str = "测试技能",
    body: str = "执行测试步骤。",
    extra_frontmatter: str = "",
) -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True)
    extra = f"{extra_frontmatter}\n" if extra_frontmatter else ""
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name or directory}\n"
        f"description: {description}\n"
        f"{extra}"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_loader_builds_stably_sorted_catalog_and_strips_frontmatter(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "z-last", description="最后一个", body="最后正文")
    _write_skill(
        skills_dir,
        "a-first",
        description="第一个",
        body="第一正文",
        extra_frontmatter="when_to_use: 用户明确要求第一个技能\nalways: true",
    )

    loader = SkillsLoader(tmp_path)

    assert [record.name for record in loader.list_skill_records()] == [
        "a-first",
        "z-last",
    ]
    assert loader.get_always_skills() == ["a-first"]
    assert loader.load_skill_body("a-first") == "第一正文"
    summary = loader.build_skills_summary()
    assert summary.index('name="a-first"') < summary.index('name="z-last"')
    assert "第一个" in summary
    assert "用户明确要求第一个技能" in summary
    assert "第一正文" not in summary
    assert "SKILL.md" not in summary


def test_loader_marks_missing_dependencies_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("BEANAGENT_SKILL_TOKEN", raising=False)
    _write_skill(
        tmp_path / "skills",
        "needs-env",
        extra_frontmatter=(
            "metadata:\n"
            "  skill:\n"
            "    requires:\n"
            "      env: [BEANAGENT_SKILL_TOKEN]"
        ),
    )

    loader = SkillsLoader(tmp_path)
    record = loader.load_skill_record("needs-env")

    assert record is not None
    assert record.available is False
    assert record.missing == "ENV: BEANAGENT_SKILL_TOKEN"
    assert loader.list_skill_records(filter_unavailable=True) == []


def test_loader_ignores_symlinked_skill_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    target = _write_skill(outside, "external")
    skills_dir = tmp_path / "workspace" / "skills"
    skills_dir.mkdir(parents=True)
    link = skills_dir / "external"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        # Windows 未启用开发者模式时不能创建符号链接，边界由生产代码测试覆盖。
        return

    loader = SkillsLoader(tmp_path / "workspace")

    assert loader.list_skill_records(filter_unavailable=False) == []
