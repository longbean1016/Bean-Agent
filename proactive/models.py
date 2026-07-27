"""主动功能跨存储、调度和 Web API 使用的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

ActivityLevel = Literal["restrained", "balanced", "active", "dev_verify"]
QuietPolicy = Literal["delay", "send", "skip"]
ScheduleTier = Literal["instant", "soft"]
ScheduleTrigger = Literal["at", "after", "every"]
NotificationStatus = Literal["pending", "delivered", "seen"]


@dataclass(slots=True)
class SessionProactiveSettings:
    """用户可理解的会话级设置，不持久化底层算法展开值。"""

    session_key: str
    reminders_enabled: bool = False
    reminder_quiet_policy: QuietPolicy = "delay"
    conversation_enabled: bool = False
    activity_level: ActivityLevel = "restrained"
    min_conversation_interval_hours: int = 8
    daily_conversation_limit: int = 2
    quiet_hours_enabled: bool = True
    quiet_start: str = "23:00"
    quiet_end: str = "08:00"
    timezone: str = "Asia/Shanghai"
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class ScheduledJob:
    """一条可恢复的定时提醒或隔离 AI 定时任务。"""

    session_key: str
    trigger: ScheduleTrigger
    tier: ScheduleTier
    fire_at: datetime
    message: str = ""
    prompt: str = ""
    name: str = ""
    timezone: str = "Asia/Shanghai"
    interval_seconds: int | None = None
    cron_expr: str = ""
    scheduled_for: datetime | None = None
    enabled: bool = True
    run_count: int = 0
    status: str = "pending"
    last_error: str = ""
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class ProactiveState:
    """每个会话的主动聊天节流、诊断与最近投递状态。"""

    session_key: str
    last_checked_at: str = ""
    last_delivered_at: str = ""
    daily_date: str = ""
    daily_count: int = 0
    last_skip_reason: str = ""
    recent_messages: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProactiveNotification:
    """不进入 Session 的提醒通知，由 Web 消息层独立展示和补发。"""

    session_key: str
    source: Literal["scheduled_reminder", "scheduled_soft"]
    source_id: str
    content: str
    scheduled_at: datetime
    recurring: bool = False
    status: NotificationStatus = "pending"
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    delivered_at: datetime | None = None
    seen_at: datetime | None = None
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """主动消息提交结果；重复 delivery_key 不会再次写入 Session。"""

    delivered: bool
    message_id: str = ""
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "ActivityLevel",
    "DeliveryResult",
    "NotificationStatus",
    "ProactiveNotification",
    "ProactiveState",
    "QuietPolicy",
    "ScheduledJob",
    "ScheduleTier",
    "ScheduleTrigger",
    "SessionProactiveSettings",
]
