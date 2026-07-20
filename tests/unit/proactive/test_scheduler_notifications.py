"""Scheduler 转换为独立通知后的任务生命周期测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from proactive.models import ScheduledJob, SessionProactiveSettings
from proactive.scheduler import SchedulerService
from proactive.store import ProactiveStore


class _Delivery:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def deliver(self, **kwargs):
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_one_shot_job_is_deleted_after_notification_created(tmp_path) -> None:
    now = datetime(2026, 7, 20, 1, tzinfo=timezone.utc)
    store = ProactiveStore(tmp_path / "proactive.db")
    store.upsert_settings(SessionProactiveSettings(session_key="web:a", reminders_enabled=True))
    job = store.add_job(ScheduledJob(
        session_key="web:a", trigger="at", tier="instant", fire_at=now, message="提醒",
    ))
    delivery = _Delivery()
    scheduler = SchedulerService(store, delivery, now_fn=lambda: now)

    await scheduler.run_due_once()

    assert store.get_job(job.id) is None
    assert delivery.calls[0]["scheduled_at"] == now
    await scheduler.close()
    store.close()


@pytest.mark.asyncio
async def test_overdue_recurring_job_uses_latest_occurrence(tmp_path) -> None:
    first = datetime(2026, 7, 20, 1, tzinfo=timezone.utc)
    now = first + timedelta(hours=5, minutes=10)
    store = ProactiveStore(tmp_path / "proactive.db")
    store.upsert_settings(SessionProactiveSettings(session_key="web:a", reminders_enabled=True))
    job = store.add_job(ScheduledJob(
        session_key="web:a", trigger="every", tier="instant", fire_at=first,
        message="活动", interval_seconds=3600,
    ))
    delivery = _Delivery()
    scheduler = SchedulerService(store, delivery, now_fn=lambda: now)

    await scheduler.run_due_once()

    assert delivery.calls[0]["scheduled_at"] == first + timedelta(hours=5)
    assert store.get_job(job.id).fire_at == first + timedelta(hours=6)
    await scheduler.close()
    store.close()
