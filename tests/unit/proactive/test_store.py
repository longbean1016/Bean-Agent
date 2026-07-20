"""主动设置与任务存储的离线单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from proactive.models import ProactiveNotification, ScheduledJob, SessionProactiveSettings
from proactive.store import ProactiveStore


def test_settings_round_trip_and_isolation(tmp_path) -> None:
    store = ProactiveStore(tmp_path / "proactive.db")
    saved = store.upsert_settings(
        SessionProactiveSettings(
            session_key="web:a",
            reminders_enabled=True,
            conversation_enabled=True,
        )
    )
    assert saved.session_key == "web:a"
    assert store.get_settings("web:a").conversation_enabled is True
    assert store.get_settings("web:b").conversation_enabled is False
    store.close()
    store.close()


def test_settings_reject_invalid_ranges(tmp_path) -> None:
    store = ProactiveStore(tmp_path / "proactive.db")
    with pytest.raises(ValueError, match="1-168"):
        store.upsert_settings(SessionProactiveSettings(session_key="web:a", min_conversation_interval_hours=0))
    with pytest.raises(ValueError, match="1-20"):
        store.upsert_settings(SessionProactiveSettings(session_key="web:a", daily_conversation_limit=21))


def test_job_round_trip_and_delivery_reservation(tmp_path) -> None:
    store = ProactiveStore(tmp_path / "proactive.db")
    job = ScheduledJob(
        session_key="web:a",
        trigger="at",
        tier="instant",
        fire_at=datetime.now(timezone.utc),
        message="提醒",
    )
    store.add_job(job)
    assert store.get_job(job.id).message == "提醒"
    assert store.reserve_delivery("job:" + job.id, "web:a", "m1") is True
    assert store.reserve_delivery("job:" + job.id, "web:a", "m2") is False


def test_recurring_notification_only_replaces_pending_occurrence(tmp_path) -> None:
    store = ProactiveStore(tmp_path / "proactive.db")
    first = store.enqueue_notification(ProactiveNotification(
        session_key="web:a", source="scheduled_reminder", source_id="daily-job",
        content="第一次", scheduled_at=datetime(2026, 7, 20, 1, tzinfo=timezone.utc), recurring=True,
    ))
    latest = store.enqueue_notification(ProactiveNotification(
        session_key="web:a", source="scheduled_reminder", source_id="daily-job",
        content="第二次", scheduled_at=datetime(2026, 7, 21, 1, tzinfo=timezone.utc), recurring=True,
    ))
    assert latest.id == first.id
    assert [item.content for item in store.list_notifications("web:a")] == ["第二次"]

    assert store.mark_notification_delivered(latest.id)
    third = store.enqueue_notification(ProactiveNotification(
        session_key="web:a", source="scheduled_reminder", source_id="daily-job",
        content="第三次", scheduled_at=datetime(2026, 7, 22, 1, tzinfo=timezone.utc), recurring=True,
    ))
    assert third.id != latest.id
    assert [item.content for item in store.list_notifications("web:a")] == ["第二次", "第三次"]
    store.close()
