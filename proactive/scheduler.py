"""对齐 akashic 的可恢复 Scheduler，统一执行 instant 与 soft 任务。"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from proactive.models import ScheduledJob
from proactive.store import ProactiveStore

logger = logging.getLogger(__name__)
_DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


class ScheduledDeliveryApi(Protocol):
    async def deliver(
        self,
        *,
        session_key: str,
        content: str,
        source: str,
        delivery_key: str,
        source_id: str,
    ) -> object: ...


SoftExecutor = Callable[[ScheduledJob], Awaitable[str]]


class SchedulerService:
    """轮询持久化任务；同一任务执行期间不会被下一次 tick 重复领取。"""

    def __init__(
        self,
        store: ProactiveStore,
        delivery: ScheduledDeliveryApi,
        *,
        soft_executor: SoftExecutor | None = None,
        tick_seconds: float = 1.0,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._delivery = delivery
        self._soft_executor = soft_executor
        self._tick_seconds = max(.1, float(tick_seconds))
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._in_flight: set[str] = set()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        """幂等启动后台循环；构造阶段不创建任务，便于 Runtime 统一管理。"""

        if self._closed:
            raise RuntimeError("SchedulerService 已关闭")
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="proactive-scheduler")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_due_once()
            except Exception:
                logger.exception("主动 Scheduler tick 失败")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_seconds)
            except TimeoutError:
                pass

    async def run_due_once(self) -> None:
        """执行当前已到期任务，暴露该入口供确定性测试使用。"""

        now = self._now_fn().astimezone(timezone.utc)
        jobs = await asyncio.to_thread(self._store.list_jobs, None, enabled_only=True)
        tasks: list[asyncio.Task[None]] = []
        for job in jobs:
            if job.id in self._in_flight or job.fire_at.astimezone(timezone.utc) > now:
                continue
            self._in_flight.add(job.id)
            tasks.append(asyncio.create_task(self._execute_and_reschedule(job), name=f"scheduled:{job.id}"))
        if tasks:
            await asyncio.gather(*tasks)

    async def _execute_and_reschedule(self, job: ScheduledJob) -> None:
        try:
            settings = await asyncio.to_thread(self._store.get_settings, job.session_key)
            if not settings.reminders_enabled:
                # 关闭提醒时保留任务本身，用户重新开启后仍可管理和恢复。
                return
            quiet_action, resume_at = _reminder_quiet_action(settings, self._now_fn())
            if quiet_action == "delay":
                job.fire_at = resume_at
                job.status = "pending"
                return
            if quiet_action == "skip":
                job.run_count += 1
                job.last_error = ""
                if job.trigger == "every":
                    job.fire_at = next_recurring_fire(job, after=self._now_fn())
                    job.status = "pending"
                else:
                    job.enabled = False
                    job.status = "skipped"
                return
            content = job.message
            source = "scheduled_reminder"
            if job.tier == "soft":
                source = "scheduled_soft"
                if self._soft_executor is None:
                    raise RuntimeError("soft 执行器未配置")
                content = (await self._soft_executor(job)).strip()
            if not content.strip():
                raise RuntimeError("定时任务没有生成可发送内容")
            await self._delivery.deliver(
                session_key=job.session_key,
                content=content,
                source=source,
                delivery_key=f"schedule:{job.id}:{job.fire_at.isoformat()}",
                source_id=job.id,
            )
            job.run_count += 1
            job.last_error = ""
            if job.trigger == "every":
                job.fire_at = next_recurring_fire(job, after=self._now_fn())
                job.status = "pending"
            else:
                job.enabled = False
                job.status = "completed"
        except Exception as error:
            job.last_error = str(error)
            job.status = "failed"
            logger.exception("定时任务执行失败: job_id=%s", job.id)
            if job.trigger != "every":
                job.enabled = False
        finally:
            await asyncio.to_thread(self._store.update_job, job)
            self._in_flight.discard(job.id)

    async def close(self) -> None:
        """停止接收新 tick，并等待当前循环退出；重复关闭无副作用。"""

        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)


def parse_duration(value: str) -> timedelta:
    """解析 `2h30m` 等稳定时长格式。"""

    match = _DURATION_RE.fullmatch(str(value).strip())
    if match is None or not any(match.groups()):
        raise ValueError("无效时间间隔，示例：30m、2h、1d2h")
    days, hours, minutes, seconds = (int(item or 0) for item in match.groups())
    result = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    if result.total_seconds() <= 0:
        raise ValueError("时间间隔必须大于 0")
    return result


def compute_fire_at(trigger: str, when: str, tz_name: str, request_time: str = "") -> tuple[datetime, int | None, str]:
    """把对话工具参数转换为带时区的首次触发时间和周期信息。"""

    tz = ZoneInfo(tz_name)
    now = datetime.fromisoformat(request_time) if request_time else datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    if trigger == "after":
        return now + parse_duration(when), None, ""
    if trigger == "at":
        text = str(when).strip()
        if re.fullmatch(r"\d{1,2}:\d{2}", text):
            hour, minute = (int(item) for item in text.split(":"))
            fire = now.astimezone(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if fire <= now.astimezone(tz):
                fire += timedelta(days=1)
            return fire, None, ""
        fire = datetime.fromisoformat(text)
        return (fire.replace(tzinfo=tz) if fire.tzinfo is None else fire), None, ""
    if trigger == "every":
        try:
            interval = int(parse_duration(when).total_seconds())
            return now + timedelta(seconds=interval), interval, ""
        except ValueError:
            fire = _next_daily_cron(str(when), tz, now)
            return fire, None, str(when).strip()
    raise ValueError("trigger 必须为 at、after 或 every")


def next_recurring_fire(job: ScheduledJob, *, after: datetime) -> datetime:
    """推进周期任务，确保下一次时间严格晚于本次执行结束时间。"""

    if job.interval_seconds:
        next_fire = job.fire_at + timedelta(seconds=job.interval_seconds)
        while next_fire <= after.astimezone(next_fire.tzinfo):
            next_fire += timedelta(seconds=job.interval_seconds)
        return next_fire
    return _next_daily_cron(job.cron_expr, ZoneInfo(job.timezone), after)


def _next_daily_cron(expression: str, tz: ZoneInfo, after: datetime) -> datetime:
    parts = expression.split()
    if len(parts) != 5 or parts[2:] != ["*", "*", "*"]:
        raise ValueError("第一版 every Cron 仅支持每日格式，例如 `0 9 * * *`")
    minute, hour = (int(parts[0]), int(parts[1]))
    if not 0 <= minute <= 59 or not 0 <= hour <= 23:
        raise ValueError("Cron 小时或分钟无效")
    local = after.astimezone(tz)
    result = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return result + timedelta(days=1) if result <= local else result


def _reminder_quiet_action(settings: object, now: datetime) -> tuple[str, datetime]:
    """解释提醒勿扰策略；send 明确表示按原定时间发送。"""

    if not bool(getattr(settings, "quiet_hours_enabled")):
        return "send", now
    tz = ZoneInfo(str(getattr(settings, "timezone")))
    local = now.astimezone(tz)
    start = datetime.strptime(str(getattr(settings, "quiet_start")), "%H:%M").time()
    end = datetime.strptime(str(getattr(settings, "quiet_end")), "%H:%M").time()
    current = local.time().replace(second=0, microsecond=0)
    in_quiet = start == end or (start < end and start <= current < end) or (start > end and (current >= start or current < end))
    if not in_quiet:
        return "send", now
    policy = str(getattr(settings, "reminder_quiet_policy"))
    if policy != "delay":
        return policy, now
    resume = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if resume <= local:
        resume += timedelta(days=1)
    return "delay", resume.astimezone(timezone.utc)


__all__ = ["SchedulerService", "compute_fire_at", "next_recurring_fire", "parse_duration"]
