"""WebSocket 到 stdio MCP、Session 提交和历史续轮的离线闭环。"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

from agent.config_models import Config
from agent.event_bus import TurnCommitted
from agent.provider import LLMResponse, ToolCall
from bootstrap.app import build_core_runtime, create_fastapi_app


class _Provider:
    def __init__(self, command: list[str], env: dict[str, str]) -> None:
        self.command = command
        self.env = env
        self.calls = 0
        self.messages: list[list[dict[str, object]]] = []
        self.schemas: list[list[str]] = []

    async def chat(self, messages, tools=None, on_content_delta=None, **kwargs):
        self.calls += 1
        self.messages.append(list(messages))
        self.schemas.append([item["function"]["name"] for item in tools or []])
        if self.calls == 1:
            return LLMResponse(
                None,
                [
                    ToolCall(
                        "add",
                        "mcp_add",
                        {"name": "demo", "command": self.command, "env": self.env},
                    )
                ],
            )
        if self.calls == 2:
            return LLMResponse(
                None,
                [ToolCall("search", "tool_search", {"query": "回显"})],
            )
        if self.calls == 3:
            return LLMResponse(
                None,
                [ToolCall("echo", "mcp_demo__echo", {"text": "hello"})],
            )
        content = "MCP 完成" if self.calls == 4 else "第二轮完成"
        if on_content_delta is not None:
            await on_content_delta({"content_delta": content})
        return LLMResponse(content)

    async def close(self) -> None:
        self.closed = True


def _receive_final(websocket) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    while True:
        frame = websocket.receive_json()
        frames.append(frame)
        if frame["type"] == "message.final":
            return frames


def test_websocket_mcp_turn_commits_and_next_turn_loads_history(tmp_path: Path) -> None:
    config = Config()
    config.memory.enabled = False
    config.agent.workdir = str(tmp_path / "workdir")
    workspace = tmp_path / "workspace"
    server = Path(__file__).parents[2] / "fixtures" / "stdio_mcp_server.py"
    provider = _Provider(
        [sys.executable, "-u", str(server)],
        {"BEANAGENT_MCP_TEST_LOG": str(tmp_path / "mcp.jsonl")},
    )
    core = build_core_runtime(config, workspace, provider=provider)
    committed: list[TurnCommitted] = []
    core.event_bus.on(TurnCommitted, committed.append)
    app = create_fastapi_app(core)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "message.send",
                    "request_id": "r1",
                    "session_id": "web:mcp",
                    "text": "添加并调用回显 MCP",
                }
            )
            first_frames = _receive_final(websocket)
            websocket.send_json(
                {
                    "type": "message.send",
                    "request_id": "r2",
                    "session_id": "web:mcp",
                    "text": "总结上一轮",
                }
            )
            second_frames = _receive_final(websocket)

        rows = core.sessions.store.fetch_session_messages("web:mcp")

    completed_tools = [
        frame["tool_name"]
        for frame in first_frames
        if frame["type"] == "react.tool.completed"
    ]
    assert completed_tools == ["mcp_add", "tool_search", "mcp_demo__echo"]
    assert first_frames[-1]["content"] == "MCP 完成"
    assert second_frames[-1]["content"] == "第二轮完成"
    assert len(committed) == 2
    assert [row["role"] for row in rows] == ["user", "assistant", "user", "assistant"]
    assert [call["name"] for call in rows[1]["tool_chain"][0]["calls"]] == ["mcp_add"]
    assert rows[1]["tool_chain"][2]["calls"][0]["name"] == "mcp_demo__echo"
    second_turn_messages = provider.messages[-1]
    assert any(item.get("role") == "tool" for item in second_turn_messages)
    assert "mcp_demo__echo" not in provider.schemas[-1]
