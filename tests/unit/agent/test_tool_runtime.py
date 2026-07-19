"""Turn 局部工具运行时的状态隔离测试。"""

from __future__ import annotations

from agent.tool_runtime import ToolRuntimeView


def test_tool_runtime_views_keep_context_and_visibility_isolated() -> None:
    first = ToolRuntimeView.create(
        channel="web",
        chat_id="chat-a",
        session_key="web:chat-a",
        visible_names={"tool_search"},
    )
    second = ToolRuntimeView.create(
        channel="web",
        chat_id="chat-b",
        session_key="web:chat-b",
        visible_names={"tool_search"},
    )

    first.unlock(["mcp_files__read"])

    assert first.context == {
        "channel": "web",
        "chat_id": "chat-a",
        "session_key": "web:chat-a",
    }
    assert first.visible_order == ["tool_search", "mcp_files__read"]
    assert first.unlocked_names == {"mcp_files__read"}
    assert second.visible_order == ["tool_search"]
    assert second.unlocked_names == set()
    assert first.context is not first.context


def test_tool_runtime_preserves_explicit_registration_order() -> None:
    view = ToolRuntimeView.create(
        channel="web",
        chat_id="chat",
        session_key="web:chat",
        visible_names=["tool_search", "mcp_add", "mcp_list"],
    )

    assert view.visible_order == ["tool_search", "mcp_add", "mcp_list"]
