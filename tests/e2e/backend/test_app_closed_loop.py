"""真实 FastAPI /ws 的离线两轮闭环验收。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent.config_models import Config
from agent.provider import LLMResponse, ToolCall
from bootstrap.app import build_core_runtime, create_fastapi_app
from memory.contracts import MemoryMutation, MemoryScope


class Embedder:
    async def embed(self, text): return [1.0, 0.0]
    async def embed_batch(self, texts): return [[1.0, 0.0] for _ in texts]
    async def close(self): self.closed = True


class Provider:
    def __init__(self, *, chat_calls: int = 0):
        self.chat_calls = chat_calls
        self.pipeline_messages = []
    async def complete(self, messages, tools=None, **kwargs):
        prompt = messages[0]["content"]
        if "记忆检索决策器" in prompt:
            return SimpleNamespace(content="<decision>RETRIEVE</decision><history_query>回答偏好</history_query>")
        if "只输出一行检索 query" in prompt:
            return SimpleNamespace(content="回答偏好")
        return SimpleNamespace(content="[]")
    async def chat(self, messages, tools=None, on_content_delta=None, **kwargs):
        self.chat_calls += 1
        self.pipeline_messages.append(list(messages))
        if self.chat_calls == 1:
            return LLMResponse(None, [ToolCall("call-1", "list_dir", {"path": "."})])
        content = "第一轮完成" if self.chat_calls == 2 else "第二轮记得你的偏好"
        if on_content_delta:
            await on_content_delta({"content_delta": content})
        return LLMResponse(content)
    async def close(self): self.closed = True


def _receive_final(websocket):
    frames = []
    while True:
        frame = websocket.receive_json()
        frames.append(frame)
        if frame["type"] == "message.final":
            return frames


def test_websocket_tool_turn_reconnects_with_history_and_memory(tmp_path: Path) -> None:
    config = Config()
    config.memory.enabled = True
    config.memory.embedding.dimensions = 2
    config.memory.optimizer.enabled = False
    config.agent.workdir = str(tmp_path / "workdir")
    workspace = tmp_path / "workspace"
    first_provider = Provider()
    first_embedder = Embedder()
    first_core = build_core_runtime(
        config,
        workspace,
        provider=first_provider,
        embedder=first_embedder,
    )
    assert first_core.memory is not None
    asyncio.run(first_core.memory.mutate(MemoryMutation(
        kind="remember", summary="用户偏好中文回答", memory_kind="preference",
        source_ref="web:c:seed", scope=MemoryScope(channel="web", chat_id="c"),
    )))
    first_app = create_fastapi_app(first_core)

    with TestClient(first_app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "message.send", "request_id": "r1", "session_id": "web:c", "text": "列出目录"})
            first_frames = _receive_final(websocket)

    assert first_provider.closed is True
    assert first_embedder.closed is True

    # 第一轮 runtime 已完全关闭；第二轮重新组装所有服务，证明 Session、工具链
    # 和记忆来自 workspace 持久化，而不是进程内缓存或旧连接残留。
    second_provider = Provider(chat_calls=2)
    second_embedder = Embedder()
    second_core = build_core_runtime(
        config,
        workspace,
        provider=second_provider,
        embedder=second_embedder,
    )
    second_app = create_fastapi_app(second_core)
    with TestClient(second_app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "message.send", "request_id": "r2", "session_id": "web:c", "text": "你记得我的回答偏好吗"})
            second_frames = _receive_final(websocket)

        rows = second_core.sessions.store.fetch_session_messages("web:c")
        second_prompt = second_provider.pipeline_messages[-1]
        assert any(frame["type"] == "react.tool.completed" for frame in first_frames)
        assert first_frames[-1]["request_id"] == "r1"
        assert second_frames[-1]["request_id"] == "r2"
        assert [row["role"] for row in rows] == ["user", "assistant", "user", "assistant"]
        assert rows[1]["tool_chain"][0]["calls"][0]["name"] == "list_dir"
        assert any(item.get("role") == "tool" for item in second_prompt)
        assert any("用户偏好中文回答" in str(item.get("content")) for item in second_prompt)

    assert second_provider.closed is True
    assert second_embedder.closed is True
