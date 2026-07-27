"""主动设置、定时任务和投递状态的独立 SQLite 存储。"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from proactive.models import ProactiveNotification, ProactiveState, ScheduledJob, SessionProactiveSettings


class ProactiveStore:
    """持有主动域数据库；独立连接避免改变普通 Session 的提交事务。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        """幂等创建主动域表，服务升级时可直接复用已有 workspace。"""

        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS proactive_settings (
                    session_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scheduled_jobs (
                    id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    fire_at TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due
                    ON scheduled_jobs(enabled, fire_at);
                CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_session
                    ON scheduled_jobs(session_key, created_at);
                CREATE TABLE IF NOT EXISTS proactive_state (
                    session_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proactive_deliveries (
                    delivery_key TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    delivered_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proactive_notifications (
                    id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    delivered_at TEXT,
                    seen_at TEXT,
                    recurring INTEGER NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_proactive_notifications_session
                    ON proactive_notifications(session_key, generated_at, id);
                CREATE INDEX IF NOT EXISTS idx_proactive_notifications_pending
                    ON proactive_notifications(session_key, status, generated_at);
                """
            )
            # 旧版本会把已完成或被勿扰策略跳过的一次性任务留在列表中。
            # 新版本的通知已独立持久化，这些任务不再承担展示职责，可安全清理。
            self._conn.execute(
                "DELETE FROM scheduled_jobs WHERE status IN ('completed', 'skipped')"
            )
            self._conn.commit()

    def get_settings(self, session_key: str) -> SessionProactiveSettings:
        """返回会话设置；尚未配置的会话保持主动能力关闭。"""

        key = _session_key(session_key)
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT payload, created_at, updated_at FROM proactive_settings WHERE session_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return SessionProactiveSettings(session_key=key)
        payload = json.loads(str(row["payload"]))
        payload.update(created_at=str(row["created_at"]), updated_at=str(row["updated_at"]))
        return SessionProactiveSettings(**payload)

    def upsert_settings(self, settings: SessionProactiveSettings) -> SessionProactiveSettings:
        """原子保存用户语义设置，避免并发 API 更新产生半份配置。"""

        validated = _validated_settings(settings)
        now = datetime.now(timezone.utc).isoformat()
        existing = self.get_settings(validated.session_key)
        created_at = existing.created_at or now
        validated.created_at = created_at
        validated.updated_at = now
        payload = asdict(validated)
        payload.pop("created_at", None)
        payload.pop("updated_at", None)
        with self._lock:
            self._ensure_open()
            self._conn.execute(
                """
                INSERT INTO proactive_settings(session_key, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (validated.session_key, json.dumps(payload, ensure_ascii=False), created_at, now),
            )
            self._conn.commit()
        return validated

    def list_enabled_conversations(self) -> list[SessionProactiveSettings]:
        """列出开启主动聊天的会话，供全局 loop 选择候选目标。"""

        with self._lock:
            self._ensure_open()
            rows = self._conn.execute("SELECT session_key FROM proactive_settings ORDER BY session_key").fetchall()
        return [item for row in rows if (item := self.get_settings(str(row["session_key"]))).conversation_enabled]

    def add_job(self, job: ScheduledJob) -> ScheduledJob:
        """持久化任务；同一 ID 重复创建会被数据库唯一约束拒绝。"""

        _validate_job(job)
        payload = _job_payload(job)
        with self._lock:
            self._ensure_open()
            self._conn.execute(
                "INSERT INTO scheduled_jobs(id, session_key, payload, fire_at, enabled, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job.id, job.session_key, json.dumps(payload, ensure_ascii=False), job.fire_at.astimezone(timezone.utc).isoformat(), int(job.enabled), job.status, job.created_at.astimezone(timezone.utc).isoformat()),
            )
            self._conn.commit()
        return job

    def update_job(self, job: ScheduledJob) -> None:
        """保存执行后的触发时间、次数和状态变化。"""

        _validate_job(job)
        with self._lock:
            self._ensure_open()
            self._conn.execute(
                "UPDATE scheduled_jobs SET payload=?, fire_at=?, enabled=?, status=? WHERE id=?",
                (json.dumps(_job_payload(job), ensure_ascii=False), job.fire_at.astimezone(timezone.utc).isoformat(), int(job.enabled), job.status, job.id),
            )
            self._conn.commit()

    def list_jobs(self, session_key: str | None = None, *, enabled_only: bool = False) -> list[ScheduledJob]:
        """按会话列出任务；Web 管理接口必须传 session_key 隔离数据。"""

        clauses: list[str] = []
        values: list[object] = []
        if session_key is not None:
            clauses.append("session_key = ?")
            values.append(_session_key(session_key))
        if enabled_only:
            clauses.append("enabled = 1")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                f"SELECT payload FROM scheduled_jobs{where} ORDER BY fire_at, id", values
            ).fetchall()
        return [_job_from_payload(json.loads(str(row["payload"]))) for row in rows]

    def get_job(self, job_id: str) -> ScheduledJob | None:
        """按稳定 ID 读取任务。"""

        with self._lock:
            self._ensure_open()
            row = self._conn.execute("SELECT payload FROM scheduled_jobs WHERE id = ?", (str(job_id),)).fetchone()
        return _job_from_payload(json.loads(str(row["payload"]))) if row else None

    def delete_job(self, job_id: str, *, session_key: str | None = None) -> bool:
        """删除任务，并可要求会话归属匹配以阻止跨会话取消。"""

        sql = "DELETE FROM scheduled_jobs WHERE id = ?"
        values: list[object] = [str(job_id)]
        if session_key is not None:
            sql += " AND session_key = ?"
            values.append(_session_key(session_key))
        with self._lock:
            self._ensure_open()
            cursor = self._conn.execute(sql, values)
            self._conn.commit()
            return cursor.rowcount > 0

    def get_state(self, session_key: str) -> ProactiveState:
        """读取会话主动状态；不存在时返回空状态。"""

        key = _session_key(session_key)
        with self._lock:
            self._ensure_open()
            row = self._conn.execute("SELECT payload FROM proactive_state WHERE session_key = ?", (key,)).fetchone()
        return ProactiveState(**json.loads(str(row["payload"]))) if row else ProactiveState(session_key=key)

    def put_state(self, state: ProactiveState) -> None:
        """原子保存节流和诊断状态。"""

        key = _session_key(state.session_key)
        state.session_key = key
        with self._lock:
            self._ensure_open()
            self._conn.execute(
                "INSERT INTO proactive_state(session_key, payload) VALUES (?, ?) ON CONFLICT(session_key) DO UPDATE SET payload=excluded.payload",
                (key, json.dumps(asdict(state), ensure_ascii=False)),
            )
            self._conn.commit()

    def reserve_delivery(self, delivery_key: str, session_key: str, message_id: str) -> bool:
        """预留幂等键；返回 False 表示该主动消息已经提交过。"""

        try:
            with self._lock:
                self._ensure_open()
                self._conn.execute(
                    "INSERT INTO proactive_deliveries(delivery_key, session_key, message_id, delivered_at) VALUES (?, ?, ?, ?)",
                    (str(delivery_key), _session_key(session_key), str(message_id), datetime.now(timezone.utc).isoformat()),
                )
                self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def release_delivery(self, delivery_key: str) -> None:
        """主动消息提交失败时释放预留键，使后续 tick 可以安全重试。"""

        with self._lock:
            self._ensure_open()
            self._conn.execute(
                "DELETE FROM proactive_deliveries WHERE delivery_key = ?",
                (str(delivery_key),),
            )
            self._conn.commit()

    def enqueue_notification(self, notification: ProactiveNotification) -> ProactiveNotification:
        """保存待投递通知；同一周期任务只覆盖尚未投递的最新一条。

        周期合并不能覆盖已经送达的记录，否则用户刷新页面后会看到旧消息内容
        被悄悄改写。一次性任务始终插入独立记录，保证不同提醒互不吞并。
        """

        item = _validated_notification(notification)
        with self._lock:
            self._ensure_open()
            existing = None
            if item.recurring:
                existing = self._conn.execute(
                    "SELECT id FROM proactive_notifications WHERE session_key=? AND source_id=? AND status='pending' ORDER BY generated_at DESC LIMIT 1",
                    (item.session_key, item.source_id),
                ).fetchone()
            if existing is not None:
                item.id = str(existing["id"])
                self._conn.execute(
                    "UPDATE proactive_notifications SET source=?, content=?, scheduled_at=?, generated_at=?, recurring=1 WHERE id=?",
                    (item.source, item.content, item.scheduled_at.astimezone(timezone.utc).isoformat(), item.generated_at.astimezone(timezone.utc).isoformat(), item.id),
                )
            else:
                self._conn.execute(
                    """INSERT INTO proactive_notifications(
                        id, session_key, source, source_id, content, scheduled_at,
                        generated_at, delivered_at, seen_at, recurring, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, 'pending')""",
                    (item.id, item.session_key, item.source, item.source_id, item.content, item.scheduled_at.astimezone(timezone.utc).isoformat(), item.generated_at.astimezone(timezone.utc).isoformat(), int(item.recurring)),
                )
            self._conn.commit()
        return item

    def list_notifications(self, session_key: str, *, pending_only: bool = False, limit: int = 500) -> list[ProactiveNotification]:
        """按生成时间返回独立通知；API 必须用 session_key 隔离会话。"""

        key = _session_key(session_key)
        safe_limit = max(1, min(int(limit), 500))
        status_clause = " AND status = 'pending'" if pending_only else ""
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                f"SELECT * FROM proactive_notifications WHERE session_key=?{status_clause} ORDER BY generated_at DESC, id DESC LIMIT ?",
                (key, safe_limit),
            ).fetchall()
        return [_notification_from_row(row) for row in reversed(rows)]

    def mark_notification_delivered(self, notification_id: str, *, seen: bool = False) -> bool:
        """记录浏览器已收到通知；重复确认不会改变首次投递时间。"""

        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._ensure_open()
            cursor = self._conn.execute(
                """UPDATE proactive_notifications
                SET status=?, delivered_at=COALESCE(delivered_at, ?),
                    seen_at=CASE WHEN ? THEN COALESCE(seen_at, ?) ELSE seen_at END
                WHERE id=?""",
                ("seen" if seen else "delivered", now, int(seen), now, str(notification_id)),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def close(self) -> None:
        """幂等关闭数据库连接。"""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("ProactiveStore 已关闭")


def _session_key(value: str) -> str:
    key = str(value).strip()
    if not key or ":" not in key:
        raise ValueError("session_key 无效")
    return key


def _validated_settings(settings: SessionProactiveSettings) -> SessionProactiveSettings:
    data = asdict(settings)
    data["session_key"] = _session_key(settings.session_key)
    if settings.activity_level not in {"restrained", "balanced", "active", "dev_verify"}:
        raise ValueError("主动程度无效")
    if settings.reminder_quiet_policy not in {"delay", "send", "skip"}:
        raise ValueError("提醒勿扰策略无效")
    if not 1 <= int(settings.min_conversation_interval_hours) <= 168:
        raise ValueError("最短聊天间隔必须为 1-168 小时")
    if not 1 <= int(settings.daily_conversation_limit) <= 20:
        raise ValueError("每日最多必须为 1-20 次")
    for field_name in ("quiet_start", "quiet_end"):
        try:
            datetime.strptime(str(data[field_name]), "%H:%M")
        except ValueError as error:
            raise ValueError(f"{field_name} 必须为 HH:MM") from error
    return SessionProactiveSettings(**data)


def _validate_job(job: ScheduledJob) -> None:
    job.session_key = _session_key(job.session_key)
    if job.tier not in {"instant", "soft"} or job.trigger not in {"at", "after", "every"}:
        raise ValueError("定时任务类型无效")
    if job.fire_at.tzinfo is None:
        raise ValueError("fire_at 必须包含时区")
    if job.tier == "instant" and not job.message.strip():
        raise ValueError("instant 任务必须包含 message")
    if job.tier == "soft" and not job.prompt.strip():
        raise ValueError("soft 任务必须包含 prompt")


def _job_payload(job: ScheduledJob) -> dict[str, object]:
    payload = asdict(job)
    payload["fire_at"] = job.fire_at.astimezone(timezone.utc).isoformat()
    payload["created_at"] = job.created_at.astimezone(timezone.utc).isoformat()
    payload["scheduled_for"] = job.scheduled_for.astimezone(timezone.utc).isoformat() if job.scheduled_for else None
    return payload


def _job_from_payload(payload: dict[str, object]) -> ScheduledJob:
    data = dict(payload)
    data["fire_at"] = datetime.fromisoformat(str(data["fire_at"]))
    data["created_at"] = datetime.fromisoformat(str(data["created_at"]))
    if data.get("scheduled_for"):
        data["scheduled_for"] = datetime.fromisoformat(str(data["scheduled_for"]))
    return ScheduledJob(**data)  # type: ignore[arg-type]


def _validated_notification(notification: ProactiveNotification) -> ProactiveNotification:
    notification.session_key = _session_key(notification.session_key)
    notification.content = str(notification.content).strip()
    if not notification.content:
        raise ValueError("通知内容不能为空")
    if notification.source not in {"scheduled_reminder", "scheduled_soft"}:
        raise ValueError("通知来源无效")
    if notification.scheduled_at.tzinfo is None or notification.generated_at.tzinfo is None:
        raise ValueError("通知时间必须包含时区")
    return notification


def _notification_from_row(row: sqlite3.Row) -> ProactiveNotification:
    return ProactiveNotification(
        id=str(row["id"]), session_key=str(row["session_key"]),
        source=str(row["source"]), source_id=str(row["source_id"]),  # type: ignore[arg-type]
        content=str(row["content"]), scheduled_at=datetime.fromisoformat(str(row["scheduled_at"])),
        generated_at=datetime.fromisoformat(str(row["generated_at"])),
        delivered_at=datetime.fromisoformat(str(row["delivered_at"])) if row["delivered_at"] else None,
        seen_at=datetime.fromisoformat(str(row["seen_at"])) if row["seen_at"] else None,
        recurring=bool(row["recurring"]), status=str(row["status"]),  # type: ignore[arg-type]
    )


__all__ = ["ProactiveStore"]
