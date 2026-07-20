"""由普通对话调用的定时任务创建、查询与取消工具。"""

from __future__ import annotations

from datetime import timezone
from typing import Any

from proactive.models import ScheduledJob
from proactive.scheduler import compute_fire_at
from proactive.store import ProactiveStore
from tools.base import Tool


class ScheduleTool(Tool):
    name = "schedule"
    description = (
        "为当前会话创建定时任务。固定文本使用 instant；需要到期后由 AI 执行 prompt 使用 soft。"
        "时间或执行方式存在歧义时必须先询问用户，不得猜测。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "tier": {"type": "string", "enum": ["instant", "soft"]},
            "trigger": {"type": "string", "enum": ["at", "after", "every"]},
            "when": {"type": "string", "description": "ISO/HH:MM、30m/2h，或每日 Cron"},
            "message": {"type": "string", "description": "instant 固定提醒文本"},
            "prompt": {"type": "string", "description": "soft 到期执行的独立提示词"},
            "timezone": {"type": "string", "description": "IANA 时区，默认 Asia/Shanghai"},
            "name": {"type": "string", "description": "便于查询和取消的名称"},
        },
        "required": ["tier", "trigger", "when"],
    }

    def __init__(self, store: ProactiveStore) -> None:
        self._store = store

    async def execute(self, **kwargs: Any) -> str:
        session_key = str(kwargs.get("session_key") or "").strip()
        if not session_key:
            return "错误：当前会话身份缺失"
        tier = str(kwargs.get("tier") or "")
        trigger = str(kwargs.get("trigger") or "")
        timezone_name = str(kwargs.get("timezone") or "Asia/Shanghai")
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
                message=str(kwargs.get("message") or ""),
                prompt=str(kwargs.get("prompt") or ""),
                name=str(kwargs.get("name") or ""),
                timezone=timezone_name,
            )
            self._store.add_job(job)
        except (TypeError, ValueError) as error:
            return f"错误：{error}"
        return f"已创建{('固定提醒' if tier == 'instant' else 'AI 定时任务')}「{job.name or job.id[:8]}」，首次触发时间：{fire_at.astimezone().isoformat()}"


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


__all__ = ["CancelScheduleTool", "ListSchedulesTool", "ScheduleTool"]
