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

from session.model_surface import INTERRUPTED_TOOL_RESULT_CONTENT

logger = logging.getLogger(__name__)

_LOCAL_TZ = ZoneInfo("Asia/Shanghai")
_MESSAGE_ROLES = {"user", "assistant", "tool"}
_SURFACE_ROLES = {"system", "user", "assistant", "tool"}
_SURFACE_OPS = {"append", "replace"}
_SURFACE_STATUSES = {"pending", "committed", "replaced", "aborted", "error", "completed"}
# pending/aborted 事件只供恢复器判断，不能进入下一次模型请求的可见前缀；
# error 仍是模型实际收到的工具失败结果，必须保留在 projection 中。
_SURFACE_PROJECTABLE_STATUSES = {"committed", "replaced", "error", "completed"}
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


@dataclass(frozen=True, slots=True)
class NewSurfaceEvent:
    """写入模型侧 durable surface 的完整事件。"""

    session_key: str
    epoch_id: str
    turn_id: str
    iteration: int
    role: str
    content: dict[str, Any]
    source_kind: str
    operation_key: str
    status: str = "committed"
    projection_version: int = 1
    surface_op: str = "append"
    replace_start: int | None = None
    replace_end: int | None = None


@dataclass(frozen=True, slots=True)
class SessionCompactionPrepare:
    """压缩提交前的不可变 prepare 快照。"""

    session_key: str
    session_created_at: str
    generation: int
    parent_generation: int
    source_ref: str
    source_plan_digest: str
    source_mutation_digest: str
    source_from_seq: int
    consolidated_through_seq: int
    source_message_ids: list[str]
    selected_source_messages: list[dict[str, Any]]
    retained_tail: list[dict[str, Any]]
    prepared_at: str


@dataclass(frozen=True, slots=True)
class SessionCompaction:
    """已经提交并可注入下一轮 Prompt 的 checkpoint ledger 行。"""

    session_key: str
    session_created_at: str
    generation: int
    parent_generation: int
    created_at: str
    trigger: str
    summary_format_version: int
    summary: str
    source_ref: str
    source_plan_digest: str
    source_mutation_digest: str
    source_from_seq: int
    consolidated_through_seq: int
    source_message_ids: list[str]
    selected_source_messages: list[dict[str, Any]]
    retained_tail: list[dict[str, Any]]
    model_runtime_id: str
    model: str
    context_window: int
    threshold_tokens: int
    hard_input_tokens: int
    keep_recent_tokens: int
    tokens_before: int
    tokens_after: int
    summary_usage: dict[str, Any]
    invalidated_at: str | None = None
    invalidated_reason: str | None = None


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

    def append_surface(self, event: NewSurfaceEvent) -> dict[str, Any]:
        """在同一事务内幂等追加一条模型侧 surface 事件。"""

        return self._append_surface_sync(event)

    def load_surface(
        self,
        session_key: str,
        *,
        epoch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """按事件顺序折叠会话的当前模型消息投影。"""

        return self._load_surface_sync(session_key, epoch_id=epoch_id)

    def fetch_surface_events(
        self,
        session_key: str,
        *,
        epoch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """读取 surface 原始事件，供恢复和诊断使用。"""

        return self._fetch_surface_events_sync(session_key, epoch_id=epoch_id)

    def load_surface_events(
        self,
        session_key: str,
    ) -> list[dict[str, Any]]:
        """读取当前折叠后的 surface 节点及其序号。"""

        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            return [dict(item) for item in self._surface_projection_events_locked(key)]

    def replace_surface(self, event: NewSurfaceEvent) -> dict[str, Any]:
        """追加一条带明确起止边界的 surface 替换事件。"""

        if event.surface_op != "replace":
            raise ValueError("replace_surface 只能接收 surface_op='replace'")
        return self._append_surface_sync(event)

    def recover_surface(self, session_key: str) -> list[dict[str, Any]]:
        """返回尚未完成的 surface 事件，恢复器据此决定继续或终止。"""

        return [
            event
            for event in self.fetch_surface_events(session_key)
            if str(event.get("status") or "") == "pending"
        ]

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
        limit: int | None = None,
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

    def delete_empty_chat_session(self, session_key: str) -> bool:
        """仅删除没有语义消息和模型 surface 的临时会话。"""

        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            cursor = self._conn.execute(
                """
                DELETE FROM sessions
                WHERE key = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM messages WHERE session_key = sessions.key
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM surface_events WHERE session_key = sessions.key
                  )
                """,
                (key,),
            )
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

    def get_active_compaction(self, session_key: str) -> SessionCompaction | None:
        """读取当前 generation；消息边界由 ledger 的 seq 字段决定。"""

        return self._get_active_compaction_sync(session_key)

    def get_compaction(self, session_key: str, generation: int) -> SessionCompaction | None:
        """按 generation 读取 checkpoint，供重试和恢复校验使用。"""

        return self._get_compaction_sync(session_key, generation)

    def next_compaction_generation(self, session_key: str) -> int:
        """返回该 session 下一代 generation，不把消息 seq 当作 generation。"""

        return self._next_compaction_generation_sync(session_key)

    def prepare_compaction(self, prepare: SessionCompactionPrepare) -> SessionCompactionPrepare:
        """持久化 prepare；同一 generation 已存在时只允许完全相同的快照重试。"""

        return self._prepare_compaction_sync(prepare)

    def commit_compaction(self, checkpoint: SessionCompaction) -> SessionCompaction:
        """原子写入 ledger 并推进 sessions.last_consolidated generation 指针。"""

        return self._commit_compaction_sync(checkpoint)

    def save_context_usage(self, session_key: str, snapshot: dict[str, Any]) -> None:
        """保存会话最近一次上下文计量快照，不混入会话业务 metadata。"""

        self._save_context_usage_sync(session_key, snapshot)

    def get_context_usage(self, session_key: str) -> dict[str, Any] | None:
        """读取上下文计量快照；没有真实 usage 时仍可返回不完整快照。"""

        return self._get_context_usage_sync(session_key)

    def save_session_usage(
        self,
        session_key: str,
        turn_id: str,
        iteration: int,
        usage: dict[str, Any],
    ) -> dict[str, Any]:
        """幂等保存一次模型调用用量并返回当前会话累计值。"""

        return self._save_session_usage_sync(session_key, turn_id, iteration, usage)

    def get_session_usage(self, session_key: str) -> dict[str, Any] | None:
        """读取会话累计用量；没有真实 Provider usage 时返回 None。"""

        return self._get_session_usage_sync(session_key)

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

                CREATE TABLE IF NOT EXISTS surface_events (
                    session_key        TEXT NOT NULL,
                    epoch_id           TEXT NOT NULL,
                    surface_seq        INTEGER NOT NULL,
                    turn_id            TEXT NOT NULL,
                    iteration          INTEGER NOT NULL,
                    role               TEXT NOT NULL,
                    content            TEXT NOT NULL,
                    source_kind        TEXT NOT NULL,
                    status             TEXT NOT NULL,
                    projection_version INTEGER NOT NULL,
                    operation_key      TEXT NOT NULL,
                    surface_op         TEXT NOT NULL DEFAULT 'append',
                    replace_start      INTEGER,
                    replace_end        INTEGER,
                    replace_generation INTEGER NOT NULL DEFAULT 0,
                    created_at         TEXT NOT NULL,
                    PRIMARY KEY (session_key, surface_seq),
                    UNIQUE (session_key, operation_key),
                    FOREIGN KEY (session_key) REFERENCES sessions(key)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_surface_events_session_order
                    ON surface_events(session_key, surface_seq);
                CREATE INDEX IF NOT EXISTS idx_surface_events_session_status
                    ON surface_events(session_key, status);

                CREATE TABLE IF NOT EXISTS session_compaction_prepares (
                    session_key                  TEXT NOT NULL,
                    session_created_at           TEXT NOT NULL,
                    generation                   INTEGER NOT NULL,
                    parent_generation            INTEGER NOT NULL,
                    source_ref                   TEXT NOT NULL,
                    source_plan_digest           TEXT NOT NULL,
                    source_mutation_digest       TEXT NOT NULL,
                    source_from_seq              INTEGER NOT NULL,
                    consolidated_through_seq     INTEGER NOT NULL,
                    source_message_ids_json      TEXT NOT NULL,
                    selected_source_messages_json TEXT NOT NULL,
                    retained_tail_json           TEXT NOT NULL,
                    prepared_at                  TEXT NOT NULL,
                    PRIMARY KEY (session_key, generation),
                    UNIQUE (session_key, source_ref),
                    FOREIGN KEY (session_key) REFERENCES sessions(key)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS session_compactions (
                    session_key                  TEXT NOT NULL,
                    session_created_at           TEXT NOT NULL,
                    generation                   INTEGER NOT NULL,
                    parent_generation            INTEGER NOT NULL DEFAULT 0,
                    created_at                   TEXT NOT NULL,
                    trigger                      TEXT NOT NULL,
                    summary_format_version      INTEGER NOT NULL,
                    summary                     TEXT NOT NULL,
                    source_ref                   TEXT NOT NULL,
                    source_plan_digest           TEXT NOT NULL,
                    source_mutation_digest       TEXT NOT NULL,
                    source_from_seq              INTEGER NOT NULL,
                    consolidated_through_seq     INTEGER NOT NULL,
                    source_message_ids_json      TEXT NOT NULL,
                    selected_source_messages_json TEXT NOT NULL,
                    retained_tail_json           TEXT NOT NULL,
                    model_runtime_id             TEXT NOT NULL,
                    model                       TEXT NOT NULL,
                    context_window              INTEGER NOT NULL,
                    threshold_tokens            INTEGER NOT NULL,
                    hard_input_tokens           INTEGER NOT NULL,
                    keep_recent_tokens          INTEGER NOT NULL,
                    tokens_before               INTEGER NOT NULL,
                    tokens_after                INTEGER NOT NULL,
                    summary_usage_json          TEXT NOT NULL,
                    invalidated_at              TEXT,
                    invalidated_reason          TEXT,
                    PRIMARY KEY (session_key, generation),
                    UNIQUE (session_key, source_ref),
                    FOREIGN KEY (session_key) REFERENCES sessions(key)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_session_compactions_active
                    ON session_compactions(session_key, invalidated_at, generation);

                CREATE TABLE IF NOT EXISTS session_usage (
                    session_key                  TEXT NOT NULL,
                    turn_id                     TEXT NOT NULL,
                    iteration                   INTEGER NOT NULL,
                    uncached_input_tokens      INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens          INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens         INTEGER NOT NULL DEFAULT 0,
                    output_tokens              INTEGER NOT NULL DEFAULT 0,
                    updated_at                 TEXT NOT NULL,
                    PRIMARY KEY (session_key, turn_id, iteration),
                    FOREIGN KEY (session_key) REFERENCES sessions(key)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_session_usage_session
                    ON session_usage(session_key);
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

    def _append_surface_sync(self, event: NewSurfaceEvent) -> dict[str, Any]:
        """在 SQLite 写事务中分配 surface 序号并折叠替换边界。"""

        key = self._validate_session_key(event.session_key)
        epoch_id = str(event.epoch_id).strip()
        turn_id = str(event.turn_id).strip()
        source_kind = str(event.source_kind).strip()
        operation_key = str(event.operation_key).strip()
        role = str(event.role).strip().lower()
        status = str(event.status).strip().lower()
        surface_op = str(event.surface_op).strip().lower()
        if not epoch_id:
            raise ValueError("surface epoch_id 不能为空")
        if not operation_key:
            raise ValueError("surface operation_key 不能为空")
        if role not in _SURFACE_ROLES:
            raise ValueError(f"不支持的 surface role: {event.role!r}")
        if not source_kind:
            raise ValueError("surface source_kind 不能为空")
        if status not in _SURFACE_STATUSES:
            raise ValueError(f"不支持的 surface status: {event.status!r}")
        if surface_op not in _SURFACE_OPS:
            raise ValueError(f"不支持的 surface_op: {event.surface_op!r}")
        if not isinstance(event.content, dict):
            raise TypeError("surface content 必须是完整模型消息字典")
        if str(event.content.get("role") or role).strip().lower() != role:
            raise ValueError("surface role 必须与 content.role 一致")
        if surface_op == "append" and (
            event.replace_start is not None or event.replace_end is not None
        ):
            raise ValueError("append surface 不能携带 replace 边界")
        if surface_op == "replace":
            if event.replace_start is None or event.replace_end is None:
                raise ValueError("replace surface 必须携带 start/end")

        content_payload = json.dumps(
            event.content,
            ensure_ascii=False,
            separators=(",", ":"),
        )
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
                    (key, now, now),
                )
                existing = self._conn.execute(
                    """
                    SELECT session_key, epoch_id, surface_seq, turn_id, iteration,
                           role, content, source_kind, status, projection_version,
                           operation_key, surface_op, replace_start, replace_end,
                           replace_generation, created_at
                    FROM surface_events
                    WHERE session_key = ? AND operation_key = ?
                    """,
                    (key, operation_key),
                ).fetchone()
                if existing is not None:
                    current = self._row_to_surface_event(existing)
                    if not self._surface_event_matches(
                        current,
                        epoch_id=epoch_id,
                        turn_id=turn_id,
                        iteration=int(event.iteration),
                        role=role,
                        content=event.content,
                        source_kind=source_kind,
                        status=status,
                        projection_version=int(event.projection_version),
                        operation_key=operation_key,
                        surface_op=surface_op,
                        replace_start=event.replace_start,
                        replace_end=event.replace_end,
                    ):
                        raise ValueError(
                            f"surface operation_key 已绑定不同事件: {key}:{operation_key}"
                        )
                    self._conn.commit()
                    return current

                current_nodes = self._surface_projection_events_locked(key)
                replace_generation = max(
                    (int(item["replace_generation"]) for item in current_nodes),
                    default=0,
                )
                if surface_op == "replace":
                    start = int(event.replace_start)
                    end = int(event.replace_end)
                    start_index = next(
                        (
                            index
                            for index, item in enumerate(current_nodes)
                            if int(item["surface_seq"]) == start
                        ),
                        None,
                    )
                    end_index = next(
                        (
                            index
                            for index, item in enumerate(current_nodes)
                            if int(item["surface_seq"]) == end
                        ),
                        None,
                    )
                    if start_index is None or end_index is None or start_index > end_index:
                        raise ValueError(
                            "replace 边界必须覆盖当前 surface 中连续存在的节点"
                        )
                    replace_generation += 1

                seq_row = self._conn.execute(
                    """
                    SELECT COALESCE(MAX(surface_seq), -1) + 1 AS next_surface_seq
                    FROM surface_events
                    WHERE session_key = ?
                    """,
                    (key,),
                ).fetchone()
                surface_seq = int(seq_row["next_surface_seq"] if seq_row else 0)
                self._conn.execute(
                    """
                    INSERT INTO surface_events (
                        session_key, epoch_id, surface_seq, turn_id, iteration,
                        role, content, source_kind, status, projection_version,
                        operation_key, surface_op, replace_start, replace_end,
                        replace_generation, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        epoch_id,
                        surface_seq,
                        turn_id,
                        int(event.iteration),
                        role,
                        content_payload,
                        source_kind,
                        status,
                        int(event.projection_version),
                        operation_key,
                        surface_op,
                        int(event.replace_start) if event.replace_start is not None else None,
                        int(event.replace_end) if event.replace_end is not None else None,
                        replace_generation,
                        now,
                    ),
                )
                self._conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE key = ?",
                    (now, key),
                )
                self._conn.commit()
                row = self._conn.execute(
                    """
                    SELECT session_key, epoch_id, surface_seq, turn_id, iteration,
                           role, content, source_kind, status, projection_version,
                           operation_key, surface_op, replace_start, replace_end,
                           replace_generation, created_at
                    FROM surface_events
                    WHERE session_key = ? AND surface_seq = ?
                    """,
                    (key, surface_seq),
                ).fetchone()
            except Exception:
                self._conn.rollback()
                raise
        if row is None:
            raise RuntimeError(f"surface 事件写入后无法读取: {key}:{surface_seq}")
        return self._row_to_surface_event(row)

    def _fetch_surface_events_sync(
        self,
        session_key: str,
        *,
        epoch_id: str | None,
    ) -> list[dict[str, Any]]:
        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            if epoch_id is None:
                rows = self._conn.execute(
                    """
                    SELECT session_key, epoch_id, surface_seq, turn_id, iteration,
                           role, content, source_kind, status, projection_version,
                           operation_key, surface_op, replace_start, replace_end,
                           replace_generation, created_at
                    FROM surface_events
                    WHERE session_key = ?
                    ORDER BY surface_seq ASC
                    """,
                    (key,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT session_key, epoch_id, surface_seq, turn_id, iteration,
                           role, content, source_kind, status, projection_version,
                           operation_key, surface_op, replace_start, replace_end,
                           replace_generation, created_at
                    FROM surface_events
                    WHERE session_key = ? AND epoch_id = ?
                    ORDER BY surface_seq ASC
                    """,
                    (key, str(epoch_id).strip()),
                ).fetchall()
        return [self._row_to_surface_event(row) for row in rows]

    def _load_surface_sync(
        self,
        session_key: str,
        *,
        epoch_id: str | None,
    ) -> list[dict[str, Any]]:
        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            nodes = self._surface_projection_events_locked(key)
        if epoch_id is not None:
            # epoch 只影响缓存声明；历史消息仍需保留在当前模型上下文中。
            # 因此这里不按 epoch 丢弃旧节点，只允许调用方筛选原始事件。
            expected = str(epoch_id).strip()
            if expected and not any(str(node.get("epoch_id")) == expected for node in nodes):
                return [dict(node["message"]) for node in nodes]
        return [dict(node["message"]) for node in nodes]

    def _surface_projection_events_locked(
        self,
        session_key: str,
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT session_key, epoch_id, surface_seq, turn_id, iteration,
                   role, content, source_kind, status, projection_version,
                   operation_key, surface_op, replace_start, replace_end,
                   replace_generation, created_at
            FROM surface_events
            WHERE session_key = ?
            ORDER BY surface_seq ASC
            """,
            (session_key,),
        ).fetchall()
        nodes: list[dict[str, Any]] = []
        for row in rows:
            event = self._row_to_surface_event(row)
            if event["status"] not in _SURFACE_PROJECTABLE_STATUSES:
                continue
            if event["surface_op"] == "append":
                nodes.append(event)
                continue
            start = event["replace_start"]
            end = event["replace_end"]
            if start is None or end is None:
                raise ValueError("surface replace 事件缺少边界")
            start_index = next(
                (
                    index for index, node in enumerate(nodes)
                    if int(node["surface_seq"]) == int(start)
                ),
                None,
            )
            end_index = next(
                (
                    index for index, node in enumerate(nodes)
                    if int(node["surface_seq"]) == int(end)
                ),
                None,
            )
            if start_index is None or end_index is None or start_index > end_index:
                raise ValueError("surface replace 事件覆盖了不连续的当前节点")
            nodes[start_index:end_index + 1] = [event]
        return nodes

    @staticmethod
    def _surface_event_matches(
        current: dict[str, Any],
        *,
        epoch_id: str,
        turn_id: str,
        iteration: int,
        role: str,
        content: dict[str, Any],
        source_kind: str,
        status: str,
        projection_version: int,
        operation_key: str,
        surface_op: str,
        replace_start: int | None,
        replace_end: int | None,
    ) -> bool:
        return (
            current["epoch_id"] == epoch_id
            and current["turn_id"] == turn_id
            and int(current["iteration"]) == iteration
            and current["role"] == role
            and current["message"] == content
            and current["source_kind"] == source_kind
            and current["status"] == status
            and int(current["projection_version"]) == projection_version
            and current["operation_key"] == operation_key
            and current["surface_op"] == surface_op
            and current["replace_start"] == replace_start
            and current["replace_end"] == replace_end
        )

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
        with self._lock:
            self._ensure_open()
            incoming = dict(metadata or {})
            existing_row = self._conn.execute(
                "SELECT metadata FROM sessions WHERE key = ?", (key,)
            ).fetchone()
            existing = _load_json_object(existing_row["metadata"]) if existing_row else {}
            # 计量快照由独立 writer 更新，Session 缓存可能尚未感知；普通
            # metadata 保存不得用旧快照覆盖这个保留键。
            if "context_usage" not in incoming and "context_usage" in existing:
                incoming["context_usage"] = existing["context_usage"]
            payload = json.dumps(incoming, ensure_ascii=False)
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
        limit: int | None,
    ) -> list[dict[str, Any]]:
        key = self._validate_session_key(session_key)
        safe_limit = None if limit is None else max(0, int(limit))
        if safe_limit == 0:
            return []

        with self._lock:
            self._ensure_open()
            cursor = self._active_message_boundary_locked(key)
            if safe_limit is None:
                rows = self._conn.execute(
                    f"""
                    SELECT {_MESSAGE_COLUMNS}
                    FROM messages
                    WHERE session_key = ? AND seq >= ?
                    ORDER BY seq DESC
                    """,
                    (key, cursor),
                ).fetchall()
            else:
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

    def get_active_message_boundary(self, session_key: str) -> int:
        """返回 active generation 覆盖到的消息 seq；无 ledger 时兼容旧 cursor。"""

        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            return self._active_message_boundary_locked(key)

    def _active_message_boundary_locked(self, key: str) -> int:
        """在持有 SQLite 锁时解析 generation -> message seq 的唯一映射。"""

        meta = self._conn.execute(
            "SELECT last_consolidated FROM sessions WHERE key = ?",
            (key,),
        ).fetchone()
        generation = max(0, int(meta["last_consolidated"] or 0)) if meta else 0
        if generation <= 0:
            return generation
        checkpoint = self._conn.execute(
            """
            SELECT consolidated_through_seq
            FROM session_compactions
            WHERE session_key = ? AND generation = ? AND invalidated_at IS NULL
            """,
            (key, generation),
        ).fetchone()
        # 旧版本或人工修复可能只更新了 sessions；在对应 ledger 缺失时保留
        # legacy cursor 行为，避免把已归档消息重新暴露给模型。
        return int(checkpoint["consolidated_through_seq"]) if checkpoint else generation

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

    def _get_active_compaction_sync(self, session_key: str) -> SessionCompaction | None:
        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                """
                SELECT c.*
                FROM sessions s
                JOIN session_compactions c
                  ON c.session_key = s.key
                 AND c.generation = s.last_consolidated
                WHERE s.key = ? AND c.invalidated_at IS NULL
                """,
                (key,),
            ).fetchone()
        return self._row_to_compaction(row) if row is not None else None

    def _get_compaction_sync(
        self,
        session_key: str,
        generation: int,
    ) -> SessionCompaction | None:
        key = self._validate_session_key(session_key)
        safe_generation = int(generation)
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT * FROM session_compactions WHERE session_key = ? AND generation = ?",
                (key, safe_generation),
            ).fetchone()
        return self._row_to_compaction(row) if row is not None else None

    def _next_compaction_generation_sync(self, session_key: str) -> int:
        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT COALESCE(MAX(generation), 0) AS generation FROM session_compactions WHERE session_key = ?",
                (key,),
            ).fetchone()
        return int((row["generation"] if row else 0) or 0) + 1

    def _prepare_compaction_sync(
        self,
        prepare: SessionCompactionPrepare,
    ) -> SessionCompactionPrepare:
        key = self._validate_session_key(prepare.session_key)
        if prepare.generation < 1:
            raise ValueError("compaction generation 必须是正整数")
        if prepare.consolidated_through_seq < prepare.source_from_seq:
            raise ValueError("compaction seq 边界无效")
        payload = (
            json.dumps(prepare.source_message_ids, ensure_ascii=False, separators=(",", ":")),
            json.dumps(prepare.selected_source_messages, ensure_ascii=False, separators=(",", ":")),
            json.dumps(prepare.retained_tail, ensure_ascii=False, separators=(",", ":")),
        )
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT * FROM session_compaction_prepares WHERE session_key = ? AND generation = ?",
                    (key, prepare.generation),
                ).fetchone()
                if existing is not None:
                    stored = self._row_to_prepare(existing)
                    if stored != prepare:
                        raise ValueError(
                            "同一 compaction generation 的 prepare 不可变字段发生漂移"
                        )
                    self._conn.commit()
                    return stored
                session = self._conn.execute(
                    "SELECT created_at FROM sessions WHERE key = ?",
                    (key,),
                ).fetchone()
                if session is None:
                    raise ValueError(f"compaction session 不存在: {key}")
                if str(session["created_at"]) != str(prepare.session_created_at):
                    raise ValueError("compaction session incarnation 不匹配")
                self._conn.execute(
                    """
                    INSERT INTO session_compaction_prepares(
                        session_key, session_created_at, generation, parent_generation,
                        source_ref, source_plan_digest, source_mutation_digest,
                        source_from_seq, consolidated_through_seq,
                        source_message_ids_json, selected_source_messages_json,
                        retained_tail_json, prepared_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        prepare.session_created_at,
                        prepare.generation,
                        prepare.parent_generation,
                        prepare.source_ref,
                        prepare.source_plan_digest,
                        prepare.source_mutation_digest,
                        prepare.source_from_seq,
                        prepare.consolidated_through_seq,
                        *payload,
                        prepare.prepared_at,
                    ),
                )
                self._conn.commit()
                return prepare
            except Exception:
                self._conn.rollback()
                raise

    def _commit_compaction_sync(
        self,
        checkpoint: SessionCompaction,
    ) -> SessionCompaction:
        key = self._validate_session_key(checkpoint.session_key)
        if checkpoint.generation < 1:
            raise ValueError("compaction generation 必须是正整数")
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                session = self._conn.execute(
                    "SELECT created_at, last_consolidated FROM sessions WHERE key = ?",
                    (key,),
                ).fetchone()
                if session is None:
                    raise ValueError(f"compaction session 不存在: {key}")
                if str(session["created_at"]) != str(checkpoint.session_created_at):
                    raise ValueError("compaction session incarnation 不匹配")
                current_generation = int(session["last_consolidated"] or 0)
                if current_generation > checkpoint.generation:
                    raise ValueError("compaction generation 不能回退")
                prepare_row = self._conn.execute(
                    "SELECT * FROM session_compaction_prepares WHERE session_key = ? AND generation = ?",
                    (key, checkpoint.generation),
                ).fetchone()
                if prepare_row is None:
                    raise ValueError("提交 checkpoint 前必须先写入 prepare")
                prepare = self._row_to_prepare(prepare_row)
                if not _prepare_matches_checkpoint(prepare, checkpoint):
                    raise ValueError("checkpoint 与 prepare 的来源契约不一致")
                existing_row = self._conn.execute(
                    "SELECT * FROM session_compactions WHERE session_key = ? AND generation = ?",
                    (key, checkpoint.generation),
                ).fetchone()
                if existing_row is not None:
                    existing = self._row_to_compaction(existing_row)
                    if existing != checkpoint:
                        raise ValueError("同一 compaction generation 的 checkpoint 不可变字段发生漂移")
                    self._conn.commit()
                    return existing
                self._conn.execute(
                    """
                    INSERT INTO session_compactions(
                        session_key, session_created_at, generation, parent_generation,
                        created_at, trigger, summary_format_version, summary,
                        source_ref, source_plan_digest, source_mutation_digest,
                        source_from_seq, consolidated_through_seq,
                        source_message_ids_json, selected_source_messages_json,
                        retained_tail_json, model_runtime_id, model, context_window,
                        threshold_tokens, hard_input_tokens, keep_recent_tokens,
                        tokens_before, tokens_after, summary_usage_json,
                        invalidated_at, invalidated_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        checkpoint.session_created_at,
                        checkpoint.generation,
                        checkpoint.parent_generation,
                        checkpoint.created_at,
                        checkpoint.trigger,
                        checkpoint.summary_format_version,
                        checkpoint.summary,
                        checkpoint.source_ref,
                        checkpoint.source_plan_digest,
                        checkpoint.source_mutation_digest,
                        checkpoint.source_from_seq,
                        checkpoint.consolidated_through_seq,
                        json.dumps(checkpoint.source_message_ids, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(checkpoint.selected_source_messages, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(checkpoint.retained_tail, ensure_ascii=False, separators=(",", ":")),
                        checkpoint.model_runtime_id,
                        checkpoint.model,
                        checkpoint.context_window,
                        checkpoint.threshold_tokens,
                        checkpoint.hard_input_tokens,
                        checkpoint.keep_recent_tokens,
                        checkpoint.tokens_before,
                        checkpoint.tokens_after,
                        json.dumps(checkpoint.summary_usage, ensure_ascii=False, separators=(",", ":")),
                        checkpoint.invalidated_at,
                        checkpoint.invalidated_reason,
                    ),
                )
                self._conn.execute(
                    "UPDATE sessions SET last_consolidated = ?, updated_at = ? WHERE key = ?",
                    (checkpoint.generation, checkpoint.created_at, key),
                )
                self._conn.commit()
                return checkpoint
            except Exception:
                self._conn.rollback()
                raise

    def _close_sync(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def _save_context_usage_sync(self, session_key: str, snapshot: dict[str, Any]) -> None:
        key = self._validate_session_key(session_key)
        if not isinstance(snapshot, dict):
            raise TypeError("context usage snapshot 必须是对象")
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT metadata FROM sessions WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                raise ValueError(f"context usage session 不存在: {key}")
            metadata = _load_json_object(row["metadata"])
            # 只更新保留键，标题和其他业务 metadata 由各自调用方继续拥有。
            metadata["context_usage"] = dict(snapshot)
            self._conn.execute(
                """
                UPDATE sessions SET metadata = ?, updated_at = ? WHERE key = ?
                """,
                (json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), default=str), _now_iso(), key),
            )
            self._conn.commit()

    def _get_context_usage_sync(self, session_key: str) -> dict[str, Any] | None:
        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT metadata FROM sessions WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        value = _load_json_object(row["metadata"]).get("context_usage")
        return dict(value) if isinstance(value, dict) else None

    def _save_session_usage_sync(
        self,
        session_key: str,
        turn_id: str,
        iteration: int,
        usage: dict[str, Any],
    ) -> dict[str, Any]:
        key = self._validate_session_key(session_key)
        clean_turn = str(turn_id or "").strip()
        if not clean_turn:
            raise ValueError("session usage turn_id 不能为空")
        if not isinstance(usage, dict):
            raise TypeError("session usage 必须是对象")
        values = {
            name: max(0, int(usage.get(name) or 0))
            for name in (
                "uncached_input_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "output_tokens",
            )
        }
        safe_iteration = max(1, int(iteration))
        with self._lock:
            self._ensure_open()
            if self._conn.execute("SELECT 1 FROM sessions WHERE key = ?", (key,)).fetchone() is None:
                raise ValueError(f"session usage session 不存在: {key}")
            self._conn.execute(
                """
                INSERT INTO session_usage(
                    session_key, turn_id, iteration, uncached_input_tokens,
                    cache_read_tokens, cache_write_tokens, output_tokens, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_key, turn_id, iteration) DO UPDATE SET
                    uncached_input_tokens = excluded.uncached_input_tokens,
                    cache_read_tokens = excluded.cache_read_tokens,
                    cache_write_tokens = excluded.cache_write_tokens,
                    output_tokens = excluded.output_tokens,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    clean_turn,
                    safe_iteration,
                    values["uncached_input_tokens"],
                    values["cache_read_tokens"],
                    values["cache_write_tokens"],
                    values["output_tokens"],
                    _now_iso(),
                ),
            )
            self._conn.commit()
            return self._aggregate_session_usage_locked(key)

    def _get_session_usage_sync(self, session_key: str) -> dict[str, Any] | None:
        key = self._validate_session_key(session_key)
        with self._lock:
            self._ensure_open()
            if self._conn.execute("SELECT 1 FROM sessions WHERE key = ?", (key,)).fetchone() is None:
                return None
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM session_usage WHERE session_key = ?", (key,)
            ).fetchone()
            if row is None or int(row["count"] or 0) == 0:
                return None
            return self._aggregate_session_usage_locked(key)

    def _aggregate_session_usage_locked(self, key: str) -> dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT
                COALESCE(SUM(uncached_input_tokens), 0) AS uncached,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read,
                COALESCE(SUM(cache_write_tokens), 0) AS cache_write,
                COALESCE(SUM(output_tokens), 0) AS output
            FROM session_usage WHERE session_key = ?
            """,
            (key,),
        ).fetchone()
        uncached = int(row["uncached"] or 0)
        cache_read = int(row["cache_read"] or 0)
        cache_write = int(row["cache_write"] or 0)
        output = int(row["output"] or 0)
        total_input = uncached + cache_read + cache_write
        # 输入总量包含缓存读写；命中率只有存在输入时才有定义。
        return {
            "total_uncached_input_tokens": uncached,
            "total_cache_read_tokens": cache_read,
            "total_cache_write_tokens": cache_write,
            "total_input_tokens": total_input,
            "cache_hit_rate": (cache_read / total_input if total_input else None),
            "total_output_tokens": output,
        }

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

        interrupted = message.get("status") == "interrupted"
        result: list[dict[str, Any]] = []
        for group in message["tool_chain"]:
            calls = group.get("calls") or []
            if interrupted:
                # 数据库保留全部已发起工具供前端恢复；模型历史也保留调用，
                # 对没有完整结果的调用补确定性占位，避免协议链断裂。
                calls = [
                    call for call in calls
                    if isinstance(call, dict)
                    and str(call.get("status") or "")
                    in {"ok", "completed", "error", "running", "interrupted"}
                ]
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
                interrupted_call = interrupted and str(call.get("status") or "") in {
                    "running", "interrupted"
                }
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("call_id", "")),
                        "content": (
                            INTERRUPTED_TOOL_RESULT_CONTENT
                            if interrupted_call
                            else str(call.get("result", ""))
                        ),
                    }
                )

        final_message: dict[str, Any] = {
            "role": "assistant",
            "content": message["content"],
        }
        if not interrupted:
            # 优先使用终答轮单独思考（``extra.final_reasoning_content``），
            # 避免历史里工具决策思考在终答 ``reasoning_content``（拼接版）里
            # 重复出现。旧数据没有此字段，fallback 到 ``reasoning_content``
            # （拼接版），行为与改造前一致；DeepSeek 协议只要求字段存在即可。
            final_reasoning = (
                message.get("final_reasoning_content")
                or message.get("reasoning_content", "")
            )
            if final_reasoning:
                final_message["reasoning_content"] = final_reasoning
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
        # 此处同时把 ``final_reasoning_content`` 等扩展字段从 extra 挂回 message
        # 顶层，供下游 ``_message_to_history`` 直接 ``message.get(...)`` 使用。
        for key, value in extra.items():
            if key not in message and key != "metadata":
                message[key] = value
        return message

    @staticmethod
    def _row_to_surface_event(row: sqlite3.Row) -> dict[str, Any]:
        try:
            message = json.loads(str(row["content"]))
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError(
                f"surface 事件内容不是合法 JSON: {row['session_key']}:{row['surface_seq']}"
            ) from error
        if not isinstance(message, dict):
            raise ValueError(
                f"surface 事件内容必须是消息对象: {row['session_key']}:{row['surface_seq']}"
            )
        return {
            "session_key": str(row["session_key"]),
            "epoch_id": str(row["epoch_id"]),
            "surface_seq": int(row["surface_seq"]),
            "turn_id": str(row["turn_id"]),
            "iteration": int(row["iteration"]),
            "role": str(row["role"]),
            "message": message,
            "source_kind": str(row["source_kind"]),
            "status": str(row["status"]),
            "projection_version": int(row["projection_version"]),
            "operation_key": str(row["operation_key"]),
            "surface_op": str(row["surface_op"]),
            "replace_start": (
                int(row["replace_start"])
                if row["replace_start"] is not None
                else None
            ),
            "replace_end": (
                int(row["replace_end"])
                if row["replace_end"] is not None
                else None
            ),
            "replace_generation": int(row["replace_generation"] or 0),
            "created_at": str(row["created_at"]),
        }

    @staticmethod
    def _row_to_prepare(row: sqlite3.Row) -> SessionCompactionPrepare:
        return SessionCompactionPrepare(
            session_key=str(row["session_key"]),
            session_created_at=str(row["session_created_at"]),
            generation=int(row["generation"]),
            parent_generation=int(row["parent_generation"]),
            source_ref=str(row["source_ref"]),
            source_plan_digest=str(row["source_plan_digest"]),
            source_mutation_digest=str(row["source_mutation_digest"]),
            source_from_seq=int(row["source_from_seq"]),
            consolidated_through_seq=int(row["consolidated_through_seq"]),
            source_message_ids=_load_json_string_list(row["source_message_ids_json"]),
            selected_source_messages=_load_json_dict_list(row["selected_source_messages_json"]),
            retained_tail=_load_json_dict_list(row["retained_tail_json"]),
            prepared_at=str(row["prepared_at"]),
        )

    @staticmethod
    def _row_to_compaction(row: sqlite3.Row) -> SessionCompaction:
        return SessionCompaction(
            session_key=str(row["session_key"]),
            session_created_at=str(row["session_created_at"]),
            generation=int(row["generation"]),
            parent_generation=int(row["parent_generation"]),
            created_at=str(row["created_at"]),
            trigger=str(row["trigger"]),
            summary_format_version=int(row["summary_format_version"]),
            summary=str(row["summary"]),
            source_ref=str(row["source_ref"]),
            source_plan_digest=str(row["source_plan_digest"]),
            source_mutation_digest=str(row["source_mutation_digest"]),
            source_from_seq=int(row["source_from_seq"]),
            consolidated_through_seq=int(row["consolidated_through_seq"]),
            source_message_ids=_load_json_string_list(row["source_message_ids_json"]),
            selected_source_messages=_load_json_dict_list(row["selected_source_messages_json"]),
            retained_tail=_load_json_dict_list(row["retained_tail_json"]),
            model_runtime_id=str(row["model_runtime_id"]),
            model=str(row["model"]),
            context_window=int(row["context_window"]),
            threshold_tokens=int(row["threshold_tokens"]),
            hard_input_tokens=int(row["hard_input_tokens"]),
            keep_recent_tokens=int(row["keep_recent_tokens"]),
            tokens_before=int(row["tokens_before"]),
            tokens_after=int(row["tokens_after"]),
            summary_usage=_load_json_object(row["summary_usage_json"]),
            invalidated_at=(str(row["invalidated_at"]) if row["invalidated_at"] else None),
            invalidated_reason=(str(row["invalidated_reason"]) if row["invalidated_reason"] else None),
        )

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


def _load_json_string_list(value: object) -> list[str]:
    if not value:
        return []
    try:
        loaded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


def _load_json_dict_list(value: object) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        loaded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return []
    return [dict(item) for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else []


def _prepare_matches_checkpoint(
    prepare: SessionCompactionPrepare,
    checkpoint: SessionCompaction,
) -> bool:
    """提交前只比较来源契约，允许 checkpoint 增加模型和 token 审计字段。"""

    return (
        prepare.session_key == checkpoint.session_key
        and prepare.session_created_at == checkpoint.session_created_at
        and prepare.generation == checkpoint.generation
        and prepare.parent_generation == checkpoint.parent_generation
        and prepare.source_ref == checkpoint.source_ref
        and prepare.source_plan_digest == checkpoint.source_plan_digest
        and prepare.source_mutation_digest == checkpoint.source_mutation_digest
        and prepare.source_from_seq == checkpoint.source_from_seq
        and prepare.consolidated_through_seq == checkpoint.consolidated_through_seq
        and prepare.source_message_ids == checkpoint.source_message_ids
        and prepare.selected_source_messages == checkpoint.selected_source_messages
        and prepare.retained_tail == checkpoint.retained_tail
    )


__all__ = [
    "NewMessage",
    "NewSurfaceEvent",
    "SessionCompaction",
    "SessionCompactionPrepare",
    "SessionStore",
]
