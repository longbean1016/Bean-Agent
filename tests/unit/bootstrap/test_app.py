"""应用核心依赖组装测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.config_models import Config, VisionConfig
from agent.skills import seed_builtin_skills
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

    assert runtime.memory is not None
    assert not hasattr(runtime.pipeline, "_history_limit")
    assert not hasattr(runtime.memory, "_consolidator")

    assert runtime.provider is provider
    assert runtime.embedder is embedder
    assert runtime.memory is not None
    assert runtime.agent_loop is not None
    assert runtime.pipeline is not None
    assert runtime.pipeline._skills is not None
    assert runtime.pipeline._prompt_cache_log.log_dir == (
        tmp_path / "workspace" / "logs" / "prompt-cache"
    ).resolve()
    assert runtime.sessions.store is runtime.memory._sessions
    assert "load_skill" in runtime.tools.get_registered_names()
    assert {
        "schedule_reminder", "schedule_task", "list_schedules", "cancel_schedule",
    } <= runtime.tools.get_registered_names()
    assert "schedule" not in runtime.tools.get_registered_names()
    assert runtime.mcp_registry is not None
    assert {"mcp_add", "mcp_remove", "mcp_list"} <= runtime.tools.get_registered_names()
    assert runtime.tools.get_metadata("mcp_add").always_on is True


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
    project = tmp_path / "source"
    project.mkdir()
    registered = runtime.sessions.store.create_workspace(str(project))
    await runtime.sessions.get_or_create(
        "web:scoped",
        workspace_id=str(registered["id"]),
        sandbox_mode="workspace-write",
    )
    context = {"session_key": "web:scoped", "turn_id": "turn-1", "call_id": "call-1"}
    try:
        result = await runtime.tools.execute(
            "read_file",
            {"path": str(outside)},
            context=context,
        )
        rejected_write = await runtime.tools.execute(
            "write_file",
            {"path": str(tmp_path / "outside" / "created.txt"), "content": "blocked"},
            context=context,
        )

        assert "class Main {}" in str(result)
        assert "审批界面" in str(rejected_write)
        assert not (tmp_path / "outside" / "created.txt").exists()
    finally:
        await runtime.sandbox_runtime.close()
        await runtime.sessions.close()


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
            session_key = created["session_id"]

            # session.created 返回前必须已经落库，否则前端紧接着加载通知会得到 404。
            notifications = client.get(
                f"/api/chat/sessions/{session_key}/notifications"
            )
            assert notifications.status_code == 200
            assert notifications.json()["items"] == []
            assert runtime.sessions.store.get_session_meta(session_key)["next_seq"] == 0


def test_mcp_api_keeps_user_and_workspace_scopes_isolated(tmp_path: Path) -> None:
    config = Config()
    config.memory.enabled = False
    workspace = tmp_path / "workspace"
    user_path = tmp_path / "user" / "mcp_servers.json"
    runtime = build_core_runtime(
        config,
        workspace,
        provider=Provider(),
        user_mcp_path=user_path,
    )
    # 停用配置用于验证列表隔离，不会在测试启动时创建外部进程。
    import json

    (workspace / "mcp_servers.json").write_text(
        json.dumps({"servers": {"workspace_server": {"type": "stdio", "command": ["x"], "enabled": False}}}),
        encoding="utf-8",
    )
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text(
        json.dumps({"servers": {"user_server": {"type": "stdio", "command": ["x"], "enabled": False}}}),
        encoding="utf-8",
    )
    with TestClient(create_fastapi_app(runtime)) as client:
        workspace_items = client.get("/api/extensions/mcp?scope=workspace").json()["items"]
        user_items = client.get("/api/extensions/mcp?scope=user").json()["items"]

    assert [item["id"] for item in workspace_items] == ["workspace_server"]
    assert [item["scope"] for item in workspace_items] == ["workspace"]
    assert [item["id"] for item in user_items] == ["user_server"]
    assert [item["scope"] for item in user_items] == ["user"]


def test_mcp_api_rejects_stale_revision_without_mutating_config(tmp_path: Path) -> None:
    config = Config()
    config.memory.enabled = False
    workspace = tmp_path / "workspace"
    runtime = build_core_runtime(config, workspace, provider=Provider(), user_mcp_path=tmp_path / "user.json")
    (workspace / "mcp_servers.json").write_text(
        json.dumps({"servers": {"demo": {"type": "stdio", "command": ["x"], "enabled": False}}}),
        encoding="utf-8",
    )
    with TestClient(create_fastapi_app(runtime)) as client:
        first = client.get("/api/extensions/mcp/demo?scope=workspace")
        assert first.status_code == 200
        revision = first.json()["revision"]
        updated = client.put(
            "/api/extensions/mcp/demo",
            json={"scope": "workspace", "revision": revision, "type": "stdio", "command": ["x"], "enabled": False},
        )
        assert updated.status_code == 200
        stale = client.put(
            "/api/extensions/mcp/demo",
            json={"scope": "workspace", "revision": revision, "type": "stdio", "command": ["y"], "enabled": False},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "revision_conflict"
        current = client.get("/api/extensions/mcp/demo?scope=workspace").json()
        assert current["command"] == "x"


def test_mcp_external_discovery_returns_safe_summary_and_imports_selected(tmp_path: Path) -> None:
    config = Config()
    config.memory.enabled = False
    workspace = tmp_path / "workspace"
    source_path = workspace / ".vscode" / "mcp.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        json.dumps({"mcpServers": {"external": {"type": "stdio", "command": ["x"], "env": {"TOKEN": "secret"}, "enabled": False}}}),
        encoding="utf-8",
    )
    runtime = build_core_runtime(config, workspace, provider=Provider(), user_mcp_path=tmp_path / "user.json")
    with TestClient(create_fastapi_app(runtime)) as client:
        discovered = client.get("/api/extensions/mcp/discover")
        assert discovered.status_code == 200
        source = next(item for item in discovered.json()["sources"] if item["scope"] == "project")
        discovery_text = json.dumps(discovered.json())
        assert "TOKEN" not in discovery_text and '"secret"' not in discovery_text
        imported = client.post(
            "/api/extensions/mcp/import",
            json={"scope": "workspace", "selections": [{"source_id": source["id"], "names": ["external"]}]},
        )
        assert imported.status_code == 200
        assert imported.json()["success_count"] == 1
        record = client.get("/api/extensions/mcp/external?scope=workspace").json()
        assert record["env_names"] == ["TOKEN"]


def test_skill_batch_import_requires_fresh_preflight_token(tmp_path: Path) -> None:
    config = Config()
    config.memory.enabled = False
    workspace = tmp_path / "workspace"
    user_skills = tmp_path / "user-skills"
    source = user_skills / "review" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\nname: review\ndescription: 审查\n---\n旧正文\n", encoding="utf-8")
    runtime = build_core_runtime(config, workspace, provider=Provider())
    runtime.skills.user_skills_dir = user_skills.resolve()
    runtime.skills._state_paths = [workspace / ".beanagent" / "skills-state.json", user_skills.parent / "skills-state.json"]

    with TestClient(create_fastapi_app(runtime)) as client:
        discovered = client.get("/api/extensions/skills/discover").json()["sources"]
        source_group = next(item for item in discovered if item["agent"] == "beanagent-user")
        payload = {
            "scope": "workspace",
            "mode": "copy",
            "selections": [{"source_id": source_group["id"], "names": ["review"]}],
        }
        preflight = client.post("/api/extensions/skills/import/preflight", json=payload)
        assert preflight.status_code == 200
        assert preflight.json()["items"][0]["status"] == "ready"

        source.write_text("---\nname: review\ndescription: 审查\n---\n新正文\n", encoding="utf-8")
        stale = client.post("/api/extensions/skills/import", json={**payload, "preflight_token": preflight.json()["token"]})
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "preflight_stale"

        fresh = client.post("/api/extensions/skills/import/preflight", json=payload).json()
        imported = client.post("/api/extensions/skills/import", json={**payload, "preflight_token": fresh["token"]})
        assert imported.status_code == 200
        assert imported.json()["success_count"] == 1
        assert "新正文" in (workspace / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8")


def test_default_skill_api_supports_diff_update_restore_and_conflict(tmp_path: Path) -> None:
    config = Config()
    config.memory.enabled = False
    workspace = tmp_path / "workspace"
    builtin = tmp_path / "builtin"
    user_skills = tmp_path / "user-skills"
    source = builtin / "weather" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\nname: weather\ndescription: 版本一\n---\n正文一\n", encoding="utf-8")
    seed_builtin_skills(user_skills, builtin_skills_dir=builtin)
    runtime = build_core_runtime(config, workspace, provider=Provider())
    runtime.skills.builtin_skills_dir = builtin.resolve()
    runtime.skills.user_skills_dir = user_skills.resolve()
    runtime.skills._state_paths = [workspace / ".beanagent" / "skills-state.json", user_skills.parent / "skills-state.json"]

    with TestClient(create_fastapi_app(runtime)) as client:
        current = client.get("/api/extensions/skills/defaults")
        assert current.status_code == 200
        assert current.json()["items"][0]["status"] == "current"

        source.write_text("---\nname: weather\ndescription: 版本二\n---\n正文二\n", encoding="utf-8")
        available = client.get("/api/extensions/skills/defaults").json()["items"][0]
        assert available["status"] == "update_available"
        diff = client.get("/api/extensions/skills/defaults/weather/diff")
        assert diff.status_code == 200
        assert "+description: 版本二" in diff.json()["diff"]
        updated = client.post(
            "/api/extensions/skills/defaults/weather/update",
            json={"expected_hash": available["user_hash"]},
        )
        assert updated.status_code == 200
        assert updated.json()["item"]["status"] == "current"

        target = user_skills / "weather" / "SKILL.md"
        target.write_text("---\nname: weather\ndescription: 用户版本\n---\n用户正文\n", encoding="utf-8")
        modified = client.get("/api/extensions/skills/defaults").json()["items"][0]
        protected = client.post(
            "/api/extensions/skills/defaults/weather/update",
            json={"expected_hash": modified["user_hash"]},
        )
        assert protected.status_code == 409
        assert protected.json()["detail"]["code"] == "user_modified"

        target.write_text("---\nname: weather\ndescription: 又一次修改\n---\n正文\n", encoding="utf-8")
        stale = client.post(
            "/api/extensions/skills/defaults/weather/restore",
            json={"expected_hash": modified["user_hash"]},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "seed_conflict"

        latest = client.get("/api/extensions/skills/defaults").json()["items"][0]
        restored = client.post(
            "/api/extensions/skills/defaults/weather/restore",
            json={"expected_hash": latest["user_hash"]},
        )
        assert restored.status_code == 200
        assert restored.json()["item"]["status"] == "current"
        assert "正文二" in target.read_text(encoding="utf-8")


def test_chat_session_route_returns_spa_index_or_build_hint(tmp_path: Path) -> None:
    config = Config()
    config.memory.enabled = False
    runtime = build_core_runtime(config, tmp_path / "workspace", provider=Provider())
    app = create_fastapi_app(runtime)

    with TestClient(app) as client:
        response = client.get("/chat/example-session")

    assert response.status_code == 200
    assert response.headers["content-type"].split(";", 1)[0] in {
        "text/html", "application/json"
    }


@pytest.mark.asyncio
async def test_memory_maintenance_replays_outbox_and_runs_optimizer() -> None:
    class Memory:
        def __init__(self): self.replayed = 0; self.optimized = 0
        async def replay_pending_consolidations(self): self.replayed += 1
        async def optimize(self): self.optimized += 1; return {}

    memory = Memory()
    loop = MemoryMaintenanceLoop(memory, enabled=True, interval_seconds=60)
    loop._seconds_until_next_tick = lambda: 0.01

    await loop.start()
    await asyncio.sleep(0.03)
    await loop.close()

    assert memory.replayed == 1
    assert memory.optimized >= 1


def test_memory_maintenance_aligns_next_run_to_absolute_time_boundary() -> None:
    now = datetime.fromtimestamp(100, tz=timezone.utc)
    loop = MemoryMaintenanceLoop(
        object(),
        enabled=True,
        interval_seconds=60,
        now_fn=lambda: now,
    )

    assert loop._seconds_until_next_tick() == 20


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
    lifecycle: list[str] = []
    original_load = core.mcp_registry.load_and_connect_all
    original_shutdown = core.mcp_registry.shutdown

    async def load_mcp() -> None:
        lifecycle.append("mcp.load")
        await original_load()

    async def shutdown_mcp() -> None:
        lifecycle.append("mcp.shutdown")
        await original_shutdown()

    core.mcp_registry.load_and_connect_all = load_mcp  # type: ignore[method-assign]
    core.mcp_registry.shutdown = shutdown_mcp  # type: ignore[method-assign]

    await runtime.start()
    await runtime.shutdown()
    await runtime.shutdown()

    assert runtime.agent_task is not None and runtime.agent_task.done()
    assert lifecycle == ["mcp.load", "mcp.shutdown"]
    assert provider.closed is True
    assert embedder.closed is True
    with pytest.raises(RuntimeError, match="SessionManager 已关闭"):
        await core.sessions.get_or_create("web:c")
