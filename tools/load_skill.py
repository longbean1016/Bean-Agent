"""向模型按需提供完整 Skill 指令的工具。"""

from __future__ import annotations

from typing import Any

from agent.skills import SkillsLoader
from tools.base import Tool


class LoadSkillTool(Tool):
    """根据稳定目录中的名称加载正文，避免所有 Skill 常驻 Prompt。"""

    name = "load_skill"
    description = (
        "按名称加载完整 SKILL.md 指令。需要执行目录中某个 Skill 时，"
        "必须先调用本工具读取正文和相对路径基准。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "需要加载的 Skill 名称",
            }
        },
        "required": ["skill"],
    }

    def __init__(self, skills: SkillsLoader) -> None:
        self._skills = skills

    async def execute(self, skill: str, **kwargs: Any) -> str:
        name = skill.strip()
        if not name:
            return "错误：缺少 Skill 名称。"
        record = self._skills.load_skill_record(name)
        if record is None:
            available = [
                item.name
                for item in self._skills.list_skill_records(filter_unavailable=False)
            ]
            suffix = f"\n已发现 Skill：{', '.join(available)}" if available else ""
            return f"错误：未找到 Skill：{name}。{suffix}"
        if not record.available:
            return f"错误：Skill 不可用：{name}。\n缺少依赖：{record.missing}"
        body = self._skills.load_skill_body(name)
        if not body:
            return f"错误：Skill 正文为空：{name}。"
        return (
            f"# Skill: {record.name}\n\n"
            f"Source: {record.source}\n"
            f"Base directory: {record.root_dir.resolve()}\n\n"
            "相对路径必须以 Base directory 为基准解析。\n\n"
            "---\n\n"
            f"{body}"
        )


__all__ = ["LoadSkillTool"]
