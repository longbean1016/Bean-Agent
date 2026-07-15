"""历史消息定位工具的引用解析、上下文和分页预览测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from session.store import NewMessage, SessionStore
from tools.message_lookup import FetchMessagesTool, SearchMessagesTool


@pytest.mark.asyncio
async def test_fetch_messages_resolves_evidence_and_expands_context(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    try:
        rows = [
            store.add_message(
                NewMessage(
                    session_key="web:chat-1",
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"消息 {index}",
                )
            )
            for index in range(3)
        ]
        tool = FetchMessagesTool(store)

        result = json.loads(
            await tool.execute(
                evidence=[{"source_ref": f'{json.dumps([rows[1]["id"]])}#片段'}],
                context=1,
            )
        )
    finally:
        store.close()

    assert result["count"] == 3
    assert result["matched_count"] == 1
    assert [message["in_source_ref"] for message in result["messages"]] == [
        False,
        True,
        False,
    ]
    assert "tool_chain" not in result["messages"][1]


@pytest.mark.asyncio
async def test_search_messages_returns_paginated_location_previews(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    try:
        long_content = "\n".join(["needle first", *[f"line {i}" for i in range(55)]])
        store.add_message(
            NewMessage(session_key="web:chat-1", role="user", content=long_content)
        )
        store.add_message(
            NewMessage(session_key="web:chat-1", role="assistant", content="needle second")
        )
        tool = SearchMessagesTool(store)

        result = json.loads(
            await tool.execute(query="needle", session_key="web:chat-1", limit=1)
        )
    finally:
        store.close()

    assert result["count"] == 1
    assert result["matched_count"] == 2
    assert result["has_more"] is True
    assert result["next_offset"] == 1
    assert result["messages"][0]["source_ref"]
