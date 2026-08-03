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
    assert first["created_at"].endswith("+08:00")


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
    assert first["timestamp"].endswith("+08:00")

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
async def test_load_history_prefers_final_reasoning_content_over_concatenated(
    store: SessionStore,
) -> None:
    """新数据终答 assistant 的 reasoning_content 应来自 final_reasoning_content，不含工具思考。"""

    store.add_message(
        NewMessage(session_key="web:chat-1", role="user", content="查询天气")
    )
    store.add_message(
        NewMessage(
            session_key="web:chat-1",
            role="assistant",
            content="上海今天有雨。",
            # reasoning_content 仍是整个 Turn 的拼接版，供前端回看完整链路使用。
            reasoning_content="先查询天气\n\n根据工具结果回答",
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
            # 终答轮单独思考；写入 extra 后由 _row_to_message 自动挂回 message 顶层。
            extra={"final_reasoning_content": "根据工具结果回答"},
        )
    )

    history = store.load_history("web:chat-1", limit=40)

    # 工具轮 assistant 自带 tool_chain.provider_fields.reasoning_content
    # 终答 assistant 的 reasoning_content 应该只来自 final_reasoning_content，
    # 不再包含拼接版里的"先查询天气"，避免历史里工具决策思考重复占 token。
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
async def test_load_history_falls_back_to_reasoning_content_without_final_reasoning(
    store: SessionStore,
) -> None:
    """旧数据没有 final_reasoning_content 时，终答 assistant 回退使用 reasoning_content。

    落库前缺乏 ``extra.final_reasoning_content`` 的历史数据（改造前已落库的消息）
    不需要迁移：fallback 路径保留原有行为，DeepSeek 协议依然合规。
    """

    store.add_message(
        NewMessage(session_key="web:chat-1", role="user", content="查询天气")
    )
    store.add_message(
        NewMessage(
            session_key="web:chat-1",
            role="assistant",
            content="上海今天有雨。",
            # 模拟旧数据：reasoning_content 是拼接版，extra 中没有 final_reasoning_content。
            reasoning_content="先查询天气\n\n根据工具结果回答",
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

    # 终答 assistant 的 reasoning_content 退回到拼接版（与改造前行为完全一致）。
    assert history[-1] == {
        "role": "assistant",
        "content": "上海今天有雨。",
        "reasoning_content": "先查询天气\n\n根据工具结果回答",
    }


@pytest.mark.asyncio
async def test_load_history_keeps_finished_tools_from_interrupted_turn(
    store: SessionStore,
) -> None:
    store.add_message(NewMessage(session_key="web:chat-1", role="user", content="question"))
    store.add_message(NewMessage(
        session_key="web:chat-1",
        role="assistant",
        content="[用户已停止生成]",
        status="interrupted",
        tool_chain=[{
            "text": "",
            "provider_fields": {"reasoning_content": "先读取文件"},
            "calls": [
                {
                    "call_id": "call-ok", "name": "read_file", "status": "ok",
                    "arguments": {"path": "a.txt"}, "result": "文件内容",
                },
                {
                    "call_id": "call-error", "name": "shell", "status": "error",
                    "arguments": {"command": "bad"}, "result": "命令失败",
                },
                {
                    "call_id": "call-running", "name": "search", "status": "interrupted",
                    "arguments": {"query": "weather"}, "result": "partial",
                },
            ],
        }],
        extra={
            "interrupted_display_content": "partial reply",
            "interrupted_display_reasoning": "partial thinking",
        },
    ))

    assert store.load_history("web:chat-1", limit=40) == [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-ok", "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
                },
                {
                    "id": "call-error", "type": "function",
                    "function": {"name": "shell", "arguments": '{"command": "bad"}'},
                },
            ],
            "reasoning_content": "先读取文件",
        },
        {"role": "tool", "tool_call_id": "call-ok", "content": "文件内容"},
        {"role": "tool", "tool_call_id": "call-error", "content": "命令失败"},
        {"role": "assistant", "content": "[用户已停止生成]"},
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


def test_load_history_starts_after_consolidation_cursor(
    store: SessionStore,
) -> None:
    for role, content in [
        ("user", "已压缩问题"),
        ("assistant", "已压缩回答"),
        ("user", "活动问题"),
        ("assistant", "活动回答"),
    ]:
        store.add_message(
            NewMessage(session_key="web:chat-1", role=role, content=content)
        )
    store.set_cursor("web:chat-1", 2)

    history = store.load_history("web:chat-1", limit=40)

    assert history == [
        {"role": "user", "content": "活动问题"},
        {"role": "assistant", "content": "活动回答"},
    ]


def test_load_history_never_aligns_before_consolidation_cursor(
    store: SessionStore,
) -> None:
    for role, content in [
        ("user", "已压缩问题"),
        ("assistant", "cursor 落点"),
        ("user", "活动问题"),
        ("assistant", "活动回答"),
    ]:
        store.add_message(
            NewMessage(session_key="web:chat-1", role=role, content=content)
        )
    store.set_cursor("web:chat-1", 1)

    history = store.load_history("web:chat-1", limit=40)

    assert history == [
        {"role": "user", "content": "活动问题"},
        {"role": "assistant", "content": "活动回答"},
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
async def test_fetch_by_ids_preserves_requested_order_across_sessions(
    store: SessionStore,
) -> None:
    first = store.add_message(
        NewMessage(session_key="web:first", role="user", content="第一条")
    )
    second = store.add_message(
        NewMessage(session_key="web:second", role="assistant", content="第二条")
    )

    fetched = store.fetch_by_ids([second["id"], first["id"]])

    assert [message["id"] for message in fetched] == [second["id"], first["id"]]


@pytest.mark.asyncio
async def test_search_messages_supports_role_filter_and_pagination(
    store: SessionStore,
) -> None:
    for role, content in [
        ("user", "Python asyncio 入门"),
        ("assistant", "Python asyncio 示例"),
        ("assistant", "Python 进阶"),
    ]:
        store.add_message(
            NewMessage(session_key="web:chat-1", role=role, content=content)
        )

    messages, total = store.search_messages(
        "Python asyncio",
        session_key="web:chat-1",
        role="assistant",
        limit=1,
        offset=0,
    )

    assert total == 2
    assert len(messages) == 1
    assert messages[0]["content"] == "Python asyncio 示例"


@pytest.mark.asyncio
async def test_cursor_defaults_to_zero_and_can_advance(store: SessionStore) -> None:
    assert store.get_cursor("web:chat-1") == 0

    store.create_session("web:chat-1")
    store.set_cursor("web:chat-1", 12)

    assert store.get_cursor("web:chat-1") == 12

    store.set_cursor("web:chat-1", 0)

    assert store.get_cursor("web:chat-1") == 0


def test_list_chat_sessions_uses_first_user_time_for_display_and_order(
    store: SessionStore,
) -> None:
    store.create_session("web:empty")
    store.add_message(
        NewMessage(
            session_key="web:older",
            role="user",
            content="较早的问题",
            timestamp="2026-07-18T10:00:00+08:00",
        )
    )
    store.add_message(
        NewMessage(
            session_key="web:newer",
            role="user",
            content="较晚创建的问题",
            timestamp="2026-07-18T11:00:00+08:00",
        )
    )
    store.add_message(
        NewMessage(
            session_key="web:older",
            role="assistant",
            content="后来才追加的回答",
            timestamp="2026-07-18T12:00:00+08:00",
        )
    )

    items, total = store.list_chat_sessions(channel="web", limit=20, offset=0)

    assert total == 2
    assert [item["key"] for item in items] == ["web:newer", "web:older"]
    assert items[0]["created_at"] == "2026-07-18T11:00:00+08:00"
    assert items[1]["created_at"] == "2026-07-18T10:00:00+08:00"
    assert items[1]["message_count"] == 2
    assert items[0]["first_message_content"] == "较晚创建的问题"


def test_update_chat_session_title_persists_metadata_and_list_value(store: SessionStore) -> None:
    store.add_message(
        NewMessage(
            session_key="web:rename",
            role="user",
            content="原始问题",
            timestamp="2026-07-18T11:00:00+08:00",
        )
    )

    updated = store.update_chat_session_title("web:rename", "新的标题")
    items, _ = store.list_chat_sessions(channel="web")

    assert updated is not None
    assert updated["metadata"]["title"] == "新的标题"
    assert items[0]["title"] == "新的标题"
    assert items[0]["first_message_content"] == "原始问题"


@pytest.mark.parametrize(
    ("content", "media", "expected"),
    [
        ("  请分析\n当前   项目结构  ", [], "请分析 当前 项目结构"),
        ("", ["D:/uploads/photo.png"], "分析图片内容"),
        ("", ["D:/uploads/report.pdf"], "分析文件内容"),
        ("", ["D:/uploads/photo.png", "D:/uploads/report.pdf"], "分析附件内容"),
    ],
)
def test_first_user_message_persists_default_title(
    store: SessionStore,
    content: str,
    media: list[str],
    expected: str,
) -> None:
    store.add_message(NewMessage(
        session_key="web:auto-title",
        role="user",
        content=content,
        extra={"media": media},
    ))

    meta = store.get_session_meta("web:auto-title")

    assert meta is not None
    assert meta["metadata"]["title"] == expected


def test_default_title_stores_eighty_characters_without_ellipsis(store: SessionStore) -> None:
    store.add_message(NewMessage(
        session_key="web:long-title",
        role="user",
        content="项" * 100,
    ))

    meta = store.get_session_meta("web:long-title")

    assert meta is not None
    assert meta["metadata"]["title"] == "项" * 80


def test_default_title_does_not_replace_manual_title(store: SessionStore) -> None:
    store.create_session("web:manual-title")
    store.update_chat_session_title("web:manual-title", "用户标题")

    store.add_message(NewMessage(
        session_key="web:manual-title",
        role="user",
        content="首条用户问题",
    ))

    meta = store.get_session_meta("web:manual-title")
    assert meta is not None
    assert meta["metadata"]["title"] == "用户标题"


def test_reopen_backfills_default_title_for_legacy_session(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-sessions.db"
    first = SessionStore(db_path)
    first.add_message(NewMessage(
        session_key="web:legacy",
        role="user",
        content="旧会话的首条问题",
    ))
    first.update_chat_session_title("web:legacy", "")
    first.close()

    second = SessionStore(db_path)
    try:
        meta = second.get_session_meta("web:legacy")
    finally:
        second.close()

    assert meta is not None
    assert meta["metadata"]["title"] == "旧会话的首条问题"


def test_delete_chat_session_cascades_messages_and_is_idempotent(store: SessionStore) -> None:
    store.add_message(NewMessage(session_key="web:delete", role="user", content="待删除内容"))
    store.add_message(NewMessage(session_key="web:keep", role="user", content="保留内容"))

    assert store.delete_chat_session("web:delete") is True
    assert store.delete_chat_session("web:delete") is False
    assert store.get_session_meta("web:delete") is None
    assert store.fetch_session_messages("web:delete") == []
    assert store.get_session_meta("web:keep") is not None
    assert store.search_messages("待删除内容", session_key="web:delete") == ([], 0)


def test_delete_empty_chat_session_does_not_delete_sessions_with_messages(
    store: SessionStore,
) -> None:
    store.create_session("web:empty")
    store.add_message(NewMessage(session_key="web:with-message", role="user", content="保留"))

    assert store.delete_empty_chat_session("web:empty") is True
    assert store.get_session_meta("web:empty") is None
    assert store.delete_empty_chat_session("web:empty") is False
    assert store.delete_empty_chat_session("web:with-message") is False
    assert store.get_session_meta("web:with-message") is not None


def test_consolidation_window_counts_before_decoding_messages(
    store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(3):
        store.add_message(NewMessage(
            session_key="web:below-threshold",
            role="user" if index % 2 == 0 else "assistant",
            content=f"消息 {index}",
        ))
    # 阈值以下只能执行 COUNT；即使消息扩展字段损坏，也不应读取或反序列化正文行。
    store._conn.execute(
        "UPDATE messages SET extra='not-json' WHERE session_key=? AND seq=?",
        ("web:below-threshold", 0),
    )
    store._conn.commit()
    monkeypatch.setattr(
        SessionStore,
        "_row_to_message",
        staticmethod(lambda row: pytest.fail("阈值以下不应解码消息行")),
    )

    result = store.fetch_consolidation_window(
        "web:below-threshold",
        keep_count=2,
        threshold=2,
        recent_count=1,
    )

    assert result is None


def test_consolidation_window_reads_only_cursor_tail(
    store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(6):
        store.add_message(NewMessage(
            session_key="web:window",
            role="user" if index % 2 == 0 else "assistant",
            content=f"消息 {index}",
        ))
    store.set_cursor("web:window", 2)
    # cursor 以前的数据不属于活动窗口，不能再被归档查询反序列化。
    store._conn.execute(
        "UPDATE messages SET extra='not-json' WHERE session_key=? AND seq=?",
        ("web:window", 0),
    )
    store._conn.commit()
    original = SessionStore._row_to_message

    def decode_active_row(row):
        assert int(row["seq"]) >= 2
        return original(row)

    monkeypatch.setattr(SessionStore, "_row_to_message", staticmethod(decode_active_row))

    result = store.fetch_consolidation_window(
        "web:window",
        keep_count=2,
        threshold=2,
        recent_count=1,
    )

    assert result is not None
    assert result.cursor == 2
    assert result.next_cursor == 4
    assert result.active_count == 4
    assert [item["seq"] for item in result.messages] == [2, 3]
    assert [item["seq"] for item in result.recent_messages] == [5]


def test_list_chat_messages_returns_persisted_frontend_fields(
    store: SessionStore,
) -> None:
    store.add_message(
        NewMessage(
            session_key="web:chat-1",
            role="user",
            content="查看图片",
            turn_id="turn-1",
            extra={"media": ["D:/workspace/uploads/image.png"]},
        )
    )

    items, total = store.list_chat_messages(
        "web:chat-1", limit=50, offset=0
    )

    assert total == 1
    assert items[0]["turn_id"] == "turn-1"
    assert items[0]["media"] == ["D:/workspace/uploads/image.png"]


def test_last_chat_message_timestamp_uses_messages_not_session_updated_at(
    store: SessionStore,
) -> None:
    first = store.add_message(
        NewMessage(session_key="web:chat-1", role="user", content="first")
    )
    second = store.add_message(
        NewMessage(session_key="web:chat-1", role="assistant", content="second")
    )

    store.set_cursor("web:chat-1", 99)

    assert store.get_last_chat_message_timestamp("web:chat-1") == second["timestamp"]
    assert store.get_last_chat_message_timestamp("web:missing") is None
    assert first["timestamp"] != second["timestamp"] or second["timestamp"]


def test_session_recent_context_is_scoped_by_session(store: SessionStore) -> None:
    store.create_session("web:chat-1")
    store.create_session("web:chat-2")

    assert store.get_recent_context("web:chat-1") == ""

    store.set_recent_context(
        "web:chat-1",
        "# Recent Context\n\n## Compression\n- 最近持续关注：项目\n",
        source_ref="web:chat-1@0-3",
    )
    store.set_recent_context(
        "web:chat-2",
        "# Recent Context\n\n## Compression\n- 最近持续关注：生活\n",
        source_ref="web:chat-2@0-3",
    )

    assert "项目" in store.get_recent_context("web:chat-1")
    assert "生活" not in store.get_recent_context("web:chat-1")
    assert "生活" in store.get_recent_context("web:chat-2")


def test_session_recent_context_is_deleted_with_session(store: SessionStore) -> None:
    store.set_recent_context(
        "web:delete",
        "# Recent Context\n\n## Compression\n- 最近持续关注：待删除\n",
        source_ref="web:delete@0-1",
    )

    assert store.get_recent_context("web:delete")
    assert store.delete_chat_session("web:delete") is True
    assert store.get_recent_context("web:delete") == ""


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
