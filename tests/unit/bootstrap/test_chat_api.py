"""聊天页 HTTP 接口、静态托管与附件安全测试。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agent.config_models import Config
from bootstrap.app import build_core_runtime, create_fastapi_app
from bootstrap.native_folder_picker import (
    NativeHostError,
    NativeHostUnavailable,
    NativePickerBusy,
)
from session.store import NewMessage, NewSessionEvent


class Provider:
    async def chat(self, *args, **kwargs):
        raise AssertionError("HTTP 接口测试不应调用 LLM")

    async def complete(self, *args, **kwargs):
        raise AssertionError("HTTP 接口测试不应调用 LLM")

    async def close(self) -> None:
        return None


class FakeDirectoryPicker:
    def __init__(self, result: Path | None = None) -> None:
        self.result = result
        self.error: Exception | None = None
        self.opened: list[Path] = []

    def pick_directory(self) -> Path | None:
        if self.error is not None:
            raise self.error
        return self.result

    def open_directory(self, path: Path) -> None:
        if self.error is not None:
            raise self.error
        self.opened.append(path)


def _client(tmp_path: Path) -> tuple[TestClient, object]:
    config = Config()
    config.memory.enabled = False
    config.agent.workdir = str(tmp_path / "workdir")
    runtime = build_core_runtime(
        config,
        tmp_path / "workspace",
        provider=Provider(),
    )
    return TestClient(create_fastapi_app(runtime)), runtime


def _native_client(
    tmp_path: Path,
    picker: FakeDirectoryPicker,
    *,
    client_host: str = "127.0.0.1",
) -> tuple[TestClient, object]:
    config = Config()
    config.memory.enabled = False
    config.agent.workdir = str(tmp_path / "workdir")
    runtime = build_core_runtime(
        config,
        tmp_path / "workspace",
        provider=Provider(),
    )
    app = create_fastapi_app(runtime, directory_picker=picker)
    return TestClient(app, client=(client_host, 50000)), runtime


def test_native_workspace_picker_is_local_non_persisting_and_maps_host_errors(
    tmp_path: Path,
) -> None:
    project = tmp_path / "picked-project"
    project.mkdir()
    picker = FakeDirectoryPicker(project)
    client, runtime = _native_client(tmp_path, picker)

    with client:
        selected = client.post("/api/chat/workspaces/pick")
        assert selected.status_code == 200
        assert selected.json() == {"path": str(project.resolve())}
        assert runtime.sessions.store.list_workspaces() == []

        picker.result = None
        assert client.post("/api/chat/workspaces/pick").json() == {"path": None}

        for error, status in (
            (NativePickerBusy("busy"), 409),
            (NativeHostUnavailable("unavailable"), 501),
            (NativeHostError("failed"), 503),
        ):
            picker.error = error
            response = client.post("/api/chat/workspaces/pick")
            assert response.status_code == status
            assert response.json()["detail"] == str(error)


def test_native_workspace_picker_rejects_remote_client(tmp_path: Path) -> None:
    picker = FakeDirectoryPicker()
    client, _runtime = _native_client(
        tmp_path,
        picker,
        client_host="192.0.2.10",
    )

    with client:
        response = client.post("/api/chat/workspaces/pick")

    assert response.status_code == 403
    assert "回环地址" in response.json()["detail"]


def test_workspace_and_session_management_endpoints(tmp_path: Path) -> None:
    project = tmp_path / "managed-project"
    project.mkdir()
    picker = FakeDirectoryPicker()
    client, runtime = _native_client(tmp_path, picker)
    workspace = runtime.sessions.store.create_workspace(str(project), "旧名称")
    runtime.sessions.store.create_session(
        "web:managed",
        workspace_id=workspace["id"],
    )

    with client:
        updated = client.patch(
            f"/api/chat/workspaces/{workspace['id']}",
            json={"title": "新名称", "pinned": True},
        )
        pinned = client.patch(
            "/api/chat/sessions/web:managed",
            json={"pinned": True},
        )
        opened = client.post(f"/api/chat/workspaces/{workspace['id']}/open")

    assert updated.status_code == 200
    assert updated.json()["title"] == "新名称"
    assert updated.json()["pinned_at"] is not None
    assert pinned.status_code == 200
    assert pinned.json()["pinned_at"] is not None
    assert opened.status_code == 204
    assert picker.opened == [project.resolve()]


def test_chat_api_lists_sessions_and_messages(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)
    runtime.sessions.store.add_message(
        NewMessage(
            session_key="web:chat-1",
            role="user",
            content="第一问",
            extra={
                "llm_user_content": "hidden",
                "llm_context_frame": "hidden",
                "llm_surface_messages": [{"role": "user", "content": "hidden"}],
            },
        )
    )
    runtime.sessions.store.add_message(
        NewMessage(session_key="web:chat-1", role="assistant", content="第一答")
    )

    with client:
        sessions = client.get("/api/chat/sessions").json()
        messages = client.get("/api/chat/sessions/web:chat-1/messages").json()

    assert sessions["total"] == 1
    assert sessions["items"][0]["key"] == "web:chat-1"
    assert [item["content"] for item in messages["items"]] == ["第一问", "第一答"]
    assert not any(key.startswith("llm_") for key in messages["items"][0])


def test_workspace_api_and_session_sandbox_closed_loop(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    with client:
        registered = client.post(
            "/api/chat/workspaces",
            json={"path": str(project), "title": "Bean 项目"},
        )
        assert registered.status_code == 201
        workspace = registered.json()
        assert workspace["canonical_path"] == str(project.resolve())
        assert workspace["valid"] is True
        assert client.get("/api/chat/workspaces").json()["items"] == [workspace]

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({
                "type": "session.create",
                "request_id": "create-sandbox",
                "workspace_id": workspace["id"],
                "sandbox_mode": "workspace-write",
            })
            created = websocket.receive_json()
            snapshot_frame = websocket.receive_json()

        assert created["type"] == "session.created"
        assert snapshot_frame["type"] == "sandbox.updated"
        assert snapshot_frame["sandbox"]["workspace_id"] == workspace["id"]
        assert snapshot_frame["sandbox"]["sandbox_mode"] == "workspace-write"
        session_id = created["session_id"]
        assert client.get(
            f"/api/chat/sessions/{session_id}/sandbox"
        ).json()["workspace_path"] == str(project.resolve())

        removed = client.delete(f"/api/chat/workspaces/{workspace['id']}")
        assert removed.status_code == 204
        detached = client.get(
            f"/api/chat/sessions/{session_id}/sandbox"
        ).json()
        assert detached["workspace_id"] is None
        assert detached["sandbox_mode"] == "read-only"

        overlap = client.post(
            "/api/chat/workspaces",
            json={"path": str(runtime.workspace)},
        )
        assert overlap.status_code == 422
        assert "数据目录重叠" in overlap.json()["detail"]

    assert project.exists()


def test_workspace_api_refuses_to_detach_busy_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runtime = _client(tmp_path)
    project = tmp_path / "busy-project"
    project.mkdir()
    workspace = runtime.sessions.store.create_workspace(str(project))
    session_id = "web:busy-workspace"
    runtime.sessions.store.create_session(
        session_id,
        workspace_id=workspace["id"],
        sandbox_mode="workspace-write",
    )
    monkeypatch.setattr(
        runtime.agent_loop,
        "is_session_busy",
        lambda key: key == session_id,
    )

    with client:
        removed = client.delete(f"/api/chat/workspaces/{workspace['id']}")
        assert runtime.sessions.store.get_workspace(workspace["id"]) is not None
        assert (
            runtime.sessions.store.get_session_meta(session_id)["workspace_id"]
            == workspace["id"]
        )

    assert removed.status_code == 409
    assert "正在运行或排队" in removed.json()["detail"]


def test_chat_api_lists_titled_running_session_without_messages(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)
    runtime.sessions.store.ensure_default_chat_session_title(
        "web:running",
        "running title from first user message",
        [],
    )

    with client:
        sessions = client.get("/api/chat/sessions").json()

    assert sessions["total"] == 1
    assert sessions["items"][0]["key"] == "web:running"
    assert sessions["items"][0]["title"] == "running title from first user message"
    assert sessions["items"][0]["message_count"] == 0


def test_chat_api_messages_returns_latest_page_and_older_page(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)
    for index in range(65):
        runtime.sessions.store.add_message(
            NewMessage(session_key="web:paged", role="user", content=f"m-{index:02d}")
        )

    with client:
        latest = client.get("/api/chat/sessions/web:paged/messages").json()
        older = client.get(
            f"/api/chat/sessions/web:paged/messages/older?before_seq={latest['next_before_seq']}&limit=10"
        ).json()

    assert len(latest["items"]) == 60
    assert latest["items"][0]["content"] == "m-05"
    assert latest["items"][-1]["content"] == "m-64"
    assert latest["has_more"] is True
    assert latest["next_before_seq"] == 5
    assert [item["content"] for item in older["items"]] == [f"m-{index:02d}" for index in range(0, 5)]
    assert older["has_more"] is False


def test_chat_api_lists_global_user_turn_navigation(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)
    runtime.sessions.store.add_message(
        NewMessage(session_key="web:turns", role="user", content="第一问", turn_id="turn-1")
    )
    runtime.sessions.store.add_message(
        NewMessage(session_key="web:turns", role="assistant", content="第一答", turn_id="turn-1")
    )
    runtime.sessions.store.add_message(
        NewMessage(session_key="web:turns", role="assistant", content="主动消息")
    )
    runtime.sessions.store.add_message(
        NewMessage(session_key="web:turns", role="user", content="第二问", turn_id="turn-2")
    )

    with client:
        turns = client.get("/api/chat/sessions/web:turns/turns").json()

    assert [item["turn_index"] for item in turns["items"]] == [1, 2]
    assert [item["seq"] for item in turns["items"]] == [0, 3]
    assert [item["id"] for item in turns["items"]] == ["turn-1", "turn-2"]
    assert [item["preview"] for item in turns["items"]] == ["第一问", "第二问"]


def test_chat_api_exposes_persisted_turn_duration(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)
    runtime.sessions.store.add_message(
        NewMessage(session_key="web:timing", role="user", content="问题", turn_id="turn-1")
    )
    runtime.sessions.store.add_message(
        NewMessage(
            session_key="web:timing",
            role="assistant",
            content="回答",
            turn_id="turn-1",
            metadata={"duration_ms": 1750},
        )
    )
    runtime.sessions.store.append_session_event(NewSessionEvent(
        session_key="web:timing",
        event_type="turn/start",
        turn_id="turn-1",
        step=0,
        data={"started_at": "2026-09-02T10:00:00+08:00"},
        operation_key="turn-1:turn-start",
    ))
    runtime.sessions.store.append_session_event(NewSessionEvent(
        session_key="web:timing",
        event_type="turn/end",
        turn_id="turn-1",
        step=1,
        data={"ended_at": "2026-09-02T10:00:01.750000+08:00", "status": "completed"},
        operation_key="turn-1:turn-end",
    ))

    with client:
        messages = client.get("/api/chat/sessions/web:timing/messages").json()
        turns = client.get("/api/chat/sessions/web:timing/turns").json()

    assert messages["items"][1]["metadata"]["duration_ms"] == 1750
    assert turns["items"][0]["duration_ms"] == 1750
    assert turns["items"][0]["ended_at"] == "2026-09-02T10:00:01.750000+08:00"


def test_chat_api_messages_around_returns_window_from_anchor(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)
    for index in range(70):
        runtime.sessions.store.add_message(
            NewMessage(session_key="web:around", role="user", content=f"m-{index:02d}")
        )

    with client:
        page = client.get("/api/chat/sessions/web:around/messages/around?anchor_seq=8").json()

    assert len(page["items"]) == 60
    assert page["items"][0]["seq"] == 8
    assert page["items"][-1]["seq"] == 67
    assert page["has_before"] is True
    assert page["has_after"] is True
    assert page["next_before_seq"] == 8


def test_chat_api_messages_appends_running_snapshot_without_persisting(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)
    runtime.sessions.store.create_session("web:running")

    def snapshot(session_key: str) -> dict[str, object] | None:
        if session_key != "web:running":
            return None
        return {
            "session_id": "web:running",
            "turn_id": "turn-live",
            "request_id": "request-live",
            "user_message": "live question",
            "user_media": [],
            "content": "partial answer",
            "thinking": "partial thinking",
            "tools": [{
                "call_id": "call-1",
                "name": "read_file",
                "status": "running",
                "arguments": {"path": "README.md"},
                "result_preview": "",
            }],
            "status": "running",
        }

    runtime.agent_loop.get_active_turn_snapshot = snapshot  # type: ignore[method-assign]

    with client:
        messages = client.get("/api/chat/sessions/web:running/messages").json()
        persisted = runtime.sessions.store.fetch_session_messages("web:running")

    assert [item["role"] for item in messages["items"]] == ["user", "assistant"]
    assert messages["items"][0]["id"] == "running:user:turn-live"
    assert messages["items"][1]["id"] == "running:assistant:turn-live"
    assert messages["items"][1]["content"] == "partial answer"
    assert messages["items"][1]["reasoning_content"] == "partial thinking"
    assert messages["items"][1]["metadata"]["running"] is True
    assert persisted == []


def test_chat_api_renames_session_and_validates_title(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)
    runtime.sessions.store.add_message(
        NewMessage(session_key="web:rename", role="user", content="原始问题")
    )

    with client:
        renamed = client.patch(
            "/api/chat/sessions/web:rename",
            json={"title": "  新标题  "},
        )
        empty = client.patch("/api/chat/sessions/web:rename", json={"title": "   "})
        too_long = client.patch("/api/chat/sessions/web:rename", json={"title": "长" * 61})
        missing = client.patch("/api/chat/sessions/web:missing", json={"title": "标题"})

    assert renamed.status_code == 200
    assert renamed.json()["title"] == "新标题"
    assert renamed.json()["updated_at"]
    assert empty.status_code == 400
    assert too_long.status_code == 400
    assert missing.status_code == 404


def test_chat_api_deletes_session_without_touching_workspace_memory(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)
    runtime.sessions.store.add_message(
        NewMessage(session_key="web:delete", role="user", content="待删除")
    )
    memory_file = runtime.workspace / "MEMORY.md"
    memory_file.write_text("长期记忆保留", encoding="utf-8")

    with client:
        deleted = client.delete("/api/chat/sessions/web:delete")
        repeated = client.delete("/api/chat/sessions/web:delete")
        assert runtime.sessions.store.get_session_meta("web:delete") is None

    assert deleted.status_code == 204
    assert repeated.status_code == 404
    assert memory_file.read_text(encoding="utf-8") == "长期记忆保留"


def test_upload_accepts_utf8_text_and_serves_only_workspace_media(
    tmp_path: Path,
) -> None:
    client, runtime = _client(tmp_path)
    with client:
        response = client.post(
            "/api/chat/uploads?filename=notes.txt",
            content="测试文本".encode(),
            headers={"content-type": "text/plain; charset=utf-8"},
        )
        assert response.status_code == 200
        payload = response.json()
        stored = Path(payload["upload_path"])
        assert stored.is_relative_to(runtime.workspace / "uploads")
        assert stored.name == "notes.txt"
        assert stored.read_text(encoding="utf-8") == "测试文本"
        assert client.get(payload["upload_url"]).content == "测试文本".encode()
        assert client.get(
            "/api/chat/media", params={"path": str(tmp_path / "outside.txt")}
        ).status_code == 404


@pytest.mark.parametrize(
    "filename",
    [
        "README.rst", "guide.adoc", "paper.tex",
        "Main.java", "main.c", "types.hpp", "Program.cs", "main.go", "lib.rs",
        "index.php", "task.rb", "App.swift", "build.kt", "build.kts", "Main.scala",
        "init.lua", "deploy.sh", "profile.bash", "env.zsh", "setup.ps1", "run.bat",
        "run.cmd", "query.sql", "analysis.r", "App.vue", "Page.svelte",
        "app.ini", "server.conf", "tool.cfg", "messages.properties",
        "events.ndjson", "records.jsonl", "data.tsv", "schema.graphql", "query.gql",
        "image.dockerfile",
    ],
)
def test_upload_preserves_supported_utf8_source_and_config_suffixes(
    tmp_path: Path,
    filename: str,
) -> None:
    client, _runtime = _client(tmp_path)

    with client:
        response = client.post(
            f"/api/chat/uploads?filename={filename}",
            content=b"example content\n",
            headers={"content-type": "application/octet-stream"},
        )

    assert response.status_code == 200
    assert response.json()["filename"] == filename


def test_upload_validates_image_content_and_rejects_binary(tmp_path: Path) -> None:
    client, _runtime = _client(tmp_path)
    image = BytesIO()
    Image.new("RGB", (4, 4), "#0f766e").save(image, format="PNG")

    with client:
        valid = client.post(
            "/api/chat/uploads?filename=sample.png",
            content=image.getvalue(),
            headers={"content-type": "image/png"},
        )
        fake = client.post(
            "/api/chat/uploads?filename=fake.png",
            content=b"not-an-image",
            headers={"content-type": "image/png"},
        )
        binary = client.post(
            "/api/chat/uploads?filename=archive.zip",
            content=b"PK\x03\x04",
            headers={"content-type": "application/zip"},
        )

    assert valid.status_code == 200
    assert fake.status_code == 400
    assert binary.status_code == 415
