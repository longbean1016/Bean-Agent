"""工作区 Skill 的确定性索引、依赖检查与正文加载。

Skill 目录摘要会进入稳定 Prompt 前缀，因此扫描和输出顺序必须完全确定；
完整正文只在本轮明确命中或模型调用加载工具时读取，避免所有 SKILL.md
高频进入主上下文。当前实现仅接受 workspace/skills 下的真实目录，不跟随符号
链接，以免 Skill 借助路径跳转读取工作区外内容。
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SkillRecord:
    """一份 SKILL.md 的只读索引记录。"""

    name: str
    root_dir: Path
    skill_file: Path
    content: str
    description: str
    when_to_use: str
    always: bool
    available: bool
    missing: str


class SkillsLoader:
    """扫描并读取工作区 ``skills/*/SKILL.md``。

    每次公开查询都重新构建轻量索引，使用户修改 Skill 后下一轮立即生效；稳定
    目录块使用摘要本身作为缓存签名，内容未变化时仍能复用本地渲染和供应商前缀。
    """

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.skills_dir = self.workspace / "skills"

    def list_skill_records(
        self,
        *,
        filter_unavailable: bool = True,
    ) -> list[SkillRecord]:
        """按 Skill 名称返回稳定排序的索引。"""

        records = self._build_index()
        if filter_unavailable:
            return [record for record in records if record.available]
        return records

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
                f'available="{str(record.available).lower()}">'
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
        if not self.skills_dir.is_dir():
            return []
        records: list[SkillRecord] = []
        for skill_dir in sorted(self.skills_dir.iterdir(), key=lambda item: item.name):
            # 符号链接即使当前目标仍在 workspace 内也不读取，避免目标后来被替换后越界。
            if skill_dir.is_symlink() or not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file() or skill_file.is_symlink():
                continue
            try:
                content = skill_file.read_text(encoding="utf-8")
                metadata = self._parse_frontmatter(content)
            except (OSError, UnicodeError, yaml.YAMLError) as error:
                logger.warning("跳过无法解析的 Skill: path=%s error=%s", skill_file, error)
                continue
            name = str(metadata.get("name") or skill_dir.name).strip()
            if not name:
                logger.warning("跳过名称为空的 Skill: path=%s", skill_file)
                continue
            config = self._skill_config(metadata.get("metadata"))
            missing = self._missing_requirements(config)
            records.append(
                SkillRecord(
                    name=name,
                    root_dir=skill_dir,
                    skill_file=skill_file,
                    content=content,
                    description=str(metadata.get("description") or name),
                    when_to_use=str(metadata.get("when_to_use") or ""),
                    always=self._as_bool(metadata.get("always"))
                    or self._as_bool(config.get("always")),
                    available=not missing,
                    missing=missing,
                )
            )
        return sorted(records, key=lambda record: record.name)

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


__all__ = ["SkillRecord", "SkillsLoader"]
