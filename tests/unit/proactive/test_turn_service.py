"""主动消息提交与审计轨迹持久化测试。"""

from __future__ import annotations

import pytest

from agent.message_bus import MessageBus
from proactive.store import ProactiveStore
from proactive.turn_service import ProactiveTurnService
from session.manager import SessionManager


@pytest.mark.asyncio
async def test_delivery_persists_filtered_tool_chain_on_assistant_message(tmp_path) -> None:
    store = ProactiveStore(tmp_path / "proactive.db")
    sessions = SessionManager(tmp_path)
    service = ProactiveTurnService(store, sessions, MessageBus())
    tool_chain = [{
        "text": "",
        "calls": [{
            "call_id": "call-memory",
            "name": "recall_memory",
            "arguments": {"query": "面试准备"},
            "result": "用户正在准备 Agent 面试",
            "status": "ok",
        }],
    }]

    result = await service.deliver(
        session_key="web:test",
        content="给你一道关于记忆去重的面试题。",
        source="proactive_conversation",
        delivery_key="conversation:test",
        source_id="test",
        tool_chain=tool_chain,
    )

    rows, total = sessions.store.list_chat_messages("web:test")
    assert result.delivered is True
    assert total == 1
    assert rows[0]["tool_chain"] == tool_chain
    await sessions.close()
    store.close()
