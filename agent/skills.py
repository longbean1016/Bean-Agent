"""工作区 Skill 的确定性索引、依赖检查与正文加载。

Skill 目录摘要会进入稳定 Prompt 前缀，因此扫描和输出顺序必须完全确定；
完整正文只在本轮明确命中或模型调用加载工具时读取，避免所有 SKILL.md
高频进入主上下文。当前实现仅接受 workspace/skills 下的真实目录，不跟随符号
链接，以免 Skill 借助路径跳转读取工作区外内容。
"""

from __future__ import annotations

import logging
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"
_SKILL_NAME = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff.-]{0,63}$")


class SkillRevisionConflict(RuntimeError):
    """Skill 编辑基于旧摘要时的并发冲突。"""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"Skill 配置已更新（期望 revision={expected}，当前 revision={actual}）")
        self.expected = expected
        self.actual = actual


def seed_builtin_skills(
    user_skills_dir: str | Path,
    *,
    builtin_skills_dir: str | Path | None = BUILTIN_SKILLS_DIR,
) -> dict[str, Any]:
    """首启幂等复制内置 Skill；已有用户目录永远不被覆盖。"""

    target_root = Path(user_skills_dir).expanduser().resolve()
    source_root = (
        Path(builtin_skills_dir).expanduser().resolve()
        if builtin_skills_dir is not None
        else None
    )
    if source_root is None or not source_root.is_dir():
        return {"seeded": [], "skipped": [], "manifest": {}}
    target_root.mkdir(parents=True, exist_ok=True)
    manifest_path = target_root.parent / "skills-seed-manifest.json"
    seeded: list[str] = []
    skipped: list[str] = []
    manifest: dict[str, Any] = {}
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        if isinstance(raw_manifest, dict):
            manifest = raw_manifest
    except (OSError, UnicodeError, json.JSONDecodeError):
        manifest = {}
    for source in sorted(source_root.iterdir(), key=lambda item: item.name):
        if not source.is_dir() or not (source / "SKILL.md").is_file():
            continue
        name = source.name
        target = target_root / name
        source_hash = _hash_skill_tree(source)
        if target.exists():
            skipped.append(name)
            previous = manifest.get(name) if isinstance(manifest.get(name), dict) else {}
            previous_seed_hash = str(previous.get("seed_hash") or "")
            target_hash = _hash_skill_tree(target) if target.is_dir() else ""
            # 只有目标仍等于上次种子版本时才允许升级；用户改过的副本永远
            # 保留原内容，manifest 仅记录可供页面提示的最新内置哈希。
            if (
                previous_seed_hash
                and previous_seed_hash == target_hash
                and source_hash != target_hash
                and bool(previous.get("managed", False))
            ):
                temporary = target_root / f".{name}.upgrade-{uuid.uuid4().hex}.tmp"
                backup = target_root / f".{name}.upgrade-{uuid.uuid4().hex}.old"
                try:
                    shutil.copytree(source, temporary, symlinks=False)
                    target.rename(backup)
                    temporary.replace(target)
                    shutil.rmtree(backup, ignore_errors=True)
                    seeded.append(name)
                    target_hash = source_hash
                except (OSError, shutil.Error):
                    shutil.rmtree(temporary, ignore_errors=True)
                    if not target.exists() and backup.exists():
                        backup.rename(target)
                    logger.warning("内置 Skill 升级失败: name=%s", name)
            manifest[name] = {
                "source": "builtin",
                "seed_hash": str(source_hash if target_hash == source_hash else (previous_seed_hash or target_hash)),
                "current_builtin_hash": source_hash,
                "managed": bool(previous.get("managed", False)) and target_hash == source_hash,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            continue
        temporary = target_root / f".{name}.seed-{uuid.uuid4().hex}.tmp"
        try:
            shutil.copytree(source, temporary, symlinks=False)
            temporary.replace(target)
        except (OSError, shutil.Error):
            shutil.rmtree(temporary, ignore_errors=True)
            logger.warning("内置 Skill 首次复制失败: name=%s", name)
            continue
        seeded.append(name)
        manifest[name] = {
            "source": "builtin",
            "seed_hash": source_hash,
            "current_builtin_hash": source_hash,
            "managed": True,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    if manifest:
        _atomic_write_path(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"seeded": seeded, "skipped": skipped, "manifest": manifest}


def _hash_skill_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _atomic_write_path(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def collect_skill_mentions(content: str, available_names: list[str]) -> list[str]:
    """按出现顺序收集合法 ``$skill-name``，未知名称不会进入 Prompt。"""

    available = set(available_names)
    seen: set[str] = set()
    result: list[str] = []
    for name in re.findall(r"\$([a-zA-Z0-9_:-]+)", content):
        if name in available and name not in seen:
            seen.add(name)
            result.append(name)
    return result


@dataclass(frozen=True, slots=True)
class SkillRecord:
    """一份 SKILL.md 的只读索引记录。"""

    name: str
    source: str
    source_id: str
    root_dir: Path
    skill_file: Path
    content: str
    description: str
    when_to_use: str
    always: bool
    available: bool
    missing: str
    scope: str = "workspace"
    version: str | None = None
    plugin_name: str | None = None
    status: str = "available"
    diagnostics: tuple[str, ...] = ()
    revision: int = 1
    enabled: bool = True


class SkillSnapshotView:
    """基于会话快照的只读 Skill 视图。

    快照保存正文而不是只保存文件路径，确保磁盘上的文件被修改或删除后，
    已有会话仍然能够重放创建快照时的内容。视图只实现 Prompt 所需的窄接口，
    不把管理操作和运行时上下文耦合在一起。
    """

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.revision = str(snapshot.get("revision") or "")
        records: list[SkillRecord] = []
        raw_records = snapshot.get("skills")
        if not isinstance(raw_records, list):
            raw_records = []
        for raw in raw_records:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            content = str(raw.get("content") or "")
            if not name:
                continue
            records.append(
                SkillRecord(
                    name=name,
                    source=str(raw.get("source") or "unknown"),
                    source_id=str(raw.get("source_id") or ""),
                    root_dir=Path(str(raw.get("root_dir") or "")),
                    skill_file=Path(str(raw.get("skill_file") or "")),
                    content=content,
                    description=str(raw.get("description") or name),
                    when_to_use=str(raw.get("when_to_use") or ""),
                    always=bool(raw.get("always")),
                    available=bool(raw.get("available", False)),
                    missing=str(raw.get("missing") or ""),
                    scope=str(raw.get("scope") or "project"),
                    version=str(raw.get("version") or "") or None,
                    plugin_name=str(raw.get("plugin_name") or "") or None,
                    status=str(raw.get("status") or "unknown"),
                    diagnostics=tuple(str(item) for item in raw.get("diagnostics", []) if str(item)),
                    revision=int(raw.get("record_revision", 1) or 1),
                    enabled=bool(raw.get("enabled", True)),
                )
            )
        self._records = tuple(sorted(records, key=lambda item: item.name))

    def list_skill_records(self, *, filter_unavailable: bool = True, scope: str | None = None) -> list[SkillRecord]:
        records = list(self._records)
        if scope in {"user", "workspace", "project"}:
            wanted_scope = "project" if scope == "workspace" else scope
            records = [record for record in records if record.scope == wanted_scope]
        if filter_unavailable:
            records = [record for record in records if record.available and record.enabled]
        return records

    def load_skill_record(self, name: str) -> SkillRecord | None:
        return next((record for record in self._records if record.name == str(name).strip()), None)

    def load_skill_body(self, name: str) -> str | None:
        record = self.load_skill_record(name)
        if record is None or not record.available or not record.enabled:
            return None
        return SkillsLoader._strip_frontmatter(record.content)

    def get_always_skills(self) -> list[str]:
        return [record.name for record in self.list_skill_records() if record.always]

    def load_skills_for_context(self, names: list[str]) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            body = self.load_skill_body(name)
            if body:
                parts.append(f"### Skill: {name}\n\n{body}")
        return "\n\n---\n\n".join(parts)

    def build_skills_summary(self) -> str:
        records = self.list_skill_records(filter_unavailable=False)
        if not records:
            return ""
        lines = ["<skills>"]
        for record in records:
            lines.append(
                f'  <skill name="{SkillsLoader._escape_xml(record.name)}" '
                f'available="{str(record.available).lower()}" '
                f'source="{SkillsLoader._escape_xml(record.source)}">'
            )
            lines.append(f"    <description>{SkillsLoader._escape_xml(record.description)}</description>")
            if record.when_to_use:
                lines.append(f"    <when_to_use>{SkillsLoader._escape_xml(record.when_to_use)}</when_to_use>")
            if not record.available and record.missing:
                lines.append(f"    <requires>{SkillsLoader._escape_xml(record.missing)}</requires>")
            lines.append("  </skill>")
        lines.append("</skills>")
        return "\n".join(lines)


class SkillsLoader:
    """扫描并读取工作区 ``skills/*/SKILL.md``。

    每次公开查询都重新构建轻量索引，使用户修改 Skill 后下一轮立即生效；稳定
    目录块使用摘要本身作为缓存签名，内容未变化时仍能复用本地渲染和供应商前缀。
    """

    def __init__(
        self,
        workspace: str | Path,
        builtin_skills_dir: str | Path | None = BUILTIN_SKILLS_DIR,
        user_skills_dir: str | Path | None = None,
        plugin_skill_roots: list[tuple[str, str | Path]] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.skills_dir = self.workspace / "skills"
        self.builtin_skills_dir = (
            Path(builtin_skills_dir).expanduser().resolve()
            if builtin_skills_dir is not None
            else None
        )
        self.user_skills_dir = (
            Path(user_skills_dir).expanduser().resolve()
            if user_skills_dir is not None
            else None
        )
        self.plugin_skill_roots = [
            (str(name), Path(path).expanduser().resolve())
            for name, path in (plugin_skill_roots or [])
        ]
        self._state_paths = [self.workspace / ".beanagent" / "skills-state.json"]
        if self.user_skills_dir is not None:
            self._state_paths.append(self.user_skills_dir.parent / "skills-state.json")

    def list_skill_records(
        self,
        *,
        filter_unavailable: bool = True,
        scope: str | None = None,
    ) -> list[SkillRecord]:
        """按 Skill 名称返回稳定排序的索引。"""

        records = self._build_scope_index(scope) if scope in {"user", "workspace"} else self._build_index()
        if filter_unavailable:
            return [record for record in records if record.available and record.enabled]
        return records

    def for_workspace(self, workspace_path: str | Path) -> "SkillsLoader":
        """为已注册项目创建独立索引，复用全局来源配置但不共享项目路径状态。"""

        return SkillsLoader(
            Path(workspace_path).expanduser().resolve(),
            builtin_skills_dir=self.builtin_skills_dir,
            user_skills_dir=self.user_skills_dir,
            plugin_skill_roots=self.plugin_skill_roots,
        )

    def directory_revision(self) -> str:
        """返回规范化目录 revision，文件遍历顺序不会造成无意义变化。"""

        digest = hashlib.sha256()
        for record in self.list_skill_records(filter_unavailable=False):
            digest.update(record.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(record.source.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(record.content.encode("utf-8")).digest())
            digest.update(str(record.enabled).encode("ascii"))
            digest.update(b"\0")
            digest.update("|".join(record.diagnostics).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    def create_snapshot(self) -> dict[str, Any]:
        """创建可持久化会话快照；正文随快照保存以隔离后续磁盘变化。"""

        records = self.list_skill_records(filter_unavailable=False)
        return {
            "schema": 1,
            "revision": self.directory_revision(),
            "skills": [
                {
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
                }
                for record in records
            ],
        }

    @staticmethod
    def from_snapshot(snapshot: dict[str, Any]) -> SkillSnapshotView:
        return SkillSnapshotView(snapshot)

    def get_skill_record(self, name: str, *, scope: str = "workspace") -> SkillRecord | None:
        return next(
            (record for record in self.list_skill_records(filter_unavailable=False, scope=scope) if record.name == name),
            None,
        )

    def create_skill(self, name: str, scope: str, content: str) -> SkillRecord:
        normalized = self._validate_name(name)
        target = self._scope_root(scope) / normalized
        if target.exists():
            raise ValueError(f"Skill 已存在: {normalized}")
        self._validate_content(content)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target / "SKILL.md", content)
        record = self.get_skill_record(normalized, scope=scope)
        if record is None:
            raise RuntimeError("Skill 写入后无法解析")
        return record

    def update_skill(self, name: str, scope: str, content: str, *, expected_revision: int | None = None) -> SkillRecord:
        record = self.get_skill_record(name, scope=scope)
        if record is None:
            raise KeyError(f"Skill 不存在: {name}")
        if expected_revision is not None and expected_revision != record.revision:
            raise SkillRevisionConflict(expected_revision, record.revision)
        self._validate_content(content)
        self._atomic_write(record.skill_file, content)
        self._bump_revision(name, scope, enabled=record.enabled)
        updated = self.get_skill_record(name, scope=scope)
        if updated is None:
            raise RuntimeError("Skill 更新后无法解析")
        return updated

    def delete_skill(self, name: str, scope: str) -> None:
        record = self.get_skill_record(name, scope=scope)
        if record is None:
            raise KeyError(f"Skill 不存在: {name}")
        if record.source in {"builtin", "plugin"}:
            raise PermissionError("只允许删除用户或工作区 Skill")
        scope_root = self._scope_root(scope).resolve()
        target = record.root_dir.resolve()
        if target.parent != scope_root:
            raise PermissionError("Skill 目录不在受管理的作用域内")
        shutil.rmtree(record.root_dir)
        self._set_enabled(name, scope, None)

    def set_skill_enabled(self, name: str, scope: str, enabled: bool) -> SkillRecord:
        record = self.get_skill_record(name, scope=scope)
        if record is None:
            raise KeyError(f"Skill 不存在: {name}")
        if record.source in {"builtin", "plugin"} and not enabled:
            raise PermissionError("内置或插件 Skill 不能单独停用")
        self._set_enabled(name, scope, enabled)
        refreshed = self.get_skill_record(name, scope=scope)
        if refreshed is None:
            raise RuntimeError("Skill 状态更新后无法读取")
        return refreshed

    def install_directory(self, source: str | Path, scope: str, *, mode: str = "copy", name: str | None = None) -> list[SkillRecord]:
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_dir():
            raise ValueError("Skill 来源目录不存在")
        if mode not in {"copy", "symlink"}:
            raise ValueError("不支持的 Skill 安装模式")
        scope_root = self._scope_root(scope).resolve()
        if source_path == scope_root or source_path.is_relative_to(scope_root) or scope_root.is_relative_to(source_path):
            raise ValueError("Skill 来源目录不能位于安装目标目录内")
        candidates = [source_path] if (source_path / "SKILL.md").is_file() else [item for item in sorted(source_path.iterdir()) if item.is_dir() and (item / "SKILL.md").is_file()]
        if name and len(candidates) != 1:
            raise ValueError("只有单个 Skill 来源时才能指定名称")
        installed: list[SkillRecord] = []
        for candidate in candidates:
            skill_name = self._validate_name(name if name else candidate.name)
            target = self._scope_root(scope) / skill_name
            if target.exists():
                raise ValueError(f"Skill 已存在: {skill_name}")
            if candidate.is_symlink() or (candidate / "SKILL.md").is_symlink():
                raise ValueError("Skill 来源不能通过符号链接安装")
            if any(item.is_symlink() for item in candidate.rglob("*")):
                raise ValueError("Skill 来源包含未校验的符号链接")
            if mode == "symlink":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(candidate, target_is_directory=True)
            else:
                shutil.copytree(candidate, target, symlinks=False)
            record = self.get_skill_record(skill_name, scope=scope)
            if record is not None:
                installed.append(record)
        if not installed:
            raise ValueError("来源目录中未发现包含 SKILL.md 的 Skill")
        return installed

    def install_git(self, url: str, scope: str, *, revision: str | None = None, mode: str = "copy") -> list[SkillRecord]:
        if not re.match(r"^(https://|ssh://|git@)[^\s]+$", url):
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
            return self.install_directory(directory, scope, mode=mode)

    def load_skill_record(self, name: str) -> SkillRecord | None:
        """按规范名称读取 Skill；名称不匹配时不猜测或模糊路由。"""

        requested = name.strip()
        return next(
            (
                record
                for record in self.list_skill_records(filter_unavailable=False)
                if record.name == requested
            ),
            None,
        )

    def load_skill_body(self, name: str) -> str | None:
        """返回可用 Skill 的正文，不向模型泄漏 YAML frontmatter。"""

        record = self.load_skill_record(name)
        if record is None or not record.available:
            return None
        return self._strip_frontmatter(record.content)

    def get_always_skills(self) -> list[str]:
        """返回每轮都应注入的 Skill，顺序与稳定索引一致。"""

        return [record.name for record in self.list_skill_records() if record.always]

    def load_skills_for_context(self, names: list[str]) -> str:
        """按调用方给定顺序拼接命中 Skill 正文，并稳定去重。"""

        parts: list[str] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            body = self.load_skill_body(name)
            if body:
                parts.append(f"### Skill: {name}\n\n{body}")
        return "\n\n---\n\n".join(parts)

    def build_skills_summary(self) -> str:
        """构建不含正文和磁盘路径的稳定 Skill 目录。"""

        records = self.list_skill_records(filter_unavailable=False)
        if not records:
            return ""
        lines = ["<skills>"]
        for record in records:
            lines.append(
                f'  <skill name="{self._escape_xml(record.name)}" '
                f'available="{str(record.available).lower()}" '
                f'source="{self._escape_xml(record.source)}">'
            )
            lines.append(
                f"    <description>{self._escape_xml(record.description)}</description>"
            )
            if record.when_to_use:
                lines.append(
                    "    <when_to_use>"
                    f"{self._escape_xml(record.when_to_use)}"
                    "</when_to_use>"
                )
            if not record.available and record.missing:
                lines.append(
                    f"    <requires>{self._escape_xml(record.missing)}</requires>"
                )
            lines.append("  </skill>")
        lines.append("</skills>")
        return "\n".join(lines)

    def _build_index(self) -> list[SkillRecord]:
        # workspace > user > plugin > builtin；显式按来源写入索引，不能依赖
        # 文件系统遍历顺序决定覆盖关系。
        records: dict[str, SkillRecord] = {}
        for record in self._scan_skills_dir(
            self.skills_dir,
            source="workspace",
            source_id="workspace",
            reject_symlinks=True,
            scope="workspace",
        ):
            records[record.name] = record
        if self.user_skills_dir is not None:
            for record in self._scan_skills_dir(
                self.user_skills_dir,
                source="user",
                source_id="user",
                reject_symlinks=True,
                scope="user",
            ):
                records.setdefault(record.name, record)
        plugin_roots = list(self.plugin_skill_roots)
        workspace_plugins = self.workspace / "plugins"
        if workspace_plugins.is_dir():
            plugin_roots.extend(
                (directory.name, directory / "skills")
                for directory in sorted(workspace_plugins.iterdir(), key=lambda item: item.name)
                if directory.is_dir()
            )
        for plugin_name, plugin_root in plugin_roots:
            for record in self._scan_skills_dir(
                plugin_root,
                source="plugin",
                source_id=f"plugin:{plugin_name}",
                reject_symlinks=True,
                scope="workspace",
                plugin_name=plugin_name,
            ):
                records.setdefault(record.name, record)
        if self.builtin_skills_dir is not None:
            for record in self._scan_skills_dir(
                self.builtin_skills_dir,
                source="builtin",
                source_id="builtin",
                reject_symlinks=False,
                scope="builtin",
            ):
                records.setdefault(record.name, record)
        return sorted(records.values(), key=lambda record: record.name)

    def _build_scope_index(self, scope: str) -> list[SkillRecord]:
        """构建管理页的来源列表，不因另一作用域同名而隐藏记录。"""
        records: dict[str, SkillRecord] = {}
        if scope == "workspace":
            for record in self._scan_skills_dir(self.skills_dir, source="workspace", source_id="workspace", reject_symlinks=True, scope="workspace"):
                records[record.name] = record
            plugin_roots = list(self.plugin_skill_roots)
            workspace_plugins = self.workspace / "plugins"
            if workspace_plugins.is_dir():
                plugin_roots.extend((directory.name, directory / "skills") for directory in sorted(workspace_plugins.iterdir(), key=lambda item: item.name) if directory.is_dir())
            for plugin_name, plugin_root in plugin_roots:
                for record in self._scan_skills_dir(plugin_root, source="plugin", source_id=f"plugin:{plugin_name}", reject_symlinks=True, scope="workspace", plugin_name=plugin_name):
                    records.setdefault(f"plugin:{plugin_name}:{record.name}", record)
        else:
            if self.user_skills_dir is not None:
                for record in self._scan_skills_dir(self.user_skills_dir, source="user", source_id="user", reject_symlinks=True, scope="user"):
                    records[record.name] = record
            if self.builtin_skills_dir is not None:
                for record in self._scan_skills_dir(self.builtin_skills_dir, source="builtin", source_id="builtin", reject_symlinks=False, scope="builtin"):
                    records.setdefault(record.name, record)
        return sorted(records.values(), key=lambda record: record.name)

    def _scan_skills_dir(
        self,
        skills_dir: Path,
        *,
        source: str,
        source_id: str,
        reject_symlinks: bool,
        scope: str,
        plugin_name: str | None = None,
    ) -> list[SkillRecord]:
        """扫描一个 Skill 根目录；workspace 额外拒绝符号链接越界。"""

        if not skills_dir.is_dir():
            return []
        records: list[SkillRecord] = []
        for skill_dir in sorted(skills_dir.iterdir(), key=lambda item: item.name):
            # 符号链接即使当前目标仍在 workspace 内也不读取，避免目标后来被替换后越界。
            if (reject_symlinks and skill_dir.is_symlink()) or not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file() or (reject_symlinks and skill_file.is_symlink()):
                continue
            diagnostics: tuple[str, ...] = ()
            content = ""
            metadata: dict[str, Any] = {}
            try:
                if skill_file.stat().st_size > 1024 * 1024:
                    raise ValueError("SKILL.md 超过 1 MiB 限制")
                content = skill_file.read_text(encoding="utf-8")
                metadata = self._parse_frontmatter(content)
            except (OSError, UnicodeError, yaml.YAMLError) as error:
                diagnostics = (f"无法解析 SKILL.md：{error}",)
                logger.warning("Skill 解析失败: path=%s error=%s", skill_file, error)
            except ValueError as error:
                content = ""
                metadata = {}
                diagnostics = (str(error),)
            name = str(metadata.get("name") or skill_dir.name).strip()
            if not name:
                logger.warning("跳过名称为空的 Skill: path=%s", skill_file)
                continue
            config = self._skill_config(metadata.get("metadata"))
            missing = self._missing_requirements(config)
            state = self._read_state().get(self._state_key(name, scope), {})
            enabled = bool(state.get("enabled", True)) if isinstance(state, dict) else True
            status = "invalid" if diagnostics else "missing_dependency" if missing else "disabled" if not enabled else "available"
            records.append(
                SkillRecord(
                    name=name,
                    source=source,
                    source_id=source_id,
                    root_dir=skill_dir,
                    skill_file=skill_file,
                    content=content,
                    description=str(metadata.get("description") or name),
                    when_to_use=str(metadata.get("when_to_use") or ""),
                    always=self._as_bool(metadata.get("always"))
                    or self._as_bool(config.get("always")),
                    available=not missing and not diagnostics,
                    missing=missing,
                    scope=scope,
                    version=str(metadata.get("version") or "") or None,
                    plugin_name=plugin_name,
                    status=status,
                    diagnostics=diagnostics,
                    revision=int(state.get("revision", 1)) if isinstance(state, dict) and isinstance(state.get("revision", 1), int) else 1,
                    enabled=enabled,
                )
            )
        return records

    def _scope_root(self, scope: str) -> Path:
        if scope == "workspace":
            return self.skills_dir
        if scope == "user" and self.user_skills_dir is not None:
            return self.user_skills_dir
        raise ValueError("不支持的 Skill 作用域")

    def _read_state(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for path in self._state_paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                merged.update(payload)
        return merged

    def _set_enabled(self, name: str, scope: str, enabled: bool | None) -> None:
        if scope == "user" and self.user_skills_dir is None:
            raise ValueError("用户级 Skill 目录未配置")
        path = self._state_paths[1 if scope == "user" and len(self._state_paths) > 1 else 0]
        state = self._read_state_file(path)
        key = self._state_key(name, scope)
        if enabled is None:
            state.pop(key, None)
        else:
            current = state.get(key) if isinstance(state.get(key), dict) else {}
            state[key] = {
                "enabled": enabled,
                "revision": int(current.get("revision", 0)) + 1,
                "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2))

    def _bump_revision(self, name: str, scope: str, *, enabled: bool) -> None:
        """内容变更也递增 revision，避免编辑器覆盖较新的文件。"""
        if scope == "user" and self.user_skills_dir is None:
            raise ValueError("用户级 Skill 目录未配置")
        path = self._state_paths[1 if scope == "user" and len(self._state_paths) > 1 else 0]
        state = self._read_state_file(path)
        key = self._state_key(name, scope)
        current = state.get(key) if isinstance(state.get(key), dict) else {}
        state[key] = {
            "enabled": enabled,
            "revision": int(current.get("revision", 1)) + 1,
            "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2))

    @staticmethod
    def _state_key(name: str, scope: str) -> str:
        return f"{scope}:{name}"

    @staticmethod
    def _read_state_file(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _validate_name(name: str) -> str:
        normalized = str(name or "").strip()
        if not _SKILL_NAME.fullmatch(normalized):
            raise ValueError("Skill 名称包含非法字符")
        return normalized

    @staticmethod
    def _validate_content(content: str) -> None:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("SKILL.md 内容不能为空")
        if len(content.encode("utf-8")) > 1024 * 1024:
            raise ValueError("SKILL.md 超过 1 MiB 限制")
        if content.startswith("---"):
            SkillsLoader._parse_frontmatter(content)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        _atomic_write_path(path, content)

    @staticmethod
    def _parse_frontmatter(content: str) -> dict[str, Any]:
        if not content.startswith("---"):
            return {}
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}
        loaded = yaml.safe_load(parts[1]) or {}
        if not isinstance(loaded, dict):
            raise yaml.YAMLError("Skill frontmatter 必须是对象")
        return {str(key): value for key, value in loaded.items()}

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        if not content.startswith("---"):
            return content.strip()
        parts = content.split("---", 2)
        return parts[2].strip() if len(parts) == 3 else content.strip()

    @staticmethod
    def _skill_config(raw: object) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        for key in ("skill", "beanagent"):
            value = raw.get(key)
            if isinstance(value, dict):
                return {str(item_key): item for item_key, item in value.items()}
        return {str(key): value for key, value in raw.items()}

    @staticmethod
    def _missing_requirements(config: dict[str, Any]) -> str:
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

    @staticmethod
    def _as_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        return isinstance(value, str) and value.lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _escape_xml(value: str) -> str:
        return (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )


__all__ = ["SkillRecord", "SkillRevisionConflict", "SkillSnapshotView", "SkillsLoader", "collect_skill_mentions", "seed_builtin_skills"]
