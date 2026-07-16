"""应用核心依赖组装测试。"""

from __future__ import annotations

from pathlib import Path

from agent.config_models import Config
from bootstrap.app import build_core_runtime, create_fastapi_app
from fastapi.testclient import TestClient


class Provider:
    async def chat(self, *args, **kwargs): raise AssertionError("组装不应调用 API")
    async def complete(self, *args, **kwargs): raise AssertionError("组装不应调用 API")
    async def close(self): self.closed = True


class Embedder:
    async def embed(self, text): return [1.0, 0.0]
    async def embed_batch(self, texts): return [[1.0, 0.0] for _ in texts]
    async def close(self): self.closed = True


def test_build_core_runtime_wires_singletons_and_all_tools(tmp_path: Path) -> None:
    config = Config()
    config.memory.enabled = True
    config.memory.embedding.dimensions = 2
    config.agent.workdir = str(tmp_path / "workdir")
    provider = Provider()
    embedder = Embedder()

    runtime = build_core_runtime(config, tmp_path / "workspace", provider=provider, embedder=embedder)

    assert runtime.provider is provider
    assert runtime.embedder is embedder
    assert runtime.memory is not None
    assert runtime.agent_loop is not None
    assert runtime.pipeline is not None
    assert runtime.sessions.store is runtime.memory._sessions
    assert len(runtime.tools.get_registered_names()) == 12


def test_fastapi_exposes_real_websocket_route(tmp_path: Path) -> None:
    config = Config()
    config.memory.enabled = False
    config.agent.workdir = str(tmp_path / "workdir")
    runtime = build_core_runtime(config, tmp_path / "workspace", provider=Provider())
    app = create_fastapi_app(runtime)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "ping", "request_id": "r1"})
            assert websocket.receive_json() == {"type": "pong", "request_id": "r1"}
            websocket.send_json({"type": "session.create", "request_id": "r2"})
            created = websocket.receive_json()
            assert created["type"] == "session.created"
            assert created["session_id"].startswith("web:")
