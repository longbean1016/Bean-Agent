"""旧分片夹具与逐片持久化回归；只操作临时数据库。"""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from session.store import NewSessionEvent, SessionStore


def chunk(index, **changes):
    return replace(NewSessionEvent(
        session_key="web:stream", event_type="assistant/chunk", turn_id="turn-1",
        step=1, data={"text": f"片段{index}", "reasoning": "思考"},
        operation_key=f"chunk-{index}",
    ), **changes)


def seed_legacy(path, count):
    store = SessionStore(path)
    store.create_session("web:stream")
    store.close()
    items = [{
        "event_seq": index, "turn_id": "turn-1", "step": 1,
        "data": chunk(index).data, "operation_key": f"chunk-{index}",
        "status": "committed", "source_event_seqs": None,
        "created_at": "2026-01-01T00:00:00+08:00",
    } for index in range(count)]
    if items:
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "INSERT INTO session_chunk_rows VALUES (?, 0, ?, 1, ?, ?)",
                ("web:stream", "turn-1", json.dumps({"items": items}), items[0]["created_at"]),
            )


@pytest.mark.parametrize("count", [0, 30, 20_000, 40_000])
def test_new_chunks_do_not_decode_or_rewrite_history(tmp_path, monkeypatch, count):
    path = tmp_path / "sessions.db"
    seed_legacy(path, count)
    store = SessionStore(path)
    before = store.fetch_session_events("web:stream")
    with monkeypatch.context() as patch:
        def forbidden(*args):
            raise AssertionError("逐片热路径不能解析历史分片")
        patch.setattr(store, "_decode_chunk_row", forbidden)
        saved = store.append_session_event(chunk(count))
        assert saved["event_seq"] == count
        assert store.append_session_event(chunk(count)) == saved
        boundary = store.append_session_event(chunk(
            count + 1, event_type="assistant/completed", source_event_seqs=[count],
        ))
        assert boundary["event_seq"] == count + 1
    assert store.fetch_session_events("web:stream") == before + [saved, boundary]
    store.close()
    reopened = SessionStore(path)
    assert reopened.fetch_session_events("web:stream") == before + [saved, boundary]
    reopened.close()


def test_legacy_retry_conflicts_provenance_and_isolation(tmp_path):
    path = tmp_path / "sessions.db"
    seed_legacy(path, 5)
    store = SessionStore(path)
    old = store.fetch_session_events("web:stream")
    assert store.append_session_event(chunk(2)) == old[2]
    for change in ({"step": 2}, {"status": "error"}, {"data": {"text": "不同"}},
                   {"event_type": "assistant/completed"}, {"source_event_seqs": [0]}):
        with pytest.raises(ValueError, match="operation_key"):
            store.append_session_event(chunk(2, **change))
    for refs in ([99], [2, 1], [1, 1]):
        with pytest.raises(ValueError):
            store.append_session_event(chunk(5, source_event_seqs=refs))
    saved = store.append_session_event(chunk(5, turn_id="turn-2", step=2, source_event_seqs=[0, 4]))
    assert saved["event_seq"] == 5
    assert store.append_session_event(chunk(0, session_key="web:other"))["event_seq"] == 0
    store.close()


def test_write_failure_rolls_back_sequence_and_retry(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    store._conn.execute("""CREATE TRIGGER fail_stream BEFORE INSERT ON session_events
        WHEN NEW.event_type = 'assistant/chunk' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.append_session_event(chunk(0))
    assert store.fetch_session_events("web:stream") == []
    store._conn.execute("DROP TRIGGER fail_stream")
    assert store.append_session_event(chunk(0))["event_seq"] == 0
    store.close()
