"""SessionDB checkpoint prepare/commit 契约测试。"""

from __future__ import annotations

import pytest
from dataclasses import replace

from session.compaction import compaction_source_ref
from session.store import (
    NewMessage,
    SessionCompaction,
    SessionCompactionPrepare,
    SessionStore,
)


def _prepare(store: SessionStore) -> SessionCompactionPrepare:
    meta = store.get_session_meta("web:c")
    assert meta is not None
    return SessionCompactionPrepare(
        session_key="web:c",
        session_created_at=meta["created_at"],
        generation=1,
        parent_generation=0,
        source_ref=compaction_source_ref("web:c@incarnation", 1),
        source_plan_digest="plan-1",
        source_mutation_digest="mutation-1",
        source_from_seq=0,
        consolidated_through_seq=2,
        source_message_ids=["web:c:0", "web:c:1"],
        selected_source_messages=[{"id": "web:c:0", "role": "user", "content": "问题"}],
        retained_tail=[{"id": "web:c:2", "role": "user", "content": "当前"}],
        prepared_at="2026-08-26T00:00:00+00:00",
    )


def _checkpoint(prepare: SessionCompactionPrepare) -> SessionCompaction:
    return SessionCompaction(
        session_key=prepare.session_key,
        session_created_at=prepare.session_created_at,
        generation=prepare.generation,
        parent_generation=prepare.parent_generation,
        created_at="2026-08-26T00:00:01+00:00",
        trigger="soft_limit",
        summary_format_version=1,
        summary="已完成摘要",
        source_ref=prepare.source_ref,
        source_plan_digest=prepare.source_plan_digest,
        source_mutation_digest=prepare.source_mutation_digest,
        source_from_seq=prepare.source_from_seq,
        consolidated_through_seq=prepare.consolidated_through_seq,
        source_message_ids=prepare.source_message_ids,
        selected_source_messages=prepare.selected_source_messages,
        retained_tail=prepare.retained_tail,
        model_runtime_id="test-runtime",
        model="test-model",
        context_window=1000,
        threshold_tokens=740,
        hard_input_tokens=800,
        keep_recent_tokens=200,
        tokens_before=760,
        tokens_after=400,
        summary_usage={"input_tokens": 10},
    )


def test_compaction_prepare_commit_is_idempotent(tmp_path) -> None:
    store = SessionStore(tmp_path / "session.db")
    store.create_session("web:c")
    store.add_message(NewMessage("web:c", "user", "问题"))
    store.add_message(NewMessage("web:c", "assistant", "回答"))

    prepare = _prepare(store)
    assert store.prepare_compaction(prepare) == prepare
    assert store.prepare_compaction(prepare) == prepare

    checkpoint = _checkpoint(prepare)
    assert store.commit_compaction(checkpoint) == checkpoint
    assert store.commit_compaction(checkpoint) == checkpoint
    assert store.get_active_compaction("web:c") == checkpoint
    assert store.get_session_meta("web:c")["last_consolidated"] == 1


def test_compaction_prepare_rejects_source_plan_drift(tmp_path) -> None:
    store = SessionStore(tmp_path / "session.db")
    store.create_session("web:c")
    prepare = _prepare(store)
    store.prepare_compaction(prepare)

    changed = replace(prepare, source_plan_digest="different")
    with pytest.raises(ValueError, match="不可变字段发生漂移"):
        store.prepare_compaction(changed)
