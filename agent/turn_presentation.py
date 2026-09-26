"""独立于模型上下文的会话展示投影，只记录真实发生的过程。"""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from agent.tool_projection import project_result_preview


class TurnPresentation:
    def __init__(self, started_at: str) -> None:
        self.data: dict[str, Any] = {"version": 1, "started_at": started_at, "parts": []}

    def append_text(self, iteration: int, kind: str, text: str) -> str:
        parts = self.data["parts"]
        if parts and parts[-1]["kind"] == kind and parts[-1].get("iteration") == iteration:
            parts[-1]["text"] += text
            return parts[-1]["id"]
        part_id = f"part-{len(parts)}"
        parts.append({"id": part_id, "kind": kind, "iteration": iteration, "text": text})
        return part_id

    def resolve_response(self, iteration: int, content: str, thinking: str, *, final: bool) -> None:
        # 非流式响应和工具声明后被供应商抑制的正文在完成边界补齐，不重放 delta。
        for kind, text in (("thinking", thinking), ("text", content)):
            matching = [p for p in self.data["parts"] if p.get("iteration") == iteration and p["kind"] == kind]
            if text and not matching:
                self.append_text(iteration, kind, text)
            elif matching and "".join(p["text"] for p in matching) != text:
                matching[0]["text"] = text
                for part in matching[1:]:
                    part["text"] = ""
        if final:
            self.data["final"] = True
            for part in self.data["parts"]:
                if part.get("iteration") == iteration and part["kind"] == "text":
                    part["kind"] = "answer"

    def add_tool(self, call_id: str) -> None:
        self.data["parts"].append({"id": f"tool-{call_id}", "kind": "tool", "call_id": call_id})

    def complete_memory_tool(self, call_id: str, result: str) -> bool:
        """在通用结果截断前提取摘要，保持原工具输出和存储协议不变。"""
        try:
            value = json.loads(result)
        except (ValueError, TypeError):
            return False
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            return False
        items = _memory_items(value["items"])
        for part in self.data["parts"]:
            if part.get("call_id") == call_id:
                part["memory_result"] = {"count": len(value["items"]), "items": items}
                return True
        return False

    def add_memory(self, details: dict[str, Any], duration_ms: int) -> None:
        if details.get("items"):
            self.data["parts"].append({
                "id": "automatic-memory", "kind": "memory", "query": project_result_preview(details["query"]),
                "items": _memory_items(details["items"]), "duration_ms": duration_ms,
            })

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.data)


def _memory_items(items: list[Any]) -> list[dict[str, str]]:
    return [
        {"id": str(item.get("id") or ""), "summary": project_result_preview(item.get("summary"), max_length=1500)}
        for item in items if isinstance(item, dict) and isinstance(item.get("summary"), str)
    ]
