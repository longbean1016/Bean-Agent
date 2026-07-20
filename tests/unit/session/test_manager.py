"""对齐 akashic-agent 的 Session 缓存、历史恢复与持久化测试。"""

from __future__ import annotations

import base64
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from session.manager import Session, SessionManager


@pytest_asyncio.fixture
async def manager(tmp_path: Path) -> AsyncIterator[SessionManager]:
    session_manager = SessionManager(tmp_path)
    try:
        yield session_manager
    finally:
        await session_manager.close()


@pytest.mark.asyncio
async def test_get_or_create_caches_complete_session_and_reloads_after_invalidate(
    manager: SessionManager,
) -> None:
    first = await manager.get_or_create("web:chat-1")
    user = first.add_message("user", "你好", turn_id="turn-1")
    await manager.append_messages(first, [user])

    assert user["id"] == "web:chat-1:0"
    assert (await manager.get_or_create("web:chat-1")) is first

    manager.invalidate("web:chat-1")
    reloaded = await manager.get_or_create("web:chat-1")

    assert reloaded is not first
    assert reloaded.messages == first.messages
    assert reloaded.messages[0]["turn_id"] == "turn-1"
    assert user["timestamp"].endswith("+08:00")
    assert first.created_at.utcoffset().total_seconds() == 8 * 60 * 60


@pytest.mark.asyncio
async def test_peek_next_message_id_uses_persisted_next_sequence(
    manager: SessionManager,
) -> None:
    assert await manager.peek_next_message_id("web:chat-1") == "web:chat-1:0"

    session = await manager.get_or_create("web:chat-1")
    user = session.add_message("user", "问题")
    assistant = session.add_message("assistant", "回答")
    await manager.append_messages(session, [user, assistant])

    assert await manager.peek_next_message_id("web:chat-1") == "web:chat-1:2"


def test_manager_creates_akashic_workspace_layout(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)

    assert manager.workspace == tmp_path
    assert manager.session_dir == tmp_path / "sessions"
    assert manager.session_dir.is_dir()
    assert manager.db_path == tmp_path / "sessions.db"

    import asyncio

    asyncio.run(manager.close())


@pytest.mark.asyncio
async def test_save_async_only_persists_messages_without_id(
    manager: SessionManager,
) -> None:
    session = await manager.get_or_create("web:chat-1")
    first = session.add_message("user", "第一条")
    await manager.save_async(session)
    second = session.add_message("assistant", "第二条")
    await manager.save_async(session)

    assert first["id"] == "web:chat-1:0"
    assert second["id"] == "web:chat-1:1"

    manager.invalidate("web:chat-1")
    reloaded = await manager.get_or_create("web:chat-1")
    assert [item["content"] for item in reloaded.messages] == ["第一条", "第二条"]


@pytest.mark.asyncio
async def test_append_messages_calls_store_on_event_loop_thread(
    manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = await manager.get_or_create("web:chat-1")
    message = session.add_message("user", "同步写入")
    caller_thread = threading.get_ident()
    store_threads: list[int] = []
    original = manager.store._add_message_sync

    def tracking_add_message(new_message):
        store_threads.append(threading.get_ident())
        return original(new_message)

    monkeypatch.setattr(manager.store, "_add_message_sync", tracking_add_message)
    await manager.append_messages(session, [message])

    assert store_threads == [caller_thread]


@pytest.mark.asyncio
async def test_completed_turn_persists_user_and_assistant_in_one_batch(
    manager: SessionManager,
) -> None:
    session = await manager.get_or_create("web:chat-1")

    # 对齐 akashic：Pipeline 完成后才构造本轮两条缓存消息，并一次提交。
    user = session.add_message("user", "用户问题", turn_id="turn-1")
    assistant = session.add_message(
        "assistant",
        "最终回答",
        turn_id="turn-1",
        tool_chain=[{"iteration": 1, "calls": []}],
    )
    await manager.append_messages(session, [user, assistant])

    assert user["id"] == "web:chat-1:0"
    assert assistant["id"] == "web:chat-1:1"
    manager.invalidate("web:chat-1")
    reloaded = await manager.get_or_create("web:chat-1")
    assert [message["role"] for message in reloaded.messages] == [
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_append_first_user_message_keeps_default_title_in_cache(
    manager: SessionManager,
) -> None:
    session = await manager.get_or_create("web:title-cache")
    user = session.add_message("user", "分析缓存标题", turn_id="turn-1")

    await manager.append_messages(session, [user])

    assert session.metadata["title"] == "分析缓存标题"
    persisted = manager.store.get_session_meta(session.key)
    assert persisted is not None
    assert persisted["metadata"]["title"] == "分析缓存标题"


@pytest.mark.asyncio
async def test_stale_cached_session_cannot_regress_consolidation_cursor(
    manager: SessionManager,
) -> None:
    session = await manager.get_or_create("web:chat-1")

    # Consolidation 在后台通过 Store 推进 cursor；前台缓存的 Session 仍可能保留旧值。
    manager.store.set_cursor(session.key, 12)
    message = session.add_message("user", "下一轮消息")
    await manager.append_messages(session, [message])

    assert session.last_consolidated == 0
    assert manager.store.get_cursor(session.key) == 12


def test_get_history_rebuilds_image_and_file_attachments(tmp_path: Path) -> None:
    image = tmp_path / "tiny.png"
    image.write_bytes(b"png-bytes")
    document = tmp_path / "notes.txt"
    document.write_text("hello", encoding="utf-8")
    session = Session(session_key="web:chat-1")
    session.add_message(
        "user",
        "请查看附件",
        media=[str(image), str(document), str(tmp_path / "missing.pdf")],
    )

    history = session.get_history()
    content = history[0]["content"]

    assert isinstance(content, list)
    assert content[0] == {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(b"png-bytes").decode()
        },
    }
    assert "[文本附件: notes.txt]" in content[-1]["text"]
    assert "hello" in content[-1]["text"]
    assert "[文件（已失效）: missing.pdf]" in content[-1]["text"]
    assert content[-1]["text"].endswith("请查看附件")


@pytest.mark.asyncio
async def test_attachment_paths_survive_sqlite_reload(
    manager: SessionManager,
    tmp_path: Path,
) -> None:
    image = tmp_path / "persisted.png"
    image.write_bytes(b"persisted-image")
    session = await manager.get_or_create("web:chat-1")
    message = session.add_message("user", "读取图片", media=[str(image)])
    await manager.append_messages(session, [message])

    manager.invalidate("web:chat-1")
    reloaded = await manager.get_or_create("web:chat-1")
    content = reloaded.get_history()[0]["content"]

    assert isinstance(content, list)
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_get_history_aligns_turn_and_expands_tools_with_truncation() -> None:
    session = Session(session_key="web:chat-1")
    session.add_message("user", "第一问")
    session.add_message("assistant", "第一答")
    session.add_message("user", "第二问")
    session.add_message(
        "assistant",
        "第二答",
        reasoning_content="最终思考",
        tool_chain=[
            {
                "iteration": 1,
                "text": "调用工具",
                "reasoning_content": "工具思考",
                "calls": [
                    {
                        "call_id": "call-1",
                        "name": "read_file",
                        "arguments": {"path": "large.txt"},
                        "result": "x" * 10_100,
                    }
                ],
            }
        ],
    )

    history = session.get_history(start_index=3)

    assert [item["role"] for item in history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert history[0]["content"] == "第二问"
    assert history[1]["reasoning_content"] == "工具思考"
    assert len(history[2]["content"]) <= 10_100
    assert "chars truncated" in history[2]["content"]
    assert history[3]["reasoning_content"] == "最终思考"


@pytest.mark.asyncio
async def test_load_history_uses_cursor_without_deleting_full_session(
    tmp_path: Path,
) -> None:
    manager = SessionManager(tmp_path, history_window=40)
    session = await manager.get_or_create("web:chat-1")
    for role, content in [
        ("user", "已压缩问题"),
        ("assistant", "已压缩回答"),
        ("user", "活动问题"),
        ("assistant", "活动回答"),
    ]:
        session.add_message(role, content)
    session.last_consolidated = 2

    history = await manager.load_history(session.key)

    assert [item["content"] for item in history] == ["活动问题", "活动回答"]
    assert [item["content"] for item in session.messages] == [
        "已压缩问题",
        "已压缩回答",
        "活动问题",
        "活动回答",
    ]
    await manager.close()


@pytest.mark.asyncio
async def test_load_history_does_not_align_before_misaligned_cursor(
    tmp_path: Path,
) -> None:
    manager = SessionManager(tmp_path, history_window=40)
    session = await manager.get_or_create("web:chat-1")
    for role, content in [
        ("user", "已压缩问题"),
        ("assistant", "cursor 落点"),
        ("user", "活动问题"),
        ("assistant", "活动回答"),
    ]:
        session.add_message(role, content)
    session.last_consolidated = 1

    history = await manager.load_history(session.key)

    assert [item["content"] for item in history] == ["活动问题", "活动回答"]
    await manager.close()


def test_clear_resets_cached_messages_and_cursor() -> None:
    session = Session(session_key="web:chat-1", last_consolidated=9)
    session.add_message("user", "待清理")

    session.clear()

    assert session.messages == []
    assert session.last_consolidated == 0


@pytest.mark.asyncio
async def test_close_is_idempotent(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)

    await manager.close()
    await manager.close()

    with pytest.raises(RuntimeError, match="已关闭"):
        await manager.get_or_create("web:chat-1")


@pytest.mark.asyncio
async def test_delete_removes_cached_session_without_recreating_it(
    manager: SessionManager,
) -> None:
    session = await manager.get_or_create("web:delete")
    message = session.add_message("user", "待删除")
    await manager.append_messages(session, [message])

    assert await manager.delete("web:delete") is True
    assert manager.store.get_session_meta("web:delete") is None
    assert "web:delete" not in manager._cache

    stale_message = session.add_message("assistant", "迟到的回复")
    with pytest.raises(RuntimeError, match="已删除"):
        await manager.append_messages(session, [stale_message])
    assert manager.store.get_session_meta("web:delete") is None
