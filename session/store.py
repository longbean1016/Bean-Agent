"""对齐 akashic-agent 的同步 SQLite 会话持久化。"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MESSAGE_ROLES = {"user", "assistant", "tool"}
_MESSAGE_COLUMNS = "id, session_key, seq, role, content, tool_chain, extra, ts"


@dataclass(slots=True)
class NewMessage:
    """写入 SessionStore 前的消息数据。"""

    session_key: str
    role: str
    content: str
    turn_id: str = ""
    tool_chain: list[dict[str, Any]] = field(default_factory=list)
    reasoning_content: str = ""
    status: str = "ok"
    timestamp: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


class SessionStore:
    """为 SessionManager、消息工具和记忆归档提供持久化接口。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._has_fts = False
        self._init_schema()

    def create_session(self, session_key: str) -> dict[str, Any]:
        """幂等创建会话并返回当前元数据。"""

        return self._create_session_sync(session_key)

    def add_message(self, message: NewMessage) -> dict[str, Any]:
        """原子分配 seq、写入消息并推进会话 next_seq。"""

        return self._add_message_sync(message)

    def get_session_meta(self, session_key: str) -> dict[str, Any] | None:
        """读取会话元数据；不存在时返回 None。"""

        return self._get_session_meta_sync(session_key)

    def fetch_session_messages(
        self,
        session_key: str,
    ) -> list[dict[str, Any]]:
        """按 seq 升序读取会话的全部持久化消息。"""

        return self._fetch_session_messages_sync(session_key)

    def upsert_session(
        self,
        session_key: str,
        *,
        created_at: str,
        updated_at: str,
        last_consolidated: int,
        metadata: dict[str, Any],
    ) -> None:
        """创建或更新 Session 元数据，不改动消息和 next_seq。"""

        self._upsert_session_sync(
            session_key,
            created_at,
            updated_at,
            last_consolidated,
            metadata,
        )

    def load_history(
        self,
        session_key: str,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        """加载最近的持久化消息，并转换成模型可消费的标准消息。"""

        return self._load_history_sync(session_key, limit)

    def fetch_messages(
        self,
        session_key: str,
        ids: list[str],
        context: int = 0,
    ) -> list[dict[str, Any]]:
        """按 ID 获取消息，并可扩展同一会话内前后若干条上下文。"""

        return self._fetch_messages_sync(session_key, ids, context)

    def search_messages(
        self,
        session_key: str,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """在指定会话内搜索消息正文。"""

        return self._search_messages_sync(session_key, query, limit)

    def set_cursor(self, session_key: str, value: int) -> None:
        """设置该会话已完成记忆归档的消息序号。"""

        self._set_cursor_sync(session_key, value)

    def get_cursor(self, session_key: str) -> int:
        """读取记忆归档 cursor；会话不存在时返回 0。"""

        return self._get_cursor_sync(session_key)

    def close(self) -> None:
        """幂等关闭 SQLite 连接。"""

        self._close_sync()

    def _init_schema(self) -> None:
        """创建基础表，并在环境支持时启用 FTS5 trigram 索引。"""

        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    key               TEXT PRIMARY KEY,
                    created_at        TEXT NOT NULL,
                    updated_at        TEXT NOT NULL,
                    last_consolidated INTEGER NOT NULL DEFAULT 0,
                    next_seq          INTEGER NOT NULL DEFAULT 0,
                    metadata          TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id          TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    seq         INTEGER NOT NULL,
                    role        TEXT NOT NULL,
                    content     TEXT NOT NULL DEFAULT '',
                    tool_chain  TEXT,
                    extra       TEXT NOT NULL DEFAULT '{}',
                    ts          TEXT NOT NULL,
                    UNIQUE (session_key, seq),
                    FOREIGN KEY (session_key) REFERENCES sessions(key)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session_seq
                    ON messages(session_key, seq);
                """
            )
            try:
                self._conn.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                        content,
                        content='messages',
                        content_rowid='rowid',
                        tokenize='trigram'
                    );

                    CREATE TRIGGER IF NOT EXISTS messages_ai
                    AFTER INSERT ON messages BEGIN
                        INSERT INTO messages_fts(rowid, content)
                        VALUES (new.rowid, new.content);
                    END;

                    CREATE TRIGGER IF NOT EXISTS messages_ad
                    AFTER DELETE ON messages BEGIN
                        INSERT INTO messages_fts(messages_fts, rowid, content)
                        VALUES ('delete', old.rowid, old.content);
                    END;

                    CREATE TRIGGER IF NOT EXISTS messages_au
                    AFTER UPDATE ON messages BEGIN
                        INSERT INTO messages_fts(messages_fts, rowid, content)
                        VALUES ('delete', old.rowid, old.content);
                        INSERT INTO messages_fts(rowid, content)
                        VALUES (new.rowid, new.content);
                    END;
                    """
                )
                self._has_fts = True
            except sqlite3.OperationalError as error:
                # 某些 Python/SQLite 构建未包含 FTS5 或 trigram tokenizer。
                # 基础表仍可使用，搜索在运行时自动走 LIKE 路径。
                self._has_fts = False
                logger.info("FTS5 trigram 不可用，消息搜索退化为 LIKE: %s", error)
            self._conn.commit()

    def _create_session_sync(self, session_key: str) -> dict[str, Any]:
        key = self._validate_session_key(session_key)
        now = _now_iso()
        with self._lock:
            self._ensure_open()
            self._conn.execute(
                """
                INSERT OR IGNORE INTO sessions (
                    key, created_at, updated_at, last_consolidated, next_seq, metadata
                ) VALUES (?, ?, ?, 0, 0, '{}')
                """,
                (key, now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                """
                SELECT key, created_at, updated_at, last_consolidated,
                       next_seq, metadata
                FROM sessions WHERE key = ?
                """,
                (key,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"会话创建后无法读取: {key}")
        return self._row_to_session(row)

    def _add_message_sync(self, message: NewMessage) -> dict[str, Any]:
        session_key = self._validate_session_key(message.session_key)
        role = str(message.role).strip().lower()
        if role not in _MESSAGE_ROLES:
            raise ValueError(f"不支持的消息 role: {message.role!r}")
        if not isinstance(message.content, str):
            raise TypeError("消息 content 必须是字符串")

        timestamp = message.timestamp or _now_iso()
        tool_chain_payload = (
            json.dumps(message.tool_chain, ensure_ascii=False)
            if message.tool_chain
            else None
        )
        extra_payload = json.dumps(
            {
                **message.extra,
                "turn_id": message.turn_id,
                "reasoning_content": message.reasoning_content,
                "status": message.status,
                "metadata": message.metadata,
            },
            ensure_ascii=False,
        )

        # next_seq 的读取、消息插入和递增必须属于同一个写事务。否则多个
        # 线程调用者可能读到相同序号，造成消息 ID 或唯一键冲突。
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = _now_iso()
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO sessions (
                        key, created_at, updated_at, last_consolidated,
                        next_seq, metadata
                    ) VALUES (?, ?, ?, 0, 0, '{}')
                    """,
                    (session_key, now, now),
                )
                session_row = self._conn.execute(
                    "SELECT next_seq FROM sessions WHERE key = ?",
                    (session_key,),
                ).fetchone()
                if session_row is None:
                    raise RuntimeError(f"无法读取会话序号: {session_key}")
                seq = int(session_row["next_seq"] or 0)
                message_id = f"{session_key}:{seq}"
                self._conn.execute(
                    """
                    INSERT INTO messages (
                        id, session_key, seq, role, content, tool_chain, extra, ts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        session_key,
                        seq,
                        role,
                        message.content,
                        tool_chain_payload,
                        extra_payload,
                        timestamp,
                    ),
                )
                self._conn.execute(
                    """
                    UPDATE sessions
                    SET next_seq = ?, updated_at = ?
                    WHERE key = ?
                    """,
                    (seq + 1, timestamp, session_key),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        return {
            "id": message_id,
            "session_key": session_key,
            "seq": seq,
            "role": role,
            "content": message.content,
            "tool_chain": list(message.tool_chain),
            "timestamp": timestamp,
            "turn_id": message.turn_id,
            "reasoning_content": message.reasoning_content,
            "status": message.status,
            "metadata": dict(message.metadata),
        }

    def _get_session_meta_sync(self, session_key: str) -> dict[str, Any] | None:
        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                """
                SELECT key, created_at, updated_at, last_consolidated,
                       next_seq, metadata
                FROM sessions WHERE key = ?
                """,
                (key,),
            ).fetchone()
        return self._row_to_session(row) if row is not None else None

    def _fetch_session_messages_sync(
        self,
        session_key: str,
    ) -> list[dict[str, Any]]:
        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS}
                FROM messages
                WHERE session_key = ?
                ORDER BY seq ASC
                """,
                (key,),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def _upsert_session_sync(
        self,
        session_key: str,
        created_at: str,
        updated_at: str,
        last_consolidated: int,
        metadata: dict[str, Any],
    ) -> None:
        key = self._validate_session_key(session_key)
        payload = json.dumps(metadata or {}, ensure_ascii=False)
        with self._lock:
            self._ensure_open()
            # next_seq 由 add_message 的事务独立维护；元数据刷新绝不能把它
            # 覆盖回旧值，否则后续消息可能获得重复 ID。
            self._conn.execute(
                """
                INSERT INTO sessions (
                    key, created_at, updated_at, last_consolidated,
                    next_seq, metadata
                ) VALUES (?, ?, ?, ?, 0, ?)
                ON CONFLICT(key) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    last_consolidated = excluded.last_consolidated,
                    metadata = excluded.metadata
                """,
                (
                    key,
                    created_at,
                    updated_at,
                    int(last_consolidated),
                    payload,
                ),
            )
            self._conn.commit()

    def _load_history_sync(
        self,
        session_key: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        key = self._validate_session_key(session_key)
        safe_limit = max(0, int(limit))
        if safe_limit == 0:
            return []

        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS}
                FROM messages
                WHERE session_key = ?
                ORDER BY seq DESC
                LIMIT ?
                """,
                (key, safe_limit),
            ).fetchall()

            # limit 只决定基础窗口大小。若窗口从 assistant 开始，继续向前
            # 补到最近的 user，避免把同一 Turn 的 user/assistant/tool_chain
            # 拆开。补齐查询与基础查询共用数据库锁，保证看到一致的历史快照。
            if rows and str(rows[-1]["role"]) != "user":
                boundary = self._conn.execute(
                    """
                    SELECT seq
                    FROM messages
                    WHERE session_key = ? AND role = ? AND seq < ?
                    ORDER BY seq DESC
                    LIMIT 1
                    """,
                    (key, "user", int(rows[-1]["seq"])),
                ).fetchone()
                if boundary is not None:
                    rows = self._conn.execute(
                        f"""
                        SELECT {_MESSAGE_COLUMNS}
                        FROM messages
                        WHERE session_key = ? AND seq >= ?
                        ORDER BY seq DESC
                        """,
                        (key, int(boundary["seq"])),
                    ).fetchall()
        persisted = [self._row_to_message(row) for row in reversed(rows)]

        history: list[dict[str, Any]] = []
        for message in persisted:
            history.extend(self._message_to_history(message))
        return history

    def _fetch_messages_sync(
        self,
        session_key: str,
        ids: list[str],
        context: int,
    ) -> list[dict[str, Any]]:
        key = self._validate_session_key(session_key)
        clean_ids = list(dict.fromkeys(str(item).strip() for item in ids if str(item).strip()))
        if not clean_ids:
            return []
        safe_context = max(0, min(int(context), 100))
        placeholders = ",".join("?" for _ in clean_ids)

        with self._lock:
            self._ensure_open()
            source_rows = self._conn.execute(
                f"""
                SELECT seq FROM messages
                WHERE session_key = ? AND id IN ({placeholders})
                """,
                (key, *clean_ids),
            ).fetchall()
            if not source_rows:
                return []

            ranges = [
                (max(0, int(row["seq"]) - safe_context), int(row["seq"]) + safe_context)
                for row in source_rows
            ]
            range_sql = " OR ".join("(seq BETWEEN ? AND ?)" for _ in ranges)
            range_params = [value for pair in ranges for value in pair]
            rows = self._conn.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS}
                FROM messages
                WHERE session_key = ? AND ({range_sql})
                ORDER BY seq ASC
                """,
                (key, *range_params),
            ).fetchall()

        source_ids = set(clean_ids)
        result: list[dict[str, Any]] = []
        for row in rows:
            message = self._row_to_message(row)
            message["in_source_ref"] = message["id"] in source_ids
            result.append(message)
        return result

    def _search_messages_sync(
        self,
        session_key: str,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        key = self._validate_session_key(session_key)
        term = str(query).strip()
        if not term:
            return []
        safe_limit = max(1, min(int(limit), 100))

        with self._lock:
            self._ensure_open()
            rows: list[sqlite3.Row] = []
            if self._has_fts and len(term) >= 3:
                # FTS 查询使用引号包裹用户文本，避免 AND/OR 等字符改变语义。
                fts_query = f'"{term.replace(chr(34), chr(34) * 2)}"'
                try:
                    rows = self._conn.execute(
                        f"""
                        SELECT {_select_columns('m')}
                        FROM messages_fts
                        JOIN messages m ON m.rowid = messages_fts.rowid
                        WHERE messages_fts MATCH ? AND m.session_key = ?
                        ORDER BY bm25(messages_fts), m.seq DESC
                        LIMIT ?
                        """,
                        (fts_query, key, safe_limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []

            if not rows:
                rows = self._conn.execute(
                    f"""
                    SELECT {_MESSAGE_COLUMNS}
                    FROM messages
                    WHERE session_key = ? AND content LIKE ?
                    ORDER BY seq DESC
                    LIMIT ?
                    """,
                    (key, f"%{term}%", safe_limit),
                ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def _set_cursor_sync(self, session_key: str, value: int) -> None:
        key = self._validate_session_key(session_key)
        cursor = max(0, int(value))
        now = _now_iso()
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO sessions (
                        key, created_at, updated_at, last_consolidated,
                        next_seq, metadata
                    ) VALUES (?, ?, ?, 0, 0, '{}')
                    """,
                    (key, now, now),
                )
                self._conn.execute(
                    """
                    UPDATE sessions
                    SET last_consolidated = ?, updated_at = ?
                    WHERE key = ?
                    """,
                    (cursor, now, key),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _get_cursor_sync(self, session_key: str) -> int:
        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT last_consolidated FROM sessions WHERE key = ?",
                (key,),
            ).fetchone()
        return int((row["last_consolidated"] if row else 0) or 0)

    def _close_sync(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def _message_to_history(
        self,
        message: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """将一条持久化消息展开为一个或多个标准模型消息。"""

        role = message["role"]
        if role == "user":
            return [{"role": "user", "content": message["content"]}]
        if role == "tool":
            tool_call_id = str(message["metadata"].get("tool_call_id", ""))
            return [
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": message["content"],
                }
            ]

        result: list[dict[str, Any]] = []
        for group in message["tool_chain"]:
            calls = group.get("calls") or []
            if not calls:
                continue
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": str(group.get("text") or ""),
                "tool_calls": [
                    {
                        "id": str(call.get("call_id", "")),
                        "type": "function",
                        "function": {
                            "name": str(call.get("name", "")),
                            "arguments": json.dumps(
                                call.get("arguments", {}),
                                ensure_ascii=False,
                            ),
                        },
                    }
                    for call in calls
                ],
            }
            provider_fields = group.get("provider_fields")
            if isinstance(provider_fields, dict) and isinstance(
                provider_fields.get("reasoning_content"), str
            ):
                # 供应商扩展字段来自持久化 JSON，只允许已定义的 reasoning
                # 字段进入模型消息，不能覆盖 role/content/tool_calls。
                assistant_message["reasoning_content"] = provider_fields[
                    "reasoning_content"
                ]
            result.append(assistant_message)
            for call in calls:
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("call_id", "")),
                        "content": str(call.get("result", "")),
                    }
                )

        final_message: dict[str, Any] = {
            "role": "assistant",
            "content": message["content"],
        }
        if message["reasoning_content"]:
            final_message["reasoning_content"] = message["reasoning_content"]
        result.append(final_message)
        return result

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "key": str(row["key"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "last_consolidated": int(row["last_consolidated"] or 0),
            "next_seq": int(row["next_seq"] or 0),
            "metadata": _load_json_object(row["metadata"]),
        }

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
        extra = _load_json_object(row["extra"])
        metadata = extra.get("metadata")
        message = {
            "id": str(row["id"]),
            "session_key": str(row["session_key"]),
            "seq": int(row["seq"]),
            "role": str(row["role"]),
            "content": str(row["content"] or ""),
            "tool_chain": _load_json_list(row["tool_chain"]),
            "timestamp": str(row["ts"]),
            "turn_id": str(extra.get("turn_id", "")),
            "reasoning_content": str(extra.get("reasoning_content", "")),
            "status": str(extra.get("status", "ok")),
            "metadata": metadata if isinstance(metadata, dict) else {},
        }
        # akashic 的 Session 消息允许携带 media、llm_user_content 等扩展字段。
        # 固定字段由数据库列和上方兼容转换决定，extra 不能反向覆盖它们。
        for key, value in extra.items():
            if key not in message and key != "metadata":
                message[key] = value
        return message

    @staticmethod
    def _validate_session_key(session_key: str) -> str:
        key = str(session_key).strip()
        if not key:
            raise ValueError("session_key 不能为空")
        return key

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SessionStore 已关闭")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _select_columns(alias: str) -> str:
    return ", ".join(f"{alias}.{column.strip()}" for column in _MESSAGE_COLUMNS.split(","))


def _load_json_object(value: object) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _load_json_list(value: object) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        loaded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(loaded, list):
        return []
    return [item for item in loaded if isinstance(item, dict)]


__all__ = ["NewMessage", "SessionStore"]
