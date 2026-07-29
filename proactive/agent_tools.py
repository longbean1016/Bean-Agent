"""Read-only tool view and terminal decision state for proactive chat."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from memory.contracts import MemoryQuery, MemoryScope
from tools.base import normalize_tool_result
from tools.registry import ToolRegistry

_SHARED_TOOL_NAMES = (
    "web_search",
    "web_fetch",
    "read_file",
    "list_dir",
    "load_skill",
)


class ProactiveToolError(RuntimeError):
    """Tool allowlist, argument, or terminal protocol error."""


@dataclass(frozen=True, slots=True)
class ProactiveToolDecision:
    decision: Literal["reply", "skip"]
    message: str = ""
    topic: str = ""
    reason: str = ""


class ProactiveToolFactory:
    """Creates isolated tool sessions for each proactive judgment."""

    def __init__(
        self,
        session_store: Any,
        memory: Any | None,
        tools: ToolRegistry,
        skills: Any | None = None,
        workspace: str = "",
    ) -> None:
        self._sessions = session_store
        self._memory = memory
        self._tools = tools
        self._skills = skills
        self.workspace = workspace

    @property
    def memory(self) -> Any | None:
        return self._memory

    @property
    def skills(self) -> Any | None:
        return self._skills

    def create(self, session_key: str) -> ProactiveToolSession:
        return ProactiveToolSession(
            session_key,
            self._sessions,
            self._memory,
            self._tools,
        )


class ProactiveToolSession:
    """Tool executor for one proactive tick."""

    def __init__(
        self,
        session_key: str,
        session_store: Any,
        memory: Any | None,
        tools: ToolRegistry,
    ) -> None:
        channel, separator, chat_id = str(session_key).partition(":")
        if not separator or not channel or not chat_id:
            raise ValueError("invalid proactive session_key")
        self.session_key = session_key
        self.channel = channel
        self.chat_id = chat_id
        self._sessions = session_store
        self._memory = memory
        self._tools = tools
        self._decision: ProactiveToolDecision | None = None

    @property
    def decision(self) -> ProactiveToolDecision | None:
        return self._decision

    def schemas(self) -> list[dict[str, Any]]:
        schemas = [_RECALL_MEMORY_SCHEMA]
        for name in _SHARED_TOOL_NAMES:
            metadata = self._tools.get_metadata(name)
            tool = self._tools.get_tool(name)
            if (
                tool is not None
                and metadata is not None
                and metadata.source_type == "builtin"
                and metadata.risk == "read-only"
            ):
                schemas.append(tool.to_schema())
        schemas.append(_FINISH_TURN_SCHEMA)
        return schemas

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        if self._decision is not None:
            raise ProactiveToolError("proactive tick already finished")
        if name == "recall_memory":
            return await self._recall_memory(arguments)
        if name == "finish_turn":
            return self._finish_turn(arguments)
        if name not in _SHARED_TOOL_NAMES:
            raise ProactiveToolError(f"tool '{name}' is not in proactive allowlist / 白名单")
        metadata = self._tools.get_metadata(name)
        tool = self._tools.get_tool(name)
        if (
            tool is None
            or metadata is None
            or metadata.source_type != "builtin"
            or metadata.risk != "read-only"
        ):
            raise ProactiveToolError(f"tool '{name}' is not an available builtin read-only tool")
        errors = tool.validate_params(arguments)
        if errors:
            raise ProactiveToolError("; ".join(errors))
        result = await self._tools.execute(
            name,
            arguments,
            context={
                "session_key": self.session_key,
                "channel": self.channel,
                "chat_id": self.chat_id,
            },
            raise_errors=True,
        )
        return normalize_tool_result(result).text

    async def _recall_memory(self, arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ProactiveToolError("recall_memory missing query")
        limit = _integer(arguments.get("limit", 2), "limit", minimum=1, maximum=2)
        if self._memory is None:
            return json.dumps(
                {"count": 0, "items": [], "trace": {"unavailable": True}},
                ensure_ascii=False,
            )
        result = await self._memory.query(MemoryQuery(
            text=query,
            intent="interest",
            scope=MemoryScope(
                session_key=self.session_key,
                channel=self.channel,
                chat_id=self.chat_id,
            ),
            limit=limit,
        ))
        items = [
            {
                "id": record.id,
                "memory_type": record.kind,
                "summary": record.summary,
                "score": round(record.score, 4),
            }
            for record in result.records
        ]
        return json.dumps(
            {"count": len(items), "items": items, "trace": result.trace},
            ensure_ascii=False,
        )

    def _finish_turn(self, arguments: dict[str, Any]) -> str:
        decision = str(arguments.get("decision") or "").strip()
        reason = str(arguments.get("reason") or "").strip()
        if decision == "reply":
            message = str(arguments.get("message") or "").strip()
            topic = str(arguments.get("topic") or "").strip()
            if not message or not topic or not reason:
                raise ProactiveToolError("reply 必须包含非空 message、topic 和 reason")
            self._decision = ProactiveToolDecision(
                "reply",
                message=message,
                topic=topic,
                reason=reason,
            )
        elif decision == "skip":
            if str(arguments.get("message") or "").strip() or str(arguments.get("topic") or "").strip():
                raise ProactiveToolError("skip 不允许包含待发送消息")
            if not reason:
                raise ProactiveToolError("skip 必须包含非空 reason")
            self._decision = ProactiveToolDecision("skip", reason=reason)
        else:
            raise ProactiveToolError("finish_turn decision must be reply or skip")
        return json.dumps({"finished": True, "decision": decision}, ensure_ascii=False)


def _integer(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ProactiveToolError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ProactiveToolError(f"{name} must be an integer") from error
    if result < minimum or result > maximum:
        raise ProactiveToolError(f"{name} must be between {minimum} and {maximum}")
    return result


def waiting_for_proactive_reply(rows: list[dict[str, Any]]) -> bool:
    """Return true when the latest proactive assistant message has no later user reply."""

    latest_user_index: int | None = None
    latest_proactive_index: int | None = None
    for index, row in enumerate(rows):
        if row.get("role") == "user":
            latest_user_index = index
        elif row.get("role") == "assistant" and _is_proactive(row):
            latest_proactive_index = index
    return latest_proactive_index is not None and (
        latest_user_index is None or latest_proactive_index > latest_user_index
    )


def _is_proactive(row: dict[str, Any]) -> bool:
    metadata = row.get("metadata")
    return bool(row.get("proactive")) or bool(
        metadata.get("proactive") if isinstance(metadata, dict) else False
    )


_RECALL_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "recall_memory",
        "description": "Read user stable preferences and profile by candidate topic.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2},
            },
            "required": ["query"],
        },
    },
}

_FINISH_TURN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "finish_turn",
        "description": "End this proactive judgment with reply or skip.",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["reply", "skip"]},
                "message": {"type": "string"},
                "topic": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["decision"],
        },
    },
}


__all__ = [
    "ProactiveToolDecision",
    "ProactiveToolError",
    "ProactiveToolFactory",
    "ProactiveToolSession",
    "waiting_for_proactive_reply",
]
