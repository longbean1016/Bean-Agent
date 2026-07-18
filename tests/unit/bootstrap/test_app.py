"""应用核心依赖组装测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.config_models import Config, VisionConfig
from bootstrap.app import AppRuntime, MemoryMaintenanceLoop, build_core_runtime, create_fastapi_app
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
    assert runtime.pipeline._skills is not None
    assert runtime.sessions.store is runtime.memory._sessions
    assert "load_skill" in runtime.tools.get_registered_names()


def test_build_core_runtime_injects_image_capabilities_into_pipeline(
    tmp_path: Path,
) -> None:
    config = Config()
    config.memory.enabled = False
    config.agent.workdir = str(tmp_path / "workdir")
    config.llm.multimodal = False
    config.llm.vl = VisionConfig(
        provider="qwen",
        model="qwen-vl-max",
        api_key="test-key",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    runtime = build_core_runtime(config, tmp_path / "workspace", provider=Provider())
    try:
        assert runtime.pipeline._multimodal is False
        assert runtime.pipeline._vl_available is True
        assert "read_image_vision" in runtime.tools.get_registered_names()
    finally:
        asyncio.run(runtime.sessions.close())
        assert runtime.vision_provider is not None
        asyncio.run(runtime.vision_provider.close())


@pytest.mark.asyncio
async def test_build_core_runtime_allows_unrestricted_reads_but_keeps_writes_scoped(
    tmp_path: Path,
) -> None:
    config = Config()
    config.memory.enabled = False
    config.agent.workdir = str(tmp_path / "source")
    workspace = tmp_path / "runtime"
    outside = tmp_path / "outside" / "Main.java"
    outside.parent.mkdir(parents=True)
    outside.write_text("class Main {}", encoding="utf-8")

    runtime = build_core_runtime(config, workspace, provider=Provider())
    result = await runtime.tools.execute("read_file", {"path": str(outside)})
    rejected_write = await runtime.tools.execute(
        "write_file",
        {"path": str(tmp_path / "outside" / "created.txt"), "content": "blocked"},
    )

    assert "class Main {}" in str(result)
    assert "超出允许目录" in str(rejected_write)
    assert not (tmp_path / "outside" / "created.txt").exists()


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


@pytest.mark.asyncio
async def test_memory_maintenance_replays_outbox_and_runs_optimizer() -> None:
    class Memory:
        def __init__(self): self.replayed = 0; self.optimized = 0
        async def replay_pending_consolidations(self): self.replayed += 1
        async def optimize(self): self.optimized += 1; return {}

    memory = Memory()
    loop = MemoryMaintenanceLoop(memory, enabled=True, interval_seconds=0.01)

    await loop.start()
    await asyncio.sleep(0.03)
    await loop.close()

    assert memory.replayed == 1
    assert memory.optimized >= 1


@pytest.mark.asyncio
async def test_app_runtime_shutdown_is_ordered_and_idempotent(tmp_path: Path) -> None:
    config = Config()
    config.memory.enabled = True
    config.memory.embedding.dimensions = 2
    config.memory.optimizer.enabled = False
    config.agent.workdir = str(tmp_path / "workdir")
    provider = Provider()
    embedder = Embedder()
    core = build_core_runtime(config, tmp_path / "workspace", provider=provider, embedder=embedder)
    runtime = AppRuntime(core)

    await runtime.start()
    await runtime.shutdown()
    await runtime.shutdown()

    assert runtime.agent_task is not None and runtime.agent_task.done()
    assert provider.closed is True
    assert embedder.closed is True
    with pytest.raises(RuntimeError, match="SessionManager 已关闭"):
        await core.sessions.get_or_create("web:c")
