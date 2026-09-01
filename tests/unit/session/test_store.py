"""SessionStore 的 SQLite 持久化与历史转换测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from session.store import NewMessage, NewSurfaceEvent, SessionStore


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


def test_context_usage_snapshot_is_persisted_without_overwriting_metadata(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "context-usage.db"
    first = SessionStore(db_path)
    first.create_session("web:usage")
    snapshot = {
        "pressure_tokens": 120,
        "projected_tokens": 135,
        "context_window": 1_000_000,
        "model_runtime_id": "deepseek:deepseek-v4-flash:1000000",
    }
    first.save_context_usage("web:usage", snapshot)
    first.update_chat_session_title("web:usage", "计量会话")
    first.close()

    second = SessionStore(db_path)
    try:
        assert second.get_context_usage("web:usage") == snapshot
        meta = second.get_session_meta("web:usage")
        assert meta is not None
        assert meta["metadata"]["title"] == "计量会话"
    finally:
        second.close()


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


def _surface_event(
    *,
    session_key: str = "web:surface",
    operation_key: str = "turn-1:0:frame",
    message: dict[str, object] | None = None,
    surface_op: str = "append",
    replace_start: int | None = None,
    replace_end: int | None = None,
    status: str = "committed",
) -> NewSurfaceEvent:
    payload = message or {"role": "user", "content": "frame"}
    return NewSurfaceEvent(
        session_key=session_key,
        epoch_id="epoch-a",
        turn_id="turn-1",
        iteration=0,
        role=str(payload["role"]),
        content=payload,
        source_kind="context_frame",
        operation_key=operation_key,
        surface_op=surface_op,
        replace_start=replace_start,
        replace_end=replace_end,
        status=status,
    )


def test_surface_append_is_idempotent_and_preserves_full_message(store: SessionStore) -> None:
    first = store.append_surface(_surface_event())
    retry = store.append_surface(_surface_event())

    assert first == retry
    assert first["surface_seq"] == 0
    assert first["message"] == {"role": "user", "content": "frame"}
    assert store.load_surface("web:surface") == [{"role": "user", "content": "frame"}]
    assert len(store.fetch_surface_events("web:surface")) == 1


def test_surface_replace_folds_current_nodes_and_increments_generation(
    store: SessionStore,
) -> None:
    store.append_surface(_surface_event(operation_key="a", message={"role": "user", "content": "a"}))
    store.append_surface(_surface_event(operation_key="b", message={"role": "assistant", "content": "b"}))
    replaced = store.replace_surface(_surface_event(
        operation_key="replace-1",
        message={"role": "user", "content": "summary"},
        surface_op="replace",
        replace_start=0,
        replace_end=1,
    ))

    assert replaced["surface_seq"] == 2
    assert replaced["replace_generation"] == 1
    assert store.load_surface("web:surface") == [{"role": "user", "content": "summary"}]
    assert store.fetch_surface_events("web:surface")[2]["surface_op"] == "replace"


def test_surface_replace_uses_node_boundaries_after_prior_replace(
    store: SessionStore,
) -> None:
    store.append_surface(_surface_event(operation_key="a", message={"role": "user", "content": "a"}))
    store.append_surface(_surface_event(operation_key="b", message={"role": "assistant", "content": "b"}))
    store.append_surface(_surface_event(operation_key="c", message={"role": "tool", "content": "c"}))
    store.replace_surface(_surface_event(
        operation_key="replace-1",
        message={"role": "user", "content": "summary"},
        surface_op="replace",
        replace_start=0,
        replace_end=1,
    ))

    # The current nodes are seq 3 (replacement) and seq 2 (tool); DSH resolves
    # start/end by their positions in the current surface, not numeric order.
    second = store.replace_surface(_surface_event(
        operation_key="replace-2",
        message={"role": "user", "content": "final summary"},
        surface_op="replace",
        replace_start=3,
        replace_end=2,
    ))
    assert second["replace_generation"] == 2
    assert store.load_surface("web:surface") == [{"role": "user", "content": "final summary"}]


def test_surface_replace_rejects_non_contiguous_current_nodes(store: SessionStore) -> None:
    store.append_surface(_surface_event(operation_key="a"))
    store.append_surface(_surface_event(operation_key="b", message={"role": "assistant", "content": "b"}))
    store.replace_surface(_surface_event(
        operation_key="replace-1",
        message={"role": "user", "content": "summary"},
        surface_op="replace",
        replace_start=0,
        replace_end=1,
    ))
    store.append_surface(_surface_event(operation_key="c", message={"role": "tool", "content": "c"}))

    with pytest.raises(ValueError, match="连续存在"):
        store.replace_surface(_surface_event(
            operation_key="replace-invalid",
            message={"role": "user", "content": "invalid"},
            surface_op="replace",
            replace_start=0,
            replace_end=2,
        ))


def test_surface_pending_events_are_isolated_and_recoverable(store: SessionStore) -> None:
    store.append_surface(_surface_event(
        session_key="web:a",
        operation_key="pending",
        status="pending",
    ))
    store.append_surface(_surface_event(
        session_key="web:b",
        operation_key="other",
        status="pending",
    ))

    assert [event["operation_key"] for event in store.recover_surface("web:a")] == ["pending"]
    assert [event["session_key"] for event in store.fetch_surface_events("web:a")] == ["web:a"]
    assert [event["session_key"] for event in store.fetch_surface_events("web:b")] == ["web:b"]


def test_surface_operation_key_rejects_conflicting_retry(store: SessionStore) -> None:
    store.append_surface(_surface_event())
    with pytest.raises(ValueError, match="不同事件"):
        store.append_surface(_surface_event(message={"role": "user", "content": "changed"}))


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
                {
                    "id": "call-running", "type": "function",
                    "function": {"name": "search", "arguments": '{"query": "weather"}'},
                },
            ],
            "reasoning_content": "先读取文件",
        },
        {"role": "tool", "tool_call_id": "call-ok", "content": "文件内容"},
        {"role": "tool", "tool_call_id": "call-error", "content": "命令失败"},
        {
            "role": "tool",
            "tool_call_id": "call-running",
            "content": "工具调用在中断前已经发出，但没有记录完整结果；结果未知。请根据工具语义决定是否重试：只有只读或幂等操作可以重试；可能产生副作用时先核验外部状态或询问用户，不要盲目重试。",
        },
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


def test_session_usage_is_idempotent_and_aggregated(store: SessionStore) -> None:
    store.create_session("web:usage")
    first = store.save_session_usage(
        "web:usage", "turn-1", 1,
        {
            "uncached_input_tokens": 100,
            "cache_read_tokens": 900,
            "cache_write_tokens": 0,
            "output_tokens": 40,
        },
    )
    repeated = store.save_session_usage(
        "web:usage", "turn-1", 1,
        {
            "uncached_input_tokens": 120,
            "cache_read_tokens": 880,
            "cache_write_tokens": 0,
            "output_tokens": 45,
        },
    )
    second = store.save_session_usage(
        "web:usage", "turn-1", 2,
        {
            "uncached_input_tokens": 50,
            "cache_read_tokens": 0,
            "cache_write_tokens": 10,
            "output_tokens": 5,
        },
    )

    assert first["total_input_tokens"] == repeated["total_input_tokens"] == 1_000
    assert repeated["total_output_tokens"] == 45
    assert second["total_input_tokens"] == 1_060
    assert second["total_output_tokens"] == 50
    assert second["cache_hit_rate"] == pytest.approx(880 / 1060)
