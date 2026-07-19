"""聊天页 HTTP 接口、静态托管与附件安全测试。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agent.config_models import Config
from bootstrap.app import build_core_runtime, create_fastapi_app
from session.store import NewMessage


class Provider:
    async def chat(self, *args, **kwargs):
        raise AssertionError("HTTP 接口测试不应调用 LLM")

    async def complete(self, *args, **kwargs):
        raise AssertionError("HTTP 接口测试不应调用 LLM")

    async def close(self) -> None:
        return None


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


def test_chat_api_lists_sessions_and_messages(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)
    runtime.sessions.store.add_message(
        NewMessage(session_key="web:chat-1", role="user", content="第一问")
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
