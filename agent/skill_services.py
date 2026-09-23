"""Skills 生命周期的窄职责服务。

这些服务不依赖 Web、会话或模型层；`SkillsLoader` 只负责组合它们并保留旧调用
接口。跨服务仅传路径、不可变解析结果或普通记录列表，避免共享可变状态。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

import yaml

DEFAULT_SOURCE_PRIORITIES = {"workspace": 4, "user": 3, "plugin": 2, "builtin": 1}


@dataclass(frozen=True, slots=True)
class PluginSkillSource:
    source_id: str
    name: str
    skills_root: Path
    icon: str | None
    install_source: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class ParsedSkillDocument:
    content: str
    metadata: dict[str, Any]
    diagnostics: tuple[str, ...]
    file_size: int


class SkillDiscovery:
    """只发现并规范化插件和外部目录，不解析 SKILL.md。"""

    @staticmethod
    def plugin_sources(
        workspace: Path,
        configured_roots: Iterable[tuple[str, Path]],
    ) -> list[PluginSkillSource]:
        sources = [
            PluginSkillSource(name, name, root, None, "已配置插件", True)
            for name, root in configured_roots
        ]
        workspace_plugins = workspace / "plugins"
        if not workspace_plugins.is_dir():
            return sources
        for directory in sorted(workspace_plugins.iterdir(), key=lambda item: item.name):
            if not directory.is_dir():
                continue
            manifest = SkillDiscovery.read_json(directory / "plugin.json")
            sources.append(PluginSkillSource(
                source_id=directory.name,
                name=str(manifest.get("name") or directory.name),
                skills_root=directory / "skills",
                icon=str(manifest.get("icon") or "") or None,
                install_source=str(manifest.get("source") or "当前项目插件"),
                enabled=bool(manifest.get("enabled", True)),
            ))
        return sources

    @staticmethod
    def read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}


class SkillParser:
    """只负责 UTF-8 内容、frontmatter 和字段级基础诊断。"""

    max_file_size = 1024 * 1024

    def parse_file(self, skill_file: Path) -> ParsedSkillDocument:
        file_size = skill_file.stat().st_size
        if file_size > self.max_file_size:
            return ParsedSkillDocument("", {}, ("file_too_large",), file_size)
        try:
            content = skill_file.read_text(encoding="utf-8")
            metadata = self.parse_frontmatter(content)
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            return ParsedSkillDocument("", {}, ("frontmatter_invalid", str(error)), file_size)
        diagnostics = () if content.startswith("---") else ("missing_frontmatter",)
        return ParsedSkillDocument(content, metadata, diagnostics, file_size)

    @staticmethod
    def parse_frontmatter(content: str) -> dict[str, Any]:
        if not content.startswith("---"):
            return {}
        match = re.match(r"^---\s*\r?\n(.*?)\r?\n---(?:\s*\r?\n|$)", content, re.DOTALL)
        if not match:
            raise yaml.YAMLError("frontmatter 未闭合")
        loaded = yaml.safe_load(match.group(1)) or {}
        if not isinstance(loaded, dict):
            raise yaml.YAMLError("frontmatter 必须是对象")
        return {str(key): value for key, value in loaded.items()}

    @staticmethod
    def strip_frontmatter(content: str) -> str:
        if not content.startswith("---"):
            return content.strip()
        match = re.match(r"^---\s*\r?\n.*?\r?\n---(?:\s*\r?\n|$)(.*)$", content, re.DOTALL)
        return (match.group(1) if match else content).strip()


class SkillDependencyChecker:
    """只读取进程环境和 CLI 可见性，不执行 Skill 中的任何命令。"""

    @staticmethod
    def skill_config(raw: object) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        for key in ("skill", "beanagent"):
            value = raw.get(key)
            if isinstance(value, dict):
                return {str(item_key): item for item_key, item in value.items()}
        return {str(key): value for key, value in raw.items()}

    @staticmethod
    def missing_requirements(config: dict[str, Any]) -> str:
        requires = config.get("requires")
        if not isinstance(requires, dict):
            return ""
        missing: list[str] = []
        bins = requires.get("bins")
        if isinstance(bins, list):
            missing.extend(
                f"CLI: {name}"
                for item in bins
                if (name := str(item).strip()) and not shutil.which(name)
            )
        env_names = requires.get("env")
        if isinstance(env_names, list):
            missing.extend(
                f"ENV: {name}"
                for item in env_names
                if (name := str(item).strip()) and not os.environ.get(name)
            )
        return ", ".join(missing)


class SkillInstaller:
    """负责来源校验、原子复制、软链接落盘和 Git 暂存。"""

    git_url = re.compile(r"^(https://|ssh://|git@)[^\s]+$")

    @staticmethod
    def candidates(source: Path) -> list[Path]:
        if (source / "SKILL.md").is_file():
            return [source]
        return [
            item for item in sorted(source.iterdir(), key=lambda path: path.name)
            if item.is_dir() and (item / "SKILL.md").is_file()
        ]

    @staticmethod
    def validate_source(candidate: Path) -> None:
        if candidate.is_symlink() or (candidate / "SKILL.md").is_symlink():
            raise ValueError("Skill 来源不能通过符号链接安装")
        if any(item.is_symlink() for item in candidate.rglob("*")):
            raise ValueError("Skill 来源包含未校验的符号链接")

    @staticmethod
    def place(
        candidate: Path,
        target: Path,
        mode: str,
        register_link: Callable[[], None],
    ) -> None:
        if mode == "symlink":
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.symlink_to(candidate, target_is_directory=True)
                register_link()
            except (OSError, ValueError):
                if target.is_symlink():
                    target.unlink()
                raise
            return
        temporary = target.parent / f".{target.name}.install-{uuid.uuid4().hex}.tmp"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(candidate, temporary, symlinks=False)
            temporary.replace(target)
        except (OSError, shutil.Error):
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def install_git(
        self,
        url: str,
        revision: str | None,
        install: Callable[[Path], list[Any]],
    ) -> list[Any]:
        if not self.git_url.match(url):
            raise ValueError("Git 来源必须使用 HTTPS、SSH 或 git@ 地址")
        with tempfile.TemporaryDirectory(prefix="beanagent-skill-") as directory:
            command = ["git", "clone"]
            if not revision:
                command.extend(["--depth", "1"])
            command.extend([url, directory])
            completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
            if completed.returncode != 0:
                raise RuntimeError("Git 仓库获取失败")
            if revision:
                checkout = subprocess.run(["git", "-C", directory, "checkout", revision], capture_output=True, text=True, timeout=60, check=False)
                if checkout.returncode != 0:
                    raise RuntimeError("Git revision 不存在")
            return install(Path(directory))


RecordT = TypeVar("RecordT")


class SkillRuntimeIndex:
    """负责优先级覆盖链、revision 和读取失败时的索引回退。"""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[Any, ...]] = {}
        self.diagnostics: tuple[str, ...] = ()

    def build(self, key: str, builder: Callable[[], list[RecordT]]) -> list[RecordT]:
        try:
            records = builder()
        except OSError as error:
            self.diagnostics = (f"scan_failed:{type(error).__name__}",)
            return list(self._cache.get(key, ()))
        self.diagnostics = ()
        self._cache[key] = tuple(records)
        return records

    def mark_overrides(self, records: Iterable[RecordT]) -> list[RecordT]:
        ordered = sorted(
            enumerate(records),
            key=lambda item: (-item[1].priority, item[0]),
        )
        winners: dict[str, Any] = {}
        result: list[RecordT] = []
        for _, record in ordered:
            winner = winners.get(record.name)
            if winner is None:
                winners[record.name] = record
                result.append(replace(record, active=True))
            else:
                result.append(replace(
                    record,
                    active=False,
                    overridden_by=f"{winner.source_id}:{winner.name}",
                    override_reason=f"被更高优先级来源 {winner.source} 覆盖",
                ))
        return sorted(result, key=lambda item: (item.name, -item.priority, item.source_id))

    @staticmethod
    def select_effective(records: Iterable[RecordT]) -> list[RecordT]:
        """按显式优先级选择同名生效项，同级来源保持发现顺序。"""

        winners: dict[str, RecordT] = {}
        for record in records:
            current = winners.get(record.name)
            if current is None or record.priority > current.priority:
                winners[record.name] = record
        return sorted(winners.values(), key=lambda item: item.name)

    @staticmethod
    def revision(records: Iterable[Any]) -> str:
        digest = hashlib.sha256()
        for record in records:
            digest.update(record.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(record.source.encode("utf-8"))
            digest.update(b"\0")
            digest.update(bytes.fromhex(record.content_hash or hashlib.sha256(record.content.encode("utf-8")).hexdigest()))
            digest.update(str(record.enabled).encode("ascii"))
            digest.update(b"\0")
            digest.update("|".join(record.diagnostics).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()


class SkillSessionSnapshot:
    """只负责把当前不可变记录序列编码为可持久化快照。"""

    @staticmethod
    def create(records: Iterable[Any], revision: str) -> dict[str, Any]:
        return {
            "schema": 2,
            "revision": revision,
            "skills": [{
                "name": record.name,
                "source": record.source,
                "source_id": record.source_id,
                "scope": "project" if record.scope == "workspace" and record.source == "workspace" else record.scope,
                "root_dir": str(record.root_dir),
                "skill_file": str(record.skill_file),
                "content": record.content,
                "description": record.description,
                "when_to_use": record.when_to_use,
                "always": record.always,
                "available": record.available,
                "missing": record.missing,
                "version": record.version,
                "plugin_name": record.plugin_name,
                "status": record.status,
                "diagnostics": list(record.diagnostics),
                "record_revision": record.revision,
                "enabled": record.enabled,
                "slug": record.slug,
                "owner": record.owner,
                "published_at": record.published_at,
                "file_size": record.file_size,
                "content_hash": record.content_hash or hashlib.sha256(record.content.encode("utf-8")).hexdigest(),
                "priority": record.priority or DEFAULT_SOURCE_PRIORITIES.get(record.source, 0),
                "active": record.active,
                "overridden_by": record.overridden_by,
                "override_reason": record.override_reason,
                "plugin_icon": record.plugin_icon,
                "plugin_source": record.plugin_source,
                "plugin_enabled": record.plugin_enabled,
            } for record in records],
        }

    @staticmethod
    def restore(
        snapshot: dict[str, Any],
        source_priorities: dict[str, int] | None = None,
    ) -> tuple[str, tuple[dict[str, Any], ...]]:
        """规范化持久化快照，并为旧 schema 补齐哈希和优先级字段。"""

        priorities = source_priorities or DEFAULT_SOURCE_PRIORITIES
        raw_records = snapshot.get("skills")
        if not isinstance(raw_records, list):
            raw_records = []
        restored: list[dict[str, Any]] = []
        for raw in raw_records:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            item = dict(raw)
            content = str(item.get("content") or "")
            source = str(item.get("source") or "unknown")
            item["name"] = name
            item["content_hash"] = str(
                item.get("content_hash")
                or hashlib.sha256(content.encode("utf-8")).hexdigest()
            )
            item["priority"] = int(
                item.get("priority", priorities.get(source, 0)) or 0
            )
            restored.append(item)
        return str(snapshot.get("revision") or ""), tuple(restored)


__all__ = [
    "ParsedSkillDocument",
    "PluginSkillSource",
    "SkillDependencyChecker",
    "SkillDiscovery",
    "SkillInstaller",
    "SkillParser",
    "SkillRuntimeIndex",
    "SkillSessionSnapshot",
]
