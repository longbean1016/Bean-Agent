"""工作区 Skill 索引、依赖检查与正文加载测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.skills import SkillRevisionConflict, SkillsLoader, seed_builtin_skills


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

    loader = SkillsLoader(tmp_path, builtin_skills_dir=None)

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


def test_loader_merges_builtin_and_workspace_with_workspace_precedence(
    tmp_path: Path,
) -> None:
    builtin = tmp_path / "builtin"
    workspace = tmp_path / "workspace"
    _write_skill(builtin, "weather", description="内置天气", body="builtin body")
    _write_skill(builtin, "summarize", description="内置总结")
    _write_skill(
        workspace / "skills",
        "weather",
        description="用户天气",
        body="workspace body",
    )

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    records = loader.list_skill_records(filter_unavailable=False)

    assert [(record.name, record.source) for record in records] == [
        ("summarize", "builtin"),
        ("weather", "workspace"),
    ]
    assert loader.load_skill_body("weather") == "workspace body"
    assert 'source="workspace"' in loader.build_skills_summary()


def test_loader_discovers_new_workspace_skill_on_next_query(tmp_path: Path) -> None:
    loader = SkillsLoader(tmp_path, builtin_skills_dir=None)
    assert loader.list_skill_records(filter_unavailable=False) == []

    _write_skill(tmp_path / "skills", "weather")

    assert [record.name for record in loader.list_skill_records()] == ["weather"]


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

    loader = SkillsLoader(tmp_path, builtin_skills_dir=None)
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

    loader = SkillsLoader(tmp_path / "workspace", builtin_skills_dir=None)

    assert loader.list_skill_records(filter_unavailable=False) == []


def test_skill_management_revision_and_enable_state(tmp_path: Path) -> None:
    loader = SkillsLoader(tmp_path, builtin_skills_dir=None, user_skills_dir=tmp_path / "user-skills")
    created = loader.create_skill("managed", "workspace", "---\nname: managed\ndescription: one\n---\nbody")
    assert created.revision == 1
    updated = loader.update_skill("managed", "workspace", "---\nname: managed\ndescription: two\n---\nnew", expected_revision=1)
    assert updated.description == "two"
    assert updated.revision == 2
    with pytest.raises(SkillRevisionConflict):
        loader.update_skill("managed", "workspace", "body", expected_revision=1)
    disabled = loader.set_skill_enabled("managed", "workspace", False)
    assert disabled.enabled is False and disabled.status == "disabled"
    assert loader.list_skill_records(filter_unavailable=True) == []
    loader.set_skill_enabled("managed", "workspace", True)
    loader.delete_skill("managed", "workspace")
    assert loader.get_skill_record("managed", scope="workspace") is None


def test_invalid_skill_is_visible_with_diagnostics(tmp_path: Path) -> None:
    directory = tmp_path / "skills" / "broken"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\nname: [broken\n---\nbody", encoding="utf-8")
    record = SkillsLoader(tmp_path, builtin_skills_dir=None).get_skill_record("broken")
    assert record is not None
    assert record.status == "invalid"
    assert record.available is False
    assert record.diagnostics


def test_skill_diagnostics_expose_machine_readable_codes(tmp_path: Path) -> None:
    directory = tmp_path / "skills" / "needs-diagnostics"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("正文但没有 frontmatter", encoding="utf-8")

    record = SkillsLoader(tmp_path, builtin_skills_dir=None).get_skill_record("needs-diagnostics")

    assert record is not None
    assert "missing_frontmatter" in record.diagnostics
    assert "missing_description" in record.diagnostics
    assert record.status == "invalid"


def test_scope_priority_and_plugin_group(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    plugin = tmp_path / "plugin"
    _write_skill(user, "same", description="user")
    _write_skill(workspace / "skills", "same", description="workspace")
    _write_skill(plugin, "plugin-only", description="plugin")
    loader = SkillsLoader(workspace, builtin_skills_dir=None, user_skills_dir=user, plugin_skill_roots=[("demo", plugin)])
    assert loader.get_skill_record("same", scope="workspace").description == "workspace"
    assert loader.get_skill_record("same", scope="user").description == "user"
    plugin_record = next(record for record in loader.list_skill_records(filter_unavailable=False) if record.plugin_name == "demo")
    assert plugin_record.source == "plugin"


def test_seed_builtin_skills_is_idempotent_and_does_not_overwrite_user_file(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    _write_skill(builtin, "skill-creator", description="默认版本")
    first = seed_builtin_skills(user, builtin_skills_dir=builtin)
    assert first["seeded"] == ["skill-creator"]
    target = user / "skill-creator" / "SKILL.md"
    target.write_text("---\nname: skill-creator\ndescription: 用户修改\n---\nbody", encoding="utf-8")
    second = seed_builtin_skills(user, builtin_skills_dir=builtin)
    assert second["seeded"] == []
    assert "用户修改" in target.read_text(encoding="utf-8")
    manifest = user.parent / "skills-seed-manifest.json"
    assert manifest.is_file()


def test_seed_builtin_skills_upgrades_unmodified_copy_but_preserves_user_edit(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    _write_skill(builtin, "weather", description="版本一", body="正文一")
    seed_builtin_skills(user, builtin_skills_dir=builtin)
    (builtin / "weather" / "SKILL.md").write_text(
        "---\nname: weather\ndescription: 版本二\n---\n正文二\n",
        encoding="utf-8",
    )

    upgraded = seed_builtin_skills(user, builtin_skills_dir=builtin)
    assert "weather" in upgraded["seeded"]
    assert "正文二" in (user / "weather" / "SKILL.md").read_text(encoding="utf-8")

    (user / "weather" / "SKILL.md").write_text(
        "---\nname: weather\ndescription: 用户版本\n---\n用户正文\n",
        encoding="utf-8",
    )
    (builtin / "weather" / "SKILL.md").write_text(
        "---\nname: weather\ndescription: 版本三\n---\n正文三\n",
        encoding="utf-8",
    )
    preserved = seed_builtin_skills(user, builtin_skills_dir=builtin)
    assert "weather" not in preserved["seeded"]
    assert "用户正文" in (user / "weather" / "SKILL.md").read_text(encoding="utf-8")
    assert preserved["manifest"]["weather"]["managed"] is False


def test_loader_can_switch_registered_project_and_create_immutable_snapshot(tmp_path: Path) -> None:
    user = tmp_path / "user"
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_skill(user, "shared", description="用户版本", body="用户正文")
    _write_skill(first / "skills", "project", description="项目一", body="项目一正文")
    _write_skill(second / "skills", "project", description="项目二", body="项目二正文")
    loader = SkillsLoader(first, builtin_skills_dir=None, user_skills_dir=user)

    assert loader.for_workspace(second).load_skill_body("project") == "项目二正文"
    snapshot = loader.create_snapshot()
    revision = loader.directory_revision()
    (first / "skills" / "project" / "SKILL.md").write_text(
        "---\nname: project\ndescription: 已修改\n---\n新正文\n",
        encoding="utf-8",
    )
    frozen = SkillsLoader.from_snapshot(snapshot)
    assert frozen.load_skill_body("project") == "项目一正文"
    assert snapshot["revision"] == revision
