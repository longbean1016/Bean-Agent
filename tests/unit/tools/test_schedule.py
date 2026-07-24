"""固定提醒与动态定时任务的独立工具契约测试。"""

from __future__ import annotations

import pytest

from proactive.store import ProactiveStore
from tools.schedule import ScheduleReminderTool, ScheduleTaskTool


def test_schedule_tools_expose_disjoint_content_schemas(tmp_path) -> None:
    store = ProactiveStore(tmp_path / "proactive.db")
    reminder = ScheduleReminderTool(store)
    task = ScheduleTaskTool(store)

    reminder_properties = reminder.parameters["properties"]
    task_properties = task.parameters["properties"]
    assert reminder.name == "schedule_reminder"
    assert set(reminder_properties) == {"trigger", "when", "message", "timezone", "name"}
    assert reminder.parameters["required"] == ["trigger", "when", "message"]
    assert "固定提醒" in reminder.description
    assert task.name == "schedule_task"
    assert set(task_properties) == {"trigger", "when", "prompt", "timezone", "name"}
    assert task.parameters["required"] == ["trigger", "when", "prompt"]
    assert "到期后" in task.description
    assert "不要写入预计执行时间" in task.description
    assert "不得编造" in task_properties["prompt"]["description"]
    assert "真实当前时间" in task_properties["prompt"]["description"]
    store.close()


@pytest.mark.asyncio
async def test_schedule_reminder_persists_instant_message(tmp_path) -> None:
    store = ProactiveStore(tmp_path / "proactive.db")
    tool = ScheduleReminderTool(store)

    result = await tool.execute(
        session_key="web:a",
        trigger="after",
        when="30m",
        message="30分钟到了，请关电脑。",
        name="关电脑提醒",
        request_time="2026-07-24T12:00:00+08:00",
    )

    jobs = store.list_jobs("web:a")
    assert "已创建固定提醒" in result
    assert len(jobs) == 1
    assert jobs[0].tier == "instant"
    assert jobs[0].message == "30分钟到了，请关电脑。"
    assert jobs[0].prompt == ""
    store.close()


@pytest.mark.asyncio
async def test_schedule_task_persists_soft_prompt(tmp_path) -> None:
    store = ProactiveStore(tmp_path / "proactive.db")
    tool = ScheduleTaskTool(store)

    result = await tool.execute(
        session_key="web:a",
        trigger="at",
        when="09:05",
        prompt="到期后查询用户的 GitHub 仓库实时状态并生成报告。",
        name="查看GitHub仓库",
        request_time="2026-07-24T12:00:00+08:00",
    )

    jobs = store.list_jobs("web:a")
    assert "已创建AI 定时任务" in result
    assert len(jobs) == 1
    assert jobs[0].tier == "soft"
    assert jobs[0].message == ""
    assert jobs[0].prompt == "到期后查询用户的 GitHub 仓库实时状态并生成报告。"
    store.close()
