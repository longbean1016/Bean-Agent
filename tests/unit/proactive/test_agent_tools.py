"""主动 Agent 工具白名单、只读上下文与草稿终态协议测试。"""

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
    recent = json.loads(await tools.execute("get_recent_chat", {"limit": 20}))
    recalled = json.loads(await tools.execute(
        "recall_memory", {"query": "徒步兴趣", "limit": 2}
    ))

    assert names == {
        "get_recent_chat", "recall_memory", "read_file", "message_push", "finish_turn"
    }
    assert "shell" not in names
    assert len(recent["recent_chat"]) == 2
    assert len(recent["recent_proactive"]) == 1
    assert recalled["items"][0]["memory_type"] == "preference"
    assert memory.request.intent == "interest"
    assert memory.request.limit == 2
    with pytest.raises(ProactiveToolError, match="白名单"):
        await tools.execute("shell", {"command": "echo no"})


@pytest.mark.asyncio
async def test_message_push_requires_single_draft_then_reply_finish() -> None:
    tools = ProactiveToolFactory(_Sessions(), None, ToolRegistry()).create("web:a")

    await tools.execute("message_push", {
        "message": "周末想不想去走走？",
        "topic": "徒步",
        "reason": "用户近期表达了兴趣",
    })
    with pytest.raises(ProactiveToolError, match="最多生成一条"):
        await tools.execute("message_push", {"message": "第二条", "topic": "重复"})
    with pytest.raises(ProactiveToolError, match="不能再改为 skip"):
        await tools.execute("finish_turn", {"decision": "skip"})
    await tools.execute("finish_turn", {"decision": "reply"})

    assert tools.decision is not None
    assert tools.decision.decision == "reply"
    assert tools.decision.message == "周末想不想去走走？"
    assert tools.decision.topic == "徒步"
