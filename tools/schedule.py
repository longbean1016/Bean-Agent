"""由普通对话调用的定时任务创建、查询与取消工具。"""

from __future__ import annotations

from abc import abstractmethod
from datetime import timezone
from typing import Any

from proactive.models import ScheduledJob
from proactive.scheduler import compute_fire_at
from proactive.store import ProactiveStore
from tools.base import Tool


_COMMON_PROPERTIES = {
    "trigger": {"type": "string", "enum": ["at", "after", "every"]},
    "when": {"type": "string", "description": "ISO/HH:MM、30m/2h，或每日 Cron"},
    "timezone": {"type": "string", "description": "IANA 时区，默认 Asia/Shanghai"},
    "name": {"type": "string", "description": "便于查询和取消的名称"},
}


class _ScheduleCreateTool(Tool):
    """复用时间解析和持久化，但不向模型暴露 tier 选择。"""

    @abstractmethod
    def _job_kind(self) -> tuple[str, str]:
        """返回内部固定的 tier 与唯一内容字段。"""

    async def execute(self, **kwargs: Any) -> str:
        session_key = str(kwargs.get("session_key") or "").strip()
        if not session_key:
            return "错误：当前会话身份缺失"
        trigger = str(kwargs.get("trigger") or "")
        timezone_name = str(kwargs.get("timezone") or "Asia/Shanghai")
        tier, content_field = self._job_kind()
        content = str(kwargs.get(content_field) or "").strip()
        if not content:
            return f"错误：{self.name} 必须提供 {content_field}"
        try:
            fire_at, interval_seconds, cron_expr = compute_fire_at(
                trigger,
                str(kwargs.get("when") or ""),
                timezone_name,
                str(kwargs.get("request_time") or ""),
            )
            job = ScheduledJob(
                session_key=session_key,
                trigger=trigger,  # type: ignore[arg-type]
                tier=tier,  # type: ignore[arg-type]
                fire_at=fire_at.astimezone(timezone.utc),
                interval_seconds=interval_seconds,
                cron_expr=cron_expr,
                message=content if tier == "instant" else "",
                prompt=content if tier == "soft" else "",
                name=str(kwargs.get("name") or ""),
                timezone=timezone_name,
            )
            self._store.add_job(job)
        except (TypeError, ValueError) as error:
            return f"错误：{error}"
        label = "固定提醒" if tier == "instant" else "AI 定时任务"
        return f"已创建{label}「{job.name or job.id[:8]}」，首次触发时间：{fire_at.astimezone().isoformat()}"

    def __init__(self, store: ProactiveStore) -> None:
        self._store = store


class ScheduleReminderTool(_ScheduleCreateTool):
    """创建到期后原样投递的固定提醒。"""

    name = "schedule_reminder"
    description = (
        "创建固定提醒。只要到期内容现在就能确定，例如喝水、关电脑、午休、买东西，"
        "必须使用本工具；最终提醒话术写入 message。时间存在歧义时先询问用户。"
    )
    parameters = {
        "type": "object",
        "properties": {
            **_COMMON_PROPERTIES,
            "message": {
                "type": "string",
                "description": "到期后原样发送给用户的最终提醒文本。",
            },
        },
        "required": ["trigger", "when", "message"],
    }

    def _job_kind(self) -> tuple[str, str]:
        return "instant", "message"


class ScheduleTaskTool(_ScheduleCreateTool):
    """创建到期后由隔离 Agent 动态执行的任务。"""

    name = "schedule_task"
    description = (
        "创建到期后才执行的动态任务。仅当到期后需要查询实时信息、读取文件、调用工具、"
        "检查状态或动态判断时使用；普通固定提醒不得使用本工具。创建时只定义未来任务，"
        "不得提前执行，也不要写入预计执行时间；真实时间由到期执行环境提供。"
        "时间或任务目标存在歧义时先询问用户。"
    )
    parameters = {
        "type": "object",
        "properties": {
            **_COMMON_PROPERTIES,
            "prompt": {
                "type": "string",
                "description": (
                    "描述到期后需要执行的独立任务，不是最终提醒话术。"
                    "使用客观指令口吻，写明任务目标、结果接收者、必要背景、需要调用的工具或"
                    "判断条件以及失败处理。不要写入预计日期、时间或‘现在是几点’，需要表达"
                    "时间时使用‘执行时刻’，到期环境会提供真实当前时间。不要在创建任务时提前"
                    "执行查询或检查；只有取得实际工具结果后才能声称已经完成，无法执行时如实"
                    "说明，不得编造。"
                ),
            },
        },
        "required": ["trigger", "when", "prompt"],
    }

    def _job_kind(self) -> tuple[str, str]:
        return "soft", "prompt"


class ListSchedulesTool(Tool):
    name = "list_schedules"
    description = "列出当前会话创建的定时提醒和 AI 定时任务。"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, store: ProactiveStore) -> None:
        self._store = store

    async def execute(self, **kwargs: Any) -> str:
        session_key = str(kwargs.get("session_key") or "").strip()
        if not session_key:
            return "错误：当前会话身份缺失"
        jobs = self._store.list_jobs(session_key)
        if not jobs:
            return "当前会话没有定时任务"
        return "\n".join(
            ["当前会话定时任务："]
            + [f"- {job.id[:8]} | {job.name or '未命名'} | {job.tier}/{job.trigger} | {job.fire_at.isoformat()} | {job.status}" for job in jobs]
        )


class CancelScheduleTool(Tool):
    name = "cancel_schedule"
    description = "按 ID 或名称取消当前会话的定时任务。"
    parameters = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
    }

    def __init__(self, store: ProactiveStore) -> None:
        self._store = store

    async def execute(self, **kwargs: Any) -> str:
        session_key = str(kwargs.get("session_key") or "").strip()
        job_id = str(kwargs.get("id") or "").strip()
        name = str(kwargs.get("name") or "").strip()
        if not session_key or (not job_id and not name):
            return "错误：需要当前会话以及任务 ID 或名称"
        matches = [
            job for job in self._store.list_jobs(session_key)
            if (job_id and (job.id == job_id or job.id.startswith(job_id))) or (name and job.name == name)
        ]
        for job in matches:
            self._store.delete_job(job.id, session_key=session_key)
        return f"已取消 {len(matches)} 个定时任务" if matches else "未找到匹配的定时任务"


__all__ = [
    "CancelScheduleTool",
    "ListSchedulesTool",
    "ScheduleReminderTool",
    "ScheduleTaskTool",
]
