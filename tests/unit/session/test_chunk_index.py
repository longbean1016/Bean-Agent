"""旧格式索引初始化、回滚、重启和回退兼容。"""

from contextlib import closing
import json
import sqlite3

import pytest

from session.chunk_index import initialize_chunk_index
from session.store import SessionStore
from tests.unit.session.test_stream_store import chunk, seed_legacy


def remove_index(db):
    db.execute("DROP TRIGGER session_chunk_index_insert")
    db.execute("DROP TRIGGER session_chunk_index_update")
    db.execute("DROP TABLE session_chunk_index_v1")


def test_old_format_initialization_backup_and_restore(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    backup = tmp_path / "backup.db"
    seed_legacy(path, 100)
    with closing(sqlite3.connect(path)) as db, db:
        remove_index(db)
    with closing(sqlite3.connect(path)) as db, closing(sqlite3.connect(backup)) as copy:
        db.backup(copy)
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    store = SessionStore(path)
    original = store.fetch_session_events("web:stream")
    assert store.append_session_event(chunk(100))["event_seq"] == 100
    store.close()
    with monkeypatch.context() as patch:
        def forbidden(*args):
            raise AssertionError("索引已存在时启动不应重复解码")
        patch.setattr(SessionStore, "_decode_chunk_row", forbidden)
        reopened = SessionStore(path)
        reopened.close()
    restored = SessionStore(backup)
    assert restored.fetch_session_events("web:stream") == original
    restored.close()


def test_failed_initialization_is_atomic_and_reentrant(tmp_path):
    path = tmp_path / "old.db"
    seed_legacy(path, 2)
    with closing(sqlite3.connect(path)) as db:
        with db:
            remove_index(db)
        db.row_factory = sqlite3.Row
        def fail(row):
            raise ValueError("invalid legacy row")
        with pytest.raises(ValueError):
            initialize_chunk_index(db, fail)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='session_chunk_index_v1'").fetchone() is None
        initialize_chunk_index(db, SessionStore._decode_chunk_row)
        assert db.execute("SELECT COUNT(*) FROM session_chunk_index_v1").fetchone()[0] == 2


def test_legacy_tail_update_and_delete_keep_index_consistent(tmp_path):
    path = tmp_path / "sessions.db"
    seed_legacy(path, 3)
    store = SessionStore(path)
    events = store.fetch_session_events("web:stream")
    tail = dict(events[-1], event_seq=3, operation_key="chunk-3", data=chunk(3).data)
    with store._conn:
        store._conn.execute("UPDATE session_chunk_rows SET chunks = ?", (json.dumps({"items": events + [tail]}),))
    assert store.append_session_event(chunk(3)) == tail
    assert store.append_session_event(chunk(4))["event_seq"] == 4
    with store._conn:
        store._conn.execute("DELETE FROM session_chunk_rows")
    assert store._conn.execute("SELECT COUNT(*) FROM session_chunk_index_v1").fetchone()[0] == 0
    store.close()
