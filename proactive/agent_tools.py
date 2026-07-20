"""主动 Agent 的只读工具视图与单次决策状态。"""

from __future__ import annotations

import asyncio
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
    """工具越权、参数错误或决策协议冲突；上层必须将本次 tick 降级为 skip。"""


@dataclass(frozen=True, slots=True)
class ProactiveToolDecision:
    decision: Literal["reply", "skip"]
    message: str = ""
    topic: str = ""
    reason: str = ""


class ProactiveToolFactory:
    """持有共享只读依赖，并为每次主动判断创建互不污染的状态容器。"""

    def __init__(self, session_store: Any, memory: Any | None, tools: ToolRegistry) -> None:
        self._sessions = session_store
        self._memory = memory
        self._tools = tools

    def create(self, session_key: str) -> ProactiveToolSession:
        return ProactiveToolSession(
            session_key,
            self._sessions,
            self._memory,
            self._tools,
        )


class ProactiveToolSession:
    """单次 tick 的工具执行器；草稿和终态绝不能跨会话或跨 tick 复用。"""

    def __init__(
        self,
        session_key: str,
        session_store: Any,
        memory: Any | None,
        tools: ToolRegistry,
    ) -> None:
        channel, separator, chat_id = str(session_key).partition(":")
        if not separator or not channel or not chat_id:
            raise ValueError("主动工具 session_key 无效")
        self.session_key = session_key
        self.channel = channel
        self.chat_id = chat_id
        self._sessions = session_store
        self._memory = memory
        self._tools = tools
        self._draft = ""
        self._topic = ""
        self._push_reason = ""
        self._decision: ProactiveToolDecision | None = None
        self._recent_chat_read = False

    @property
    def decision(self) -> ProactiveToolDecision | None:
        return self._decision

    def schemas(self) -> list[dict[str, Any]]:
        """只返回主动判断所需工具；共享注册表中的其它能力保持不可见。"""

        schemas = [_GET_RECENT_CHAT_SCHEMA, _RECALL_MEMORY_SCHEMA]
        for name in _SHARED_TOOL_NAMES:
            metadata = self._tools.get_metadata(name)
            tool = self._tools.get_tool(name)
            # 同名 MCP 可覆盖全局注册项，因此这里同时校验来源，不能只检查名称。
            if (
                tool is not None
                and metadata is not None
                and metadata.source_type == "builtin"
                and metadata.risk == "read-only"
            ):
                schemas.append(tool.to_schema())
        schemas.extend((_MESSAGE_PUSH_SCHEMA, _FINISH_TURN_SCHEMA))
        return schemas

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        if self._decision is not None:
            raise ProactiveToolError("主动 tick 已结束，不能继续调用工具")
        if name == "get_recent_chat":
            return await self._get_recent_chat(arguments)
        if name == "recall_memory":
            return await self._recall_memory(arguments)
        if name == "message_push":
            return self._message_push(arguments)
        if name == "finish_turn":
            return self._finish_turn(arguments)
        if name not in _SHARED_TOOL_NAMES:
            raise ProactiveToolError(f"工具 '{name}' 不在主动 Agent 白名单")
        metadata = self._tools.get_metadata(name)
        tool = self._tools.get_tool(name)
        if (
            tool is None
            or metadata is None
            or metadata.source_type != "builtin"
            or metadata.risk != "read-only"
        ):
            raise ProactiveToolError(f"工具 '{name}' 不是可用的内置只读工具")
        errors = tool.validate_params(arguments)
        if errors:
            raise ProactiveToolError("；".join(errors))
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

    async def _get_recent_chat(self, arguments: dict[str, Any]) -> str:
        limit = _integer(arguments.get("limit", 20), "limit", minimum=1, maximum=20)
        rows = await asyncio.to_thread(
            _recent_rows,
            self._sessions,
            self.session_key,
        )
        chat = [row for row in rows if row.get("role") in {"user", "assistant"}]
        passive: list[dict[str, object]] = []
        proactive: list[dict[str, object]] = []
        for row in chat:
            item = {
                "role": str(row.get("role") or ""),
                "content": str(row.get("content") or ""),
                "timestamp": str(row.get("timestamp") or ""),
            }
            if bool(row.get("proactive")) or bool(
                (row.get("metadata") or {}).get("proactive")
                if isinstance(row.get("metadata"), dict)
                else False
            ):
                proactive.append(item)
            else:
                passive.append(item)
        passive = passive[-limit:]
        # 主动历史只用于避免重复打扰，不与普通聊天争夺 20 条上下文预算。
        proactive = proactive[-limit:]
        self._recent_chat_read = True
        return json.dumps(
            {"recent_chat": passive, "recent_proactive": proactive},
            ensure_ascii=False,
        )

    async def _recall_memory(self, arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ProactiveToolError("recall_memory 缺少 query")
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

    def _message_push(self, arguments: dict[str, Any]) -> str:
        if not self._recent_chat_read:
            raise ProactiveToolError("message_push 前必须先调用 get_recent_chat")
        if self._draft:
            raise ProactiveToolError("每次主动 tick 最多生成一条消息草稿")
        message = str(arguments.get("message") or "").strip()
        topic = str(arguments.get("topic") or "").strip()
        if not message or not topic:
            raise ProactiveToolError("message_push 必须包含非空 message 和 topic")
        self._draft = message
        self._topic = topic
        self._push_reason = str(arguments.get("reason") or "").strip()
        return json.dumps({"drafted": True}, ensure_ascii=False)

    def _finish_turn(self, arguments: dict[str, Any]) -> str:
        if not self._recent_chat_read:
            raise ProactiveToolError("finish_turn 前必须先调用 get_recent_chat")
        decision = str(arguments.get("decision") or "").strip()
        reason = str(arguments.get("reason") or "").strip()
        if decision == "reply":
            if not self._draft:
                raise ProactiveToolError("finish_turn(reply) 前必须调用 message_push")
            self._decision = ProactiveToolDecision(
                "reply",
                message=self._draft,
                topic=self._topic,
                reason=reason or self._push_reason,
            )
        elif decision == "skip":
            if self._draft:
                raise ProactiveToolError("message_push 后不能再改为 skip")
            self._decision = ProactiveToolDecision("skip", reason=reason or "model_skip")
        else:
            raise ProactiveToolError("finish_turn decision 必须是 reply 或 skip")
        return json.dumps({"finished": True, "decision": decision}, ensure_ascii=False)


def _integer(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ProactiveToolError(f"{name} 必须是整数")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ProactiveToolError(f"{name} 必须是整数") from error
    if result < minimum or result > maximum:
        raise ProactiveToolError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return result


def _recent_rows(session_store: Any, session_key: str) -> list[dict[str, Any]]:
    """读取会话尾部窗口，避免长会话永远只返回最早的消息。"""

    _head, total = session_store.list_chat_messages(session_key, limit=1, offset=0)
    rows, _ = session_store.list_chat_messages(
        session_key,
        limit=500,
        offset=max(0, total - 500),
    )
    return rows


_GET_RECENT_CHAT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_recent_chat",
        "description": "读取当前会话最近的普通聊天和主动消息，仅用于判断是否值得打扰。",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
        },
    },
}
_RECALL_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "recall_memory",
        "description": "按候选话题只读检索用户的稳定偏好和个人画像。",
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
_MESSAGE_PUSH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "message_push",
        "description": "生成一条主动聊天草稿；本工具不发送消息。",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "topic": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["message", "topic"],
        },
    },
}
_FINISH_TURN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "finish_turn",
        "description": "以 reply 或 skip 明确结束本次主动判断。",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["reply", "skip"]},
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
]
