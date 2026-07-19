"""Markdown 两阶段提交、崩溃恢复和 consolidation cursor 测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from memory.consolidator import ConsolidationDraft, Consolidator
from memory.md_store import MarkdownMemoryStore
from memory.optimizer import MemoryOptimizer
from session.store import NewMessage, SessionStore


def test_pending_snapshot_rollback_and_startup_recovery(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.append_pending("- [identity] 用户是开发者")
    assert "用户是开发者" in store.snapshot_pending()
    store.append_pending("- [preference] 喜欢简洁")

    recovered = MarkdownMemoryStore(tmp_path)

    assert recovered.read_pending().splitlines() == [
        "- [identity] 用户是开发者",
        "- [preference] 喜欢简洁",
    ]
    assert not recovered.pending_snapshot_file.exists()


def test_markdown_store_initializes_complete_memory_files_without_overwrite(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text("已有长期记忆", encoding="utf-8")

    store = MarkdownMemoryStore(tmp_path)
    try:
        assert (memory_dir / "MEMORY.md").read_text(encoding="utf-8") == "已有长期记忆"
        assert (memory_dir / "SELF.md").read_text(encoding="utf-8").startswith("# BeanAgent 的自我认知")
        assert (memory_dir / "PENDING.md").read_text(encoding="utf-8") == ""
        assert (memory_dir / "RECENT_CONTEXT.md").read_text(encoding="utf-8") == ""
    finally:
        store.close()


def test_append_pending_once_is_idempotent(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)

    assert store.append_pending_once("- [key_info] port 8080", source_ref="web:c:0") is True
    assert store.append_pending_once("- [key_info] port 8080", source_ref="web:c:0") is False
    assert store.read_pending().count("port 8080") == 1


class Extractor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def extract(self, messages, previous_recent_context):
        if self.fail:
            raise RuntimeError("LLM failed")
        return ConsolidationDraft(
            history_entries=[{"summary": "完成项目", "emotional_weight": 2}],
            pending_items=[{"tag": "identity", "content": "用户是开发者"}],
            recent_context="# Recent Context\n\n## Compression\n- 正在开发项目",
        )


@pytest.mark.asyncio
async def test_consolidation_advances_cursor_only_after_markdown_commit(tmp_path: Path) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    markdown = MarkdownMemoryStore(tmp_path)
    try:
        for index in range(6):
            sessions.add_message(NewMessage(
                session_key="web:c",
                role="user" if index % 2 == 0 else "assistant",
                content=f"消息 {index}",
                timestamp=f"2026-07-16T10:0{index}:00+08:00",
            ))
        consolidator = Consolidator(sessions, markdown, Extractor(), keep_count=2, threshold=4)

        result = await consolidator.consolidate("web:c")
    finally:
        sessions.close()

    assert result is not None
    assert result.cursor == 4
    assert "[2026-07-16T10:00:00+08:00][user] 消息 0" in result.conversation
    assert "用户是开发者" in markdown.read_pending()
    assert "正在开发项目" in markdown.read_recent_context()


@pytest.mark.asyncio
async def test_consolidation_failure_keeps_cursor(tmp_path: Path) -> None:
    sessions = SessionStore(tmp_path / "sessions.db")
    markdown = MarkdownMemoryStore(tmp_path)
    try:
        for index in range(4):
            sessions.add_message(NewMessage(session_key="web:c", role="user", content=f"消息 {index}"))
        consolidator = Consolidator(sessions, markdown, Extractor(fail=True), keep_count=1, threshold=3)
        with pytest.raises(RuntimeError, match="LLM failed"):
            await consolidator.consolidate("web:c")
        cursor = sessions.get_cursor("web:c")
    finally:
        sessions.close()

    assert cursor == 0


@pytest.mark.asyncio
async def test_below_threshold_refreshes_recent_turns_without_llm(tmp_path: Path) -> None:
    class ForbiddenExtractor:
        async def extract(self, messages, previous_recent_context):
            raise AssertionError("未达到阈值时不应调用归档 LLM")

    sessions = SessionStore(tmp_path / "sessions.db")
    markdown = MarkdownMemoryStore(tmp_path)
    try:
        sessions.add_message(NewMessage(session_key="web:c", role="user", content="新问题"))
        sessions.add_message(NewMessage(
            session_key="web:c",
            role="assistant",
            content="新回答" + "长" * 80,
        ))
        sessions.add_message(NewMessage(session_key="web:c", role="tool", content="工具原文"))
        consolidator = Consolidator(sessions, markdown, ForbiddenExtractor(), keep_count=20, threshold=5)

        result = await consolidator.consolidate("web:c")
    finally:
        sessions.close()

    assert result is None
    recent = markdown.read_recent_context()
    assert "## Recent Turns" in recent
    assert "[user] 新问题" in recent
    assert "[a-preview] 新回答" in recent
    assert "[assistant]" not in recent
    assert "工具原文" not in recent
    assert "长" * 61 not in recent


class OptimizerLLM:
    def __init__(self, *, fail_second: bool = False) -> None:
        self.calls = 0
        self.fail_second = fail_second

    async def complete(self, messages, tools=None):
        self.calls += 1
        if self.fail_second and self.calls == 2:
            raise RuntimeError("self update failed")
        content = "# 用户长期记忆\n- 用户是开发者" if self.calls == 1 else "# BeanAgent\n## 人格与形象\n- 直接"
        return type("Response", (), {"content": content})()


@pytest.mark.asyncio
async def test_optimizer_commits_snapshot_only_after_both_outputs(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.append_pending("- [identity] 用户是开发者")

    result = await MemoryOptimizer(store, OptimizerLLM()).optimize()

    assert result["pending_chars"] > 0
    assert "用户是开发者" in store.read_long_term()
    assert store.read_pending() == ""
    assert not store.pending_snapshot_file.exists()


@pytest.mark.asyncio
async def test_optimizer_failure_rolls_snapshot_back(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.append_pending("- [identity] 用户是开发者")

    with pytest.raises(RuntimeError, match="self update failed"):
        await MemoryOptimizer(store, OptimizerLLM(fail_second=True)).optimize()

    assert "用户是开发者" in store.read_pending()
    assert not store.pending_snapshot_file.exists()
