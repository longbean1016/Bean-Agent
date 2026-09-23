"""Skills 生命周期窄职责服务测试。"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent.skill_services import (
    SkillDependencyChecker,
    SkillDiscovery,
    SkillInstaller,
    SkillParser,
    SkillRuntimeIndex,
    SkillSessionSnapshot,
)
from agent.skills import SkillRecord


def test_discovery_normalizes_configured_and_manifest_plugins(tmp_path: Path) -> None:
    configured = tmp_path / "configured-skills"
    plugin = tmp_path / "plugins" / "example"
    configured.mkdir()
    (plugin / "skills").mkdir(parents=True)
    (plugin / "plugin.json").write_text(
        json.dumps({
            "name": "示例插件",
            "icon": "icons/plugin.svg",
            "source": "本地安装",
            "enabled": False,
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    sources = SkillDiscovery.plugin_sources(
        tmp_path,
        [("configured", configured)],
    )

    assert [(item.source_id, item.name) for item in sources] == [
        ("configured", "configured"),
        ("example", "示例插件"),
    ]
    assert sources[1].icon == "icons/plugin.svg"
    assert sources[1].install_source == "本地安装"
    assert sources[1].enabled is False


def test_parser_returns_content_metadata_and_diagnostics(tmp_path: Path) -> None:
    valid = tmp_path / "SKILL.md"
    valid.write_text(
        "---\nname: demo\ndescription: 测试\n---\n正文\n",
        encoding="utf-8",
    )

    parsed = SkillParser().parse_file(valid)

    assert parsed.metadata["name"] == "demo"
    assert SkillParser.strip_frontmatter(parsed.content) == "正文"
    assert parsed.diagnostics == ()

    valid.write_text("没有 frontmatter", encoding="utf-8")
    assert SkillParser().parse_file(valid).diagnostics == ("missing_frontmatter",)


def test_dependency_checker_only_reports_missing_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BEANAGENT_MISSING_TOKEN", raising=False)
    monkeypatch.setattr(
        "agent.skill_services.shutil.which",
        lambda name: None if name == "missing-cli" else f"/bin/{name}",
    )
    config = SkillDependencyChecker.skill_config({
        "skill": {
            "requires": {
                "bins": ["available-cli", "missing-cli"],
                "env": ["BEANAGENT_MISSING_TOKEN"],
            },
        },
    })

    assert SkillDependencyChecker.missing_requirements(config) == (
        "CLI: missing-cli, ENV: BEANAGENT_MISSING_TOKEN"
    )


def test_installer_places_copy_atomically(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target" / "demo"
    source.mkdir()
    (source / "SKILL.md").write_text("正文", encoding="utf-8")

    SkillInstaller.place(
        source,
        target,
        "copy",
        lambda: pytest.fail("复制安装不应登记软链接"),
    )

    assert (target / "SKILL.md").read_text(encoding="utf-8") == "正文"
    assert not list(target.parent.glob(".demo.install-*.tmp"))


def test_installer_rolls_back_failed_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target" / "demo"
    source.mkdir()
    (source / "SKILL.md").write_text("正文", encoding="utf-8")

    def fail_after_creating_temporary(_source: Path, temporary: Path, **_kwargs) -> None:
        temporary.mkdir(parents=True)
        (temporary / "partial").write_text("partial", encoding="utf-8")
        raise shutil.Error("copy failed")

    monkeypatch.setattr("agent.skill_services.shutil.copytree", fail_after_creating_temporary)

    with pytest.raises(shutil.Error):
        SkillInstaller.place(source, target, "copy", lambda: None)

    assert not target.exists()
    assert not list(target.parent.glob(".demo.install-*.tmp"))


@dataclass(frozen=True)
class _IndexRecord:
    name: str
    source: str
    source_id: str
    priority: int
    active: bool = True
    overridden_by: str | None = None
    override_reason: str | None = None


def test_runtime_index_selects_priority_marks_overrides_and_falls_back() -> None:
    index = SkillRuntimeIndex()
    user = _IndexRecord("demo", "user", "user", 3)
    project = _IndexRecord("demo", "workspace", "workspace", 4)

    assert index.select_effective([user, project]) == [project]
    marked = index.mark_overrides([user, project])
    assert [(item.source, item.active) for item in marked] == [
        ("workspace", True),
        ("user", False),
    ]
    assert marked[1].overridden_by == "workspace:demo"

    assert index.build("demo", lambda: [project]) == [project]

    def failed_scan() -> list[_IndexRecord]:
        raise OSError("scan failed")

    assert index.build("demo", failed_scan) == [project]
    assert index.diagnostics == ("scan_failed:OSError",)


def test_snapshot_service_creates_and_restores_legacy_fields(tmp_path: Path) -> None:
    content = "---\nname: demo\n---\n正文"
    record = SkillRecord(
        name="demo",
        source="user",
        source_id="user",
        root_dir=tmp_path / "demo",
        skill_file=tmp_path / "demo" / "SKILL.md",
        content=content,
        description="测试",
        when_to_use="",
        always=False,
        available=True,
        missing="",
        scope="user",
        priority=3,
    )

    snapshot = SkillSessionSnapshot.create([record], "revision-2")
    assert snapshot["schema"] == 2
    assert snapshot["skills"][0]["priority"] == 3
    assert snapshot["skills"][0]["content_hash"] == hashlib.sha256(
        content.encode("utf-8"),
    ).hexdigest()

    revision, restored = SkillSessionSnapshot.restore(
        {"schema": 1, "revision": "revision-1", "skills": [{
            "name": "demo",
            "source": "user",
            "content": content,
        }]},
        {"user": 3},
    )
    assert revision == "revision-1"
    assert restored[0]["priority"] == 3
    assert restored[0]["content_hash"] == hashlib.sha256(
        content.encode("utf-8"),
    ).hexdigest()
