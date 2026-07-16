"""应用核心依赖组装测试。"""

from __future__ import annotations

from pathlib import Path

from agent.config_models import Config
from bootstrap.app import build_core_runtime


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
