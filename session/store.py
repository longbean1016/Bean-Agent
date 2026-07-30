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
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_LOCAL_TZ = ZoneInfo("Asia/Shanghai")
_MESSAGE_ROLES = {"user", "assistant", "tool"}
_MESSAGE_COLUMNS = "id, session_key, seq, role, content, tool_chain, extra, ts"
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_DEFAULT_TITLE_LIMIT = 80


def _default_session_title(content: str, media: object) -> str:
    """从首条用户消息生成稳定标题；文字优先，附件只在无文字时兜底。"""

    normalized = " ".join(str(content).split())
    if normalized:
        # 数据库存放可复用的标题正文，省略号仅由前端根据实际宽度渲染。
        return normalized[:_DEFAULT_TITLE_LIMIT]

    paths = [str(item) for item in media] if isinstance(media, list) else []
    if not paths:
        return "新对话"
    image_count = sum(Path(path).suffix.lower() in _IMAGE_SUFFIXES for path in paths)
    if image_count == len(paths):
        return "分析图片内容"
    if image_count == 0:
        return "分析文件内容"
    return "分析附件内容"


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

    def fetch_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """跨会话按消息 ID 查询，并保持调用方给出的 ID 顺序。"""

        clean_ids = list(dict.fromkeys(str(item).strip() for item in ids if str(item).strip()))
        if not clean_ids:
            return []
        placeholders = ",".join("?" for _ in clean_ids)
        order_expression = " ".join(
            f"WHEN ? THEN {index}" for index in range(len(clean_ids))
        )
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS}
                FROM messages
                WHERE id IN ({placeholders})
                ORDER BY CASE id {order_expression} END
                """,
                (*clean_ids, *clean_ids),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def fetch_by_ids_with_context(
        self,
        ids: list[str],
        context: int,
    ) -> list[dict[str, Any]]:
        """跨会话读取命中消息，并在各自 Session 内扩展前后文。"""

        clean_ids = list(dict.fromkeys(str(item).strip() for item in ids if str(item).strip()))
        if not clean_ids:
            return []
        safe_context = max(0, int(context))
        if safe_context == 0:
            messages = self.fetch_by_ids(clean_ids)
            for message in messages:
                message["in_source_ref"] = True
            return messages

        # 消息 ID 的最后一段是 seq，前缀完整保留为 session_key；无效引用静默忽略。
        session_sequences: dict[str, set[int]] = {}
        for message_id in clean_ids:
            parts = message_id.rsplit(":", 1)
            if len(parts) != 2:
                continue
            try:
                sequence = int(parts[1])
            except ValueError:
                continue
            session_sequences.setdefault(parts[0], set()).add(sequence)

        source_ids = set(clean_ids)
        results: list[dict[str, Any]] = []
        with self._lock:
            self._ensure_open()
            for session_key, sequences in session_sequences.items():
                expanded = {
                    value
                    for sequence in sequences
                    for value in range(
                        max(0, sequence - safe_context),
                        sequence + safe_context + 1,
                    )
                }
                placeholders = ",".join("?" for _ in expanded)
                rows = self._conn.execute(
                    f"""
                    SELECT {_MESSAGE_COLUMNS}
                    FROM messages
                    WHERE session_key = ? AND seq IN ({placeholders})
                    ORDER BY seq ASC
                    """,
                    (session_key, *sorted(expanded)),
                ).fetchall()
                for row in rows:
                    message = self._row_to_message(row)
                    message["in_source_ref"] = message["id"] in source_ids
                    results.append(message)
        return results

    def search_messages(
        self,
        query: str,
        *legacy_args: object,
        session_key: str | None = None,
        role: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int] | list[dict[str, Any]]:
        """搜索原始消息；旧的 ``(session_key, query)`` 调用仍返回消息列表。"""

        if legacy_args:
            # 兼容已发布的最小契约，待上层全部迁移后再单独移除。
            legacy_session_key = query
            legacy_query = str(legacy_args[0])
            messages, _ = self._search_messages_sync(
                legacy_query,
                session_key=legacy_session_key,
                role=role,
                limit=limit,
                offset=offset,
            )
            return messages
        return self._search_messages_sync(
            query,
            session_key=session_key,
            role=role,
            limit=limit,
            offset=offset,
        )

    def list_chat_sessions(
        self,
        *,
        channel: str = "web",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """按第一次用户提问时间列出聊天会话，并包含运行中但尚未落消息的标题会话。"""

        safe_limit = max(1, min(int(limit), 200))
        safe_offset = max(0, int(offset))
        prefix = f"{str(channel).strip()}:%"
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                """
                SELECT s.key,
                       COALESCE((
                           SELECT first.ts
                           FROM messages first
                           WHERE first.session_key = s.key
                             AND first.role = 'user'
                           ORDER BY first.seq ASC
                           LIMIT 1
                       ), s.created_at) AS created_at,
                       s.updated_at,
                       s.metadata,
                       COUNT(m.id) AS message_count,
                       COALESCE((
                           SELECT first.content
                           FROM messages first
                           WHERE first.session_key = s.key
                             AND first.role = 'user'
                           ORDER BY first.seq ASC
                           LIMIT 1
                       ), '') AS first_message_content
                FROM sessions s
                LEFT JOIN messages m ON m.session_key = s.key
                WHERE s.key LIKE ?
                 GROUP BY s.key, s.created_at, s.updated_at, s.metadata
                ORDER BY created_at DESC, s.key DESC
                """,
                (prefix,),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            metadata = _load_json_object(item.pop("metadata", "{}"))
            item["title"] = str(metadata.get("title") or "")
            if int(item.get("message_count") or 0) <= 0 and not item["title"].strip():
                continue
            items.append(item)
        total = len(items)
        return items[safe_offset:safe_offset + safe_limit], total

    def ensure_default_chat_session_title(
        self,
        session_key: str,
        content: str,
        media: object,
    ) -> dict[str, Any] | None:
        """在首条消息最终落库前写入稳定标题，供运行中会话目录恢复使用。"""

        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            now = _now_iso()
            self._conn.execute(
                """
                INSERT OR IGNORE INTO sessions (
                    key, created_at, updated_at, last_consolidated, next_seq, metadata
                ) VALUES (?, ?, ?, 0, 0, '{}')
                """,
                (key, now, now),
            )
            row = self._conn.execute(
                "SELECT metadata FROM sessions WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            metadata = _load_json_object(row["metadata"])
            if not str(metadata.get("title") or "").strip():
                metadata["title"] = _default_session_title(content, media)
                self._conn.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE key = ?",
                    (json.dumps(metadata, ensure_ascii=False), now, key),
                )
            self._conn.commit()
            summary = self._conn.execute(
                """
                SELECT s.key, s.created_at, s.updated_at, s.metadata,
                       COUNT(m.id) AS message_count,
                       COALESCE((
                           SELECT first.content
                           FROM messages first
                           WHERE first.session_key = s.key
                             AND first.role = 'user'
                           ORDER BY first.seq ASC
                           LIMIT 1
                       ), '') AS first_message_content
                FROM sessions s
                LEFT JOIN messages m ON m.session_key = s.key
                WHERE s.key = ?
                GROUP BY s.key, s.created_at, s.updated_at, s.metadata
                """,
                (key,),
            ).fetchone()
        if summary is None:
            return None
        item = dict(summary)
        metadata = _load_json_object(item.pop("metadata", "{}"))
        item["title"] = str(metadata.get("title") or "")
        return item

    def update_chat_session_title(self, session_key: str, title: str) -> dict[str, Any] | None:
        """只更新会话展示标题，不改写首条消息或会话创建时间。"""

        key = self._validate_session_key(session_key)
        clean_title = str(title).strip()
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT metadata FROM sessions WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            metadata = _load_json_object(row["metadata"])
            metadata["title"] = clean_title
            self._conn.execute(
                "UPDATE sessions SET metadata = ?, updated_at = ? WHERE key = ?",
                (json.dumps(metadata, ensure_ascii=False), _now_iso(), key),
            )
            self._conn.commit()
        return self.get_session_meta(key)

    def delete_chat_session(self, session_key: str) -> bool:
        """删除会话及其消息，返回删除前会话是否存在。

        消息由 SQLite 外键级联删除，FTS 删除触发器同步清理索引。该事务只连接
        Session 数据库，因此不会触及独立存储的向量记忆或 Markdown 长期记忆。
        """

        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            cursor = self._conn.execute("DELETE FROM sessions WHERE key = ?", (key,))
            self._conn.commit()
            return cursor.rowcount > 0

    def list_chat_messages(
        self,
        session_key: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """为聊天前端读取原始消息，不展开模型专用的 tool 消息。"""

        key = self._validate_session_key(session_key)
        safe_limit = max(1, min(int(limit), 500))
        safe_offset = max(0, int(offset))
        with self._lock:
            self._ensure_open()
            count_row = self._conn.execute(
                "SELECT COUNT(1) AS count FROM messages WHERE session_key = ?",
                (key,),
            ).fetchone()
            rows = self._conn.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS}
                FROM messages
                WHERE session_key = ?
                ORDER BY seq ASC
                LIMIT ? OFFSET ?
                """,
                (key, safe_limit, safe_offset),
            ).fetchall()
        total = int((count_row["count"] if count_row else 0) or 0)
        return [self._row_to_message(row) for row in rows], total

    def list_latest_chat_messages(
        self,
        session_key: str,
        *,
        limit: int = 60,
    ) -> tuple[list[dict[str, Any]], int, bool, int | None]:
        """返回最新一页聊天消息，结果仍按 seq 升序供前端直接渲染。"""

        key = self._validate_session_key(session_key)
        safe_limit = max(1, min(int(limit), 200))
        with self._lock:
            self._ensure_open()
            count_row = self._conn.execute(
                "SELECT COUNT(1) AS count FROM messages WHERE session_key = ?",
                (key,),
            ).fetchone()
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
        total = int((count_row["count"] if count_row else 0) or 0)
        items = [self._row_to_message(row) for row in reversed(rows)]
        has_more = total > len(items)
        next_before_seq = int(items[0]["seq"]) if has_more and items else None
        return items, total, has_more, next_before_seq

    def list_chat_messages_before(
        self,
        session_key: str,
        *,
        before_seq: int,
        limit: int = 60,
    ) -> tuple[list[dict[str, Any]], bool, int | None]:
        """按 seq 游标向前读取更早消息，避免首屏加载超大历史。"""

        key = self._validate_session_key(session_key)
        safe_before = max(0, int(before_seq))
        safe_limit = max(1, min(int(limit), 200))
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS}
                FROM messages
                WHERE session_key = ? AND seq < ?
                ORDER BY seq DESC
                LIMIT ?
                """,
                (key, safe_before, safe_limit),
            ).fetchall()
            more_before = int(rows[-1]["seq"]) if rows else safe_before
            more_row = self._conn.execute(
                "SELECT 1 FROM messages WHERE session_key = ? AND seq < ? LIMIT 1",
                (key, more_before),
            ).fetchone()
        items = [self._row_to_message(row) for row in reversed(rows)]
        has_more = more_row is not None
        next_before_seq = int(items[0]["seq"]) if has_more and items else None
        return items, has_more, next_before_seq

    def list_chat_messages_from(
        self,
        session_key: str,
        *,
        anchor_seq: int,
        limit: int = 60,
    ) -> tuple[list[dict[str, Any]], bool, bool, int | None]:
        """从指定 seq 往后读取一个正文窗口，用于右侧全局导航跳转。"""

        key = self._validate_session_key(session_key)
        safe_anchor = max(0, int(anchor_seq))
        safe_limit = max(1, min(int(limit), 200))
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS}
                FROM messages
                WHERE session_key = ? AND seq >= ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (key, safe_anchor, safe_limit),
            ).fetchall()
            first_seq = int(rows[0]["seq"]) if rows else safe_anchor
            last_seq = int(rows[-1]["seq"]) if rows else safe_anchor
            before_row = self._conn.execute(
                "SELECT 1 FROM messages WHERE session_key = ? AND seq < ? LIMIT 1",
                (key, first_seq),
            ).fetchone()
            after_row = self._conn.execute(
                "SELECT 1 FROM messages WHERE session_key = ? AND seq > ? LIMIT 1",
                (key, last_seq),
            ).fetchone()
        items = [self._row_to_message(row) for row in rows]
        next_before_seq = int(items[0]["seq"]) if before_row is not None and items else None
        return items, before_row is not None, after_row is not None, next_before_seq

    def list_chat_turns(self, session_key: str) -> list[dict[str, Any]]:
        """返回全局普通对话导航，只统计 user 消息，主动 assistant 不参与编号。"""

        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS}
                FROM messages
                WHERE session_key = ? AND role = 'user'
                ORDER BY seq ASC
                """,
                (key,),
            ).fetchall()
        turns: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            message = self._row_to_message(row)
            question = " ".join(str(message.get("content") or "").split()) or "附件消息"
            preview = question[:80].rstrip()
            turns.append({
                "id": str(message.get("turn_id") or message["id"]),
                "message_id": message["id"],
                "seq": int(message["seq"]),
                "turn_index": index,
                "question": question[:240].rstrip(),
                "preview": preview,
                "timestamp": message["timestamp"],
            })
        return turns

    def get_last_chat_message_timestamp(self, session_key: str) -> str | None:
        """Return the last user/assistant message timestamp for proactive idle checks."""

        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                """
                SELECT ts
                FROM messages
                WHERE session_key = ? AND role IN ('user', 'assistant')
                ORDER BY seq DESC
                LIMIT 1
                """,
                (key,),
            ).fetchone()
        return str(row["ts"]) if row is not None else None

    def set_cursor(self, session_key: str, value: int) -> None:
        """设置该会话已完成记忆归档的消息序号。"""

        self._set_cursor_sync(session_key, value)

    def get_cursor(self, session_key: str) -> int:
        """读取记忆归档 cursor；会话不存在时返回 0。"""

        return self._get_cursor_sync(session_key)

    def get_recent_context(self, session_key: str) -> str:
        """读取当前会话的近期压缩上下文；未生成时返回空字符串。"""

        return self._get_recent_context_sync(session_key)

    def set_recent_context(
        self,
        session_key: str,
        content: str,
        *,
        source_ref: str = "",
    ) -> None:
        """写入当前会话的近期压缩上下文，与消息 cursor 使用同一个 Session DB。"""

        self._set_recent_context_sync(session_key, content, source_ref=source_ref)

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

                CREATE TABLE IF NOT EXISTS session_recent_context (
                    session_key TEXT PRIMARY KEY,
                    content     TEXT NOT NULL DEFAULT '',
                    source_ref  TEXT NOT NULL DEFAULT '',
                    updated_at  TEXT NOT NULL,
                    FOREIGN KEY (session_key) REFERENCES sessions(key)
                        ON DELETE CASCADE
                );
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
            self._backfill_default_titles_locked()
            self._conn.commit()

    def _backfill_default_titles_locked(self) -> None:
        """幂等补齐旧会话标题，不改变原创建时间、更新时间和手动标题。"""

        rows = self._conn.execute(
            """
            SELECT s.key, s.metadata, first.content, first.extra
            FROM sessions s
            JOIN messages first ON first.id = (
                SELECT candidate.id
                FROM messages candidate
                WHERE candidate.session_key = s.key
                  AND candidate.role = 'user'
                ORDER BY candidate.seq ASC
                LIMIT 1
            )
            """
        ).fetchall()
        for row in rows:
            metadata = _load_json_object(row["metadata"])
            if str(metadata.get("title") or "").strip():
                continue
            extra = _load_json_object(row["extra"])
            metadata["title"] = _default_session_title(
                str(row["content"] or ""),
                extra.get("media"),
            )
            self._conn.execute(
                "UPDATE sessions SET metadata = ? WHERE key = ?",
                (json.dumps(metadata, ensure_ascii=False), str(row["key"])),
            )

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
                    "SELECT next_seq, metadata FROM sessions WHERE key = ?",
                    (session_key,),
                ).fetchone()
                if session_row is None:
                    raise RuntimeError(f"无法读取会话序号: {session_key}")
                seq = int(session_row["next_seq"] or 0)
                message_id = f"{session_key}:{seq}"
                if role == "user":
                    first_user = self._conn.execute(
                        """
                        SELECT 1 FROM messages
                        WHERE session_key = ? AND role = 'user'
                        LIMIT 1
                        """,
                        (session_key,),
                    ).fetchone()
                    metadata = _load_json_object(session_row["metadata"])
                    if first_user is None and not str(metadata.get("title") or "").strip():
                        # 标题与首条用户消息属于同一事务，进程中断不能留下半套目录状态。
                        metadata["title"] = _default_session_title(
                            message.content,
                            message.extra.get("media"),
                        )
                        self._conn.execute(
                            "UPDATE sessions SET metadata = ? WHERE key = ?",
                            (json.dumps(metadata, ensure_ascii=False), session_key),
                        )
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
            # Consolidation 在后台直接推进 cursor，而 Session 缓存可能仍持有旧值；普通
            # metadata upsert 只允许 cursor 单调前进，显式重置仍由 set_cursor() 负责。
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
                    last_consolidated = MAX(
                        sessions.last_consolidated,
                        excluded.last_consolidated
                    ),
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
            meta = self._conn.execute(
                "SELECT last_consolidated FROM sessions WHERE key = ?",
                (key,),
            ).fetchone()
            cursor = max(0, int(meta["last_consolidated"] or 0)) if meta else 0
            rows = self._conn.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS}
                FROM messages
                WHERE session_key = ? AND seq >= ?
                ORDER BY seq DESC
                LIMIT ?
                """,
                (key, cursor, safe_limit),
            ).fetchall()

            # limit 只决定基础窗口大小。若窗口从 assistant 开始，继续向前
            # 补到最近的 user，避免把同一 Turn 的 user/assistant/tool_chain
            # 拆开。补齐查询与基础查询共用数据库锁，保证看到一致的历史快照。
            if rows and str(rows[-1]["role"]) != "user":
                boundary = self._conn.execute(
                    """
                    SELECT seq
                    FROM messages
                    WHERE session_key = ? AND role = ? AND seq >= ? AND seq < ?
                    ORDER BY seq DESC
                    LIMIT 1
                    """,
                    (key, "user", cursor, int(rows[-1]["seq"])),
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
        # cursor 可能来自旧版本或人工修复并落在 assistant 上。没有合法 user
        # 边界时丢弃孤立前缀，绝不能越过 cursor 重新加载已压缩原文。
        while persisted and persisted[0]["role"] != "user":
            persisted.pop(0)

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
        query: str,
        *,
        session_key: str | None,
        role: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        terms = [term for term in str(query).split() if term]
        if not terms:
            return [], 0
        safe_limit = max(1, min(int(limit), 100))
        safe_offset = max(0, int(offset))

        filters: list[str] = []
        filter_values: list[object] = []
        if session_key:
            filters.append("session_key = ?")
            filter_values.append(self._validate_session_key(session_key))
        if role:
            filters.append("role = ?")
            filter_values.append(str(role))

        term_filter = " OR ".join("content LIKE ?" for _ in terms)
        score_expression = " + ".join(
            "(CASE WHEN content LIKE ? THEN 1 ELSE 0 END)" for _ in terms
        )
        where = " AND ".join([*filters, f"({term_filter})"])
        patterns = [f"%{term}%" for term in terms]

        # 长词走 FTS，短词仍走 LIKE，再将两路候选合并去重。FTS 在部分 SQLite
        # 构建中不可用或可能拒绝特殊查询词，此时必须无损降级到下方 LIKE 路径。
        fts_terms = [term for term in terms if len(term) >= 3]
        if self._has_fts and fts_terms:
            fts_query = " OR ".join(fts_terms)
            alias_filters = [
                condition.replace("session_key", "m.session_key").replace(
                    "role", "m.role"
                )
                for condition in filters
            ]
            alias_term_filter = " OR ".join("m.content LIKE ?" for _ in terms)
            alias_score = " + ".join(
                "(CASE WHEN m.content LIKE ? THEN 1 ELSE 0 END)" for _ in terms
            )
            base_where = " AND ".join(alias_filters)
            connector = "AND" if base_where else "WHERE"
            where_prefix = f"WHERE {base_where}" if base_where else ""
            count_sql = (
                "SELECT COUNT(1) AS count FROM messages m "
                "LEFT JOIN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?) fts "
                "ON m.rowid = fts.rowid "
                f"{where_prefix} {connector} (fts.rowid IS NOT NULL OR ({alias_term_filter}))"
            )
            query_sql = (
                f"SELECT {_select_columns('m')}, ({alias_score}) AS match_score, "
                "fts.rank_score AS rank_score FROM messages m "
                "LEFT JOIN (SELECT rowid, bm25(messages_fts) AS rank_score "
                "FROM messages_fts WHERE messages_fts MATCH ?) fts ON m.rowid = fts.rowid "
                f"{where_prefix} {connector} (fts.rowid IS NOT NULL OR ({alias_term_filter})) "
                "ORDER BY match_score DESC, "
                "CASE WHEN rank_score IS NULL THEN 1 ELSE 0 END, rank_score ASC, m.seq DESC "
                "LIMIT ? OFFSET ?"
            )
            try:
                with self._lock:
                    self._ensure_open()
                    count_row = self._conn.execute(
                        count_sql,
                        (fts_query, *filter_values, *patterns),
                    ).fetchone()
                    rows = self._conn.execute(
                        query_sql,
                        (
                            *patterns,
                            fts_query,
                            *filter_values,
                            *patterns,
                            safe_limit,
                            safe_offset,
                        ),
                    ).fetchall()
                total = int((count_row["count"] if count_row else 0) or 0)
                return [self._row_to_message(row) for row in rows], total
            except sqlite3.OperationalError:
                pass

        with self._lock:
            self._ensure_open()
            count_row = self._conn.execute(
                f"SELECT COUNT(1) AS count FROM messages WHERE {where}",
                (*filter_values, *patterns),
            ).fetchone()
            rows = self._conn.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS}, ({score_expression}) AS match_score
                FROM messages
                WHERE {where}
                ORDER BY match_score DESC, seq DESC
                LIMIT ? OFFSET ?
                """,
                (*patterns, *filter_values, *patterns, safe_limit, safe_offset),
            ).fetchall()
        total = int((count_row["count"] if count_row else 0) or 0)
        return [self._row_to_message(row) for row in rows], total

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

    def _get_recent_context_sync(self, session_key: str) -> str:
        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT content FROM session_recent_context WHERE session_key = ?",
                (key,),
            ).fetchone()
        return str(row["content"] or "") if row else ""

    def _set_recent_context_sync(
        self,
        session_key: str,
        content: str,
        *,
        source_ref: str = "",
    ) -> None:
        key = self._validate_session_key(session_key)
        now = _now_iso()
        with self._lock:
            self._ensure_open()
            # recent context 是会话 cursor 的伴生状态；缺失会话先幂等创建，避免孤儿摘要。
            self._conn.execute(
                """
                INSERT OR IGNORE INTO sessions(
                    key, created_at, updated_at, last_consolidated,
                    next_seq, metadata
                )
                VALUES (?, ?, ?, 0, 0, '{}')
                """,
                (key, now, now),
            )
            self._conn.execute(
                """
                INSERT INTO session_recent_context(
                    session_key, content, source_ref, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    content = excluded.content,
                    source_ref = excluded.source_ref,
                    updated_at = excluded.updated_at
                """,
                (key, str(content), str(source_ref or ""), now),
            )
            self._conn.commit()

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
    # Session 时间直接面向用户和前端展示，显式固定业务时区，避免服务进程
    # 在 UTC 容器或 Windows 时区数据缺失时写出不一致的偏移。
    return datetime.now(_LOCAL_TZ).isoformat()


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
