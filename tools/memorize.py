"""长期记忆写入工具。"""

from __future__ import annotations

from typing import Any, cast

from memory.contracts import MemoryMutation, MemoryScope, MemoryToolSpec, MemoryWriteApi
from tools.base import Tool


class MemorizeTool(Tool):
    name = "memorize"
    description = "由当前 memory engine 的 tool_profile 注入工具描述。"
    parameters = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}

    def __init__(self, memory: MemoryWriteApi, spec: MemoryToolSpec) -> None:
        self._memory = memory
        self.description = spec.description
        self.parameters = cast(dict[str, Any], spec.parameters)

    async def execute(self, summary: str, memory_kind: str = "", tool_requirement: str | None = None, steps: list[str] | None = None, metadata: dict[str, object] | None = None, current_user_source_ref: str | None = None, channel: str | None = None, chat_id: str | None = None, **extra: Any) -> str:
        payload = dict(metadata or {})
        payload.update(extra)
        if tool_requirement is not None: payload["tool_requirement"] = tool_requirement
        if steps is not None: payload["steps"] = steps
        result = await self._memory.mutate(MemoryMutation(kind="remember", summary=summary, memory_kind=memory_kind.strip(), source_ref=str(current_user_source_ref or "").strip(), scope=MemoryScope(session_key=f"{channel}:{chat_id}" if channel and chat_id else "", channel=channel or "", chat_id=chat_id or ""), metadata=payload))
        kind = f"；kind={result.actual_kind}" if result.actual_kind else ""
        return f"已记住（item_id={result.item_id}{kind}；status={result.status}）：{summary}"


__all__ = ["MemorizeTool"]
