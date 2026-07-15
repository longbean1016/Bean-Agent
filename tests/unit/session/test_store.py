"""SessionStore 的 SQLite 持久化与历史转换测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from session.store import NewMessage, SessionStore


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    """每个测试使用独立数据库，并确保连接在结束时关闭。"""

    session_store = SessionStore(tmp_path / "sessions.db")
    try:
        yield session_store
    finally:
        session_store.close()


@pytest.mark.asyncio
async def test_create_session_is_idempotent(store: SessionStore) -> None:
    first = store.create_session("web:chat-1")
    second = store.create_session("web:chat-1")

    assert first["key"] == "web:chat-1"
    assert first["created_at"] == second["created_at"]
    assert second["last_consolidated"] == 0
    assert second["next_seq"] == 0


@pytest.mark.asyncio
async def test_add_message_allocates_seq_and_preserves_turn_fields(
    store: SessionStore,
) -> None:
    first = store.add_message(
        NewMessage(
            session_key="web:chat-1",
            role="user",
            content="你好",
            turn_id="turn-1",
            metadata={"request_id": "request-1"},
        )
    )
    second = store.add_message(
        NewMessage(
            session_key="web:chat-1",
            role="assistant",
            content="你好，有什么可以帮你？",
            turn_id="turn-1",
            reasoning_content="先礼貌回应",
            status="ok",
        )
    )

    assert first["id"] == "web:chat-1:0"
    assert second["id"] == "web:chat-1:1"
    assert second["seq"] == 1
    assert second["reasoning_content"] == "先礼貌回应"
    assert first["metadata"] == {"request_id": "request-1"}

    fetched = store.fetch_messages(
        "web:chat-1", [first["id"], second["id"]]
    )
    assert [item["seq"] for item in fetched] == [0, 1]
    assert fetched[1]["turn_id"] == "turn-1"


@pytest.mark.asyncio
async def test_concurrent_add_message_keeps_unique_contiguous_seq(
    store: SessionStore,
) -> None:
    store.create_session("web:concurrent")

    rows = await asyncio.gather(
        *[
            asyncio.to_thread(
                store.add_message,
                NewMessage(
                    session_key="web:concurrent",
                    role="user",
                    content=f"消息 {index}",
                ),
            )
            for index in range(20)
        ]
    )

    assert sorted(row["seq"] for row in rows) == list(range(20))


@pytest.mark.asyncio
async def test_load_history_expands_tool_chain_and_reasoning(
    store: SessionStore,
) -> None:
    store.add_message(
        NewMessage(session_key="web:chat-1", role="user", content="查询天气")
    )
    store.add_message(
        NewMessage(
            session_key="web:chat-1",
            role="assistant",
            content="上海今天有雨。",
            reasoning_content="根据工具结果回答",
            tool_chain=[
                {
                    "iteration": 1,
                    "text": "",
                    "provider_fields": {"reasoning_content": "先查询天气"},
                    "calls": [
                        {
                            "call_id": "call-1",
                            "name": "weather",
                            "arguments": {"city": "上海"},
                            "result": "有雨，26 度",
                            "status": "ok",
                        }
                    ],
                }
            ],
        )
    )

    history = store.load_history("web:chat-1", limit=40)

    assert history == [
        {"role": "user", "content": "查询天气"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "weather",
                        "arguments": '{"city": "上海"}',
                    },
                }
            ],
            "reasoning_content": "先查询天气",
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "有雨，26 度"},
        {
            "role": "assistant",
            "content": "上海今天有雨。",
            "reasoning_content": "根据工具结果回答",
        },
    ]


@pytest.mark.asyncio
async def test_load_history_limit_expands_to_complete_user_turn(
    store: SessionStore,
) -> None:
    store.add_message(
        NewMessage(session_key="web:chat-1", role="user", content="旧消息")
    )
    store.add_message(
        NewMessage(session_key="web:chat-1", role="assistant", content="新回复")
    )

    history = store.load_history("web:chat-1", limit=1)

    assert history == [
        {"role": "user", "content": "旧消息"},
        {"role": "assistant", "content": "新回复"},
    ]


@pytest.mark.asyncio
async def test_load_history_aligns_only_to_nearest_user_boundary(
    store: SessionStore,
) -> None:
    for role, content in [
        ("user", "第一问"),
        ("assistant", "第一答"),
        ("user", "第二问"),
        ("assistant", "第二答"),
    ]:
        store.add_message(
            NewMessage(session_key="web:chat-1", role=role, content=content)
        )

    history = store.load_history("web:chat-1", limit=1)

    assert history == [
        {"role": "user", "content": "第二问"},
        {"role": "assistant", "content": "第二答"},
    ]


@pytest.mark.asyncio
async def test_fetch_messages_expands_context_and_marks_source(
    store: SessionStore,
) -> None:
    rows = []
    for index in range(5):
        rows.append(
            store.add_message(
                NewMessage(
                    session_key="web:chat-1",
                    role="user",
                    content=f"消息 {index}",
                )
            )
        )
    store.add_message(
        NewMessage(session_key="web:other", role="user", content="其他会话")
    )

    fetched = store.fetch_messages(
        "web:chat-1", [rows[2]["id"]], context=1
    )

    assert [item["seq"] for item in fetched] == [1, 2, 3]
    assert [item["in_source_ref"] for item in fetched] == [False, True, False]
    assert all(item["session_key"] == "web:chat-1" for item in fetched)


@pytest.mark.asyncio
async def test_search_messages_is_scoped_and_limited(store: SessionStore) -> None:
    store.add_message(
        NewMessage(session_key="web:chat-1", role="user", content="今天学习 Python")
    )
    store.add_message(
        NewMessage(session_key="web:chat-1", role="assistant", content="Python 很适合入门")
    )
    store.add_message(
        NewMessage(session_key="web:other", role="assistant", content="Python 其他会话")
    )

    results = store.search_messages("web:chat-1", "Python", limit=1)

    assert len(results) == 1
    assert results[0]["session_key"] == "web:chat-1"
    assert "Python" in results[0]["content"]


@pytest.mark.asyncio
async def test_cursor_defaults_to_zero_and_can_advance(store: SessionStore) -> None:
    assert store.get_cursor("web:chat-1") == 0

    store.create_session("web:chat-1")
    store.set_cursor("web:chat-1", 12)

    assert store.get_cursor("web:chat-1") == 12


@pytest.mark.asyncio
async def test_add_message_rejects_invalid_role(store: SessionStore) -> None:
    with pytest.raises(ValueError, match="role"):
        store.add_message(
            NewMessage(session_key="web:chat-1", role="system", content="非法")
        )


@pytest.mark.asyncio
async def test_reopen_keeps_messages_and_close_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    first = SessionStore(db_path)
    first.add_message(
        NewMessage(session_key="web:chat-1", role="user", content="持久化消息")
    )
    first.close()
    first.close()

    second = SessionStore(db_path)
    try:
        history = second.load_history("web:chat-1")
        added = second.add_message(
            NewMessage(session_key="web:chat-1", role="assistant", content="继续")
        )
    finally:
        second.close()

    assert history == [{"role": "user", "content": "持久化消息"}]
    assert added["seq"] == 1
