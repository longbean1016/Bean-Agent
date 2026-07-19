"""查询全局工具目录并为当前 Turn 返回可解锁候选。"""

from __future__ import annotations

import json
from typing import Any, Iterable

from tools.base import Tool
from tools.registry import ToolRegistry


class ToolSearchTool(Tool):
    """只返回候选名称；真正的 Turn 可见性由 Pipeline 更新。"""

    name = "tool_search"
    description = (
        "搜索并加载当前任务需要的工具。已知名称时使用 select:工具名，"
        "未知名称时用功能关键词搜索；结果中的工具可在下一步直接调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "工具名称或功能关键词"},
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 5,
            },
            "allowed_risk": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["read-only", "write", "external-side-effect"],
                },
            },
        },
        "required": ["query"],
    }

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def execute(
        self,
        query: str,
        top_k: int = 5,
        allowed_risk: list[str] | None = None,
        excluded_names: Iterable[str] | None = None,
        **_: Any,
    ) -> str:
        """返回稳定 JSON，避免工具自身持有任何会话或 Turn 状态。"""

        text = str(query or "").strip()
        excluded = {str(name) for name in excluded_names or ()}
        if not text:
            return self._render(tip="query 不能为空")
        if text.casefold().startswith("select:"):
            return self._select(text[7:], excluded, allowed_risk)

        matched = self._registry.search(
            text,
            top_k=top_k,
            allowed_risk=set(allowed_risk) if allowed_risk else None,
            excluded_names=excluded,
        )
        unlocked = [item["name"] for item in matched]
        return self._render(matched=matched, unlocked=unlocked)

    def _select(
        self,
        names_text: str,
        excluded: set[str],
        allowed_risk: list[str] | None,
    ) -> str:
        requested = [name.strip() for name in names_text.split(",") if name.strip()]
        already_loaded: list[str] = []
        unlocked: list[str] = []
        matched: list[dict[str, str]] = []
        missing: list[str] = []
        blocked: list[str] = []
        risk_filter = set(allowed_risk) if allowed_risk else None
        for name in requested:
            if name in excluded:
                already_loaded.append(name)
                continue
            tool = self._registry.get_tool(name)
            meta = self._registry.get_metadata(name)
            if tool is None or meta is None:
                missing.append(name)
                continue
            if risk_filter is not None and meta.risk not in risk_filter:
                blocked.append(name)
                continue
            matched.append(
                {
                    "name": name,
                    "description": tool.description,
                    "risk": meta.risk,
                    "source_type": meta.source_type,
                    "source_name": meta.source_name,
                }
            )
            unlocked.append(name)
        tips: list[str] = []
        if missing:
            tips.append(f"未找到工具: {', '.join(missing)}")
        if blocked:
            tips.append(f"风险级别不匹配: {', '.join(blocked)}")
        return self._render(
            matched=matched,
            unlocked=unlocked,
            already_loaded=already_loaded,
            tip="; ".join(tips),
        )

    @staticmethod
    def _render(
        *,
        matched: list[dict[str, str]] | None = None,
        unlocked: list[str] | None = None,
        already_loaded: list[str] | None = None,
        tip: str = "",
    ) -> str:
        result: dict[str, Any] = {
            "matched": matched or [],
            "unlocked": unlocked or [],
            "already_loaded": already_loaded or [],
        }
        if unlocked:
            result["next_action"] = "unlocked 中的工具已加载，下一步直接调用所需工具"
        if tip:
            result["tip"] = tip
        return json.dumps(result, ensure_ascii=False, indent=2)


__all__ = ["ToolSearchTool"]
