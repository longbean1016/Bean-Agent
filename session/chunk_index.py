"""旧格式分片的派生索引；新事件直接使用 session_events 的单事件记录。"""

import sqlite3
from collections.abc import Callable
from typing import Any


def initialize_chunk_index(
    connection: sqlite3.Connection,
    decode: Callable[[sqlite3.Row], list[dict[str, Any]]],
) -> None:
    """一次事务建立 v1 索引，失败整体回滚；不改写旧分片及其序号。"""

    connection.execute("BEGIN IMMEDIATE")
    try:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_chunk_index_v1'"
        ).fetchone():
            connection.commit()
            return
        connection.execute("""
            CREATE TABLE session_chunk_index_v1 (
                session_key TEXT NOT NULL,
                event_seq INTEGER NOT NULL,
                operation_key TEXT NOT NULL,
                row_seq INTEGER NOT NULL,
                PRIMARY KEY (session_key, event_seq),
                UNIQUE (session_key, operation_key),
                FOREIGN KEY (session_key, row_seq)
                    REFERENCES session_chunk_rows(session_key, row_seq) ON DELETE CASCADE
            )
        """)
        connection.execute("""CREATE INDEX idx_chunk_index_row
            ON session_chunk_index_v1(session_key, row_seq)""")
        for row in connection.execute("SELECT * FROM session_chunk_rows"):
            connection.executemany(
                "INSERT INTO session_chunk_index_v1 VALUES (?, ?, ?, ?)",
                [(row["session_key"], item["event_seq"], item["operation_key"], row["row_seq"])
                 for item in decode(row)],
            )
        # 旧版本仍能读取新写入的单事件行；其追加旧格式尾行时，也要同步派生索引。
        insert = """
            INSERT INTO session_chunk_index_v1
            SELECT NEW.session_key, json_extract(value, '$.event_seq'),
                   json_extract(value, '$.operation_key'), NEW.row_seq
            FROM json_each(NEW.chunks, '$.items');
        """
        connection.execute(f"""CREATE TRIGGER session_chunk_index_insert
            AFTER INSERT ON session_chunk_rows BEGIN {insert} END""")
        connection.execute(f"""CREATE TRIGGER session_chunk_index_update
            AFTER UPDATE ON session_chunk_rows BEGIN
                DELETE FROM session_chunk_index_v1
                WHERE session_key = OLD.session_key AND row_seq = OLD.row_seq;
                {insert}
            END""")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
