"""长期记忆软删除工具。"""

from __future__ import annotations

import json
from typing import Any, cast

from memory.contracts import MemoryMutation, MemoryToolSpec, MemoryWriteApi
from tools.base import Tool


class ForgetMemoryTool(Tool):
    name = "forget_memory"
    description = "由当前 memory engine 的 tool_profile 注入工具描述。"
    parameters = {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}}}, "required": ["ids"]}

    def __init__(self, memory: MemoryWriteApi, spec: MemoryToolSpec) -> None:
        self._memory = memory
        self.description = spec.description
        self.parameters = cast(dict[str, Any], spec.parameters)

    async def execute(self, ids: list[str], **_: Any) -> str:
        # 去重但保留输入顺序，使返回的 requested_ids 可直接对应用户请求。
        clean = list(dict.fromkeys(str(item).strip() for item in ids if str(item).strip()))
        if not clean:
            return _render([], [], [], [])
        result = await self._memory.mutate(MemoryMutation(kind="forget", ids=tuple(clean)))
        return _render(clean, result.affected_ids, result.missing_ids, result.items)


def _render(requested: list[str], affected: list[str], missing: list[str], items: list[dict[str, object]]) -> str:
    return json.dumps({"requested_ids": requested, "superseded_ids": affected, "missing_ids": missing, "count": len(affected), "items": items}, ensure_ascii=False)


__all__ = ["ForgetMemoryTool"]
