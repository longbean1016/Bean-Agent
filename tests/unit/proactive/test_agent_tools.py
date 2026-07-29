"""主动 Agent 工具白名单、只读上下文与终态协议测试。"""

from __future__ import annotations

import json

import pytest

from memory.contracts import MemoryQueryResult, MemoryRecord
from proactive.agent_tools import ProactiveToolError, ProactiveToolFactory
from tools.base import Tool
from tools.registry import ToolRegistry


class _Sessions:
    def list_chat_messages(self, session_key: str, *, limit: int, offset: int):
        return ([
            {"role": "user", "content": "最近想去徒步", "timestamp": "1"},
            {"role": "assistant", "content": "可以看看周边路线", "timestamp": "2"},
            {
                "role": "assistant",
                "content": "之前的主动消息",
                "timestamp": "3",
                "proactive": True,
            },
        ], 3)


class _SessionRows:
    def __init__(self, rows_by_session: dict[str, list[dict[str, object]]]) -> None:
        self._rows_by_session = rows_by_session

    def list_chat_messages(self, session_key: str, *, limit: int, offset: int):
        rows = self._rows_by_session.get(session_key, [])
        return rows[offset:offset + limit], len(rows)


class _Memory:
    request = None

    async def query(self, request):
        self.request = request
        return MemoryQueryResult(
            records=[MemoryRecord("m1", "preference", "用户喜欢徒步", 0.9)],
            trace={"read_only": True},
        )


class _ReadFile(Tool):
    name = "read_file"
    description = "只读文件"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    async def execute(self, path: str, **kwargs):
        return f"read:{path}"


class _Shell(Tool):
    name = "shell"
    description = "执行命令"
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}}

    async def execute(self, command: str = "", **kwargs):
        return command


@pytest.mark.asyncio
async def test_proactive_tools_expose_only_allowlist_and_read_interest_memory() -> None:
    registry = ToolRegistry()
    registry.register(_ReadFile())
    registry.register(_Shell())
    memory = _Memory()
    tools = ProactiveToolFactory(_Sessions(), memory, registry).create("web:a")

    names = {schema["function"]["name"] for schema in tools.schemas()}
    recalled = json.loads(await tools.execute(
        "recall_memory", {"query": "徒步兴趣", "limit": 2}
    ))

    assert names == {
        "recall_memory", "read_file", "finish_turn"
    }
    assert "shell" not in names
    assert recalled["items"][0]["memory_type"] == "preference"
    assert memory.request.intent == "interest"
    assert memory.request.limit == 2
    with pytest.raises(ProactiveToolError, match="白名单"):
        await tools.execute("shell", {"command": "echo no"})


@pytest.mark.asyncio
async def test_finish_turn_reply_carries_message_topic_and_reason() -> None:
    tools = ProactiveToolFactory(_Sessions(), None, ToolRegistry()).create("web:a")

    result = json.loads(await tools.execute("finish_turn", {
        "decision": "reply",
        "message": "周末想不想去走走？",
        "topic": "徒步",
        "reason": "用户近期表达了兴趣",
    }))

    assert result == {"finished": True, "decision": "reply"}
    assert tools.decision is not None
    assert tools.decision.decision == "reply"
    assert tools.decision.message == "周末想不想去走走？"
    assert tools.decision.topic == "徒步"
    assert tools.decision.reason == "用户近期表达了兴趣"


@pytest.mark.asyncio
async def test_finish_turn_validates_reply_and_skip_shapes() -> None:
    tools = ProactiveToolFactory(_Sessions(), None, ToolRegistry()).create("web:a")

    with pytest.raises(ProactiveToolError, match="reply 必须包含非空 message、topic 和 reason"):
        await tools.execute("finish_turn", {"decision": "reply", "message": "hi", "topic": "t"})

    with pytest.raises(ProactiveToolError, match="skip 不允许包含待发送消息"):
        await tools.execute("finish_turn", {"decision": "skip", "message": "hi", "reason": "no"})

    await tools.execute("finish_turn", {"decision": "skip", "reason": "没有合适话题"})

    assert tools.decision is not None
    assert tools.decision.decision == "skip"
    assert tools.decision.reason == "没有合适话题"
