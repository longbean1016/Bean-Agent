"""跨会话 Turn 的并发准入、FIFO 排队和任务生命周期管理。"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from agent.message_bus import InboundMessage

logger = logging.getLogger(__name__)

StartTurn = Callable[[InboundMessage], Awaitable[None]]
QueuePositionsCallback = Callable[[list["QueuePosition"]], Awaitable[None]]
QueuedCancelledCallback = Callable[[InboundMessage], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SubmitResult:
    status: Literal["started", "queued", "rejected"]
    position: int = 0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CancelResult:
    status: Literal["cancelled", "interrupted", "idle"]


@dataclass(frozen=True, slots=True)
class QueuePosition:
    message: InboundMessage
    position: int


class TurnScheduler:
    """同会话单任务、跨会话限流，并为等待任务维护稳定 FIFO。"""

    def __init__(
        self,
        *,
        max_running: int,
        max_queued: int,
        start_turn: StartTurn,
        on_queue_positions: QueuePositionsCallback | None = None,
        on_queued_cancelled: QueuedCancelledCallback | None = None,
    ) -> None:
        if max_running < 1:
            raise ValueError("max_running 必须大于等于 1")
        if max_queued < 0:
            raise ValueError("max_queued 必须大于等于 0")
        self._max_running = int(max_running)
        self._max_queued = int(max_queued)
        self._start_turn = start_turn
        self._on_queue_positions = on_queue_positions
        self._on_queued_cancelled = on_queued_cancelled
        self._running: dict[str, asyncio.Task[None]] = {}
        self._waiting: deque[InboundMessage] = deque()
        self._queued_by_session: dict[str, InboundMessage] = {}
        self._lock = asyncio.Lock()
        self._accepting = True
        self._closed = False

    async def submit(self, message: InboundMessage) -> SubmitResult:
        positions: list[QueuePosition] | None = None
        async with self._lock:
            if not self._accepting:
                return SubmitResult("rejected", reason="closed")
            if message.session_key in self._running or message.session_key in self._queued_by_session:
                return SubmitResult("rejected", reason="session_busy")
            if len(self._running) < self._max_running:
                self._start_locked(message)
                return SubmitResult("started")
            if len(self._waiting) >= self._max_queued:
                return SubmitResult("rejected", reason="queue_full")
            self._waiting.append(message)
            self._queued_by_session[message.session_key] = message
            positions = self._positions_locked()
            position = len(self._waiting)
        await self._notify_positions(positions)
        return SubmitResult("queued", position=position)

    async def cancel(self, session_key: str) -> CancelResult:
        cancelled_message: InboundMessage | None = None
        positions: list[QueuePosition] | None = None
        async with self._lock:
            task = self._running.get(session_key)
            if task is not None:
                task.cancel()
                return CancelResult("interrupted")
            cancelled_message = self._queued_by_session.pop(session_key, None)
            if cancelled_message is None:
                return CancelResult("idle")
            self._waiting.remove(cancelled_message)
            positions = self._positions_locked()
        await self._notify_queued_cancelled(cancelled_message)
        await self._notify_positions(positions)
        return CancelResult("cancelled")

    def is_busy(self, session_key: str) -> bool:
        """queued 和 running 都属于普通 Turn 占用，主动聊天必须避让。"""

        return session_key in self._running or session_key in self._queued_by_session

    async def close(self) -> None:
        """停止准入并回收全部等待和运行任务；重复关闭保持幂等。"""

        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._accepting = False
            queued = list(self._waiting)
            self._waiting.clear()
            self._queued_by_session.clear()
            tasks = list(self._running.values())
            for task in tasks:
                task.cancel()
        for message in queued:
            await self._notify_queued_cancelled(message)
        await self._notify_positions([])
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _start_locked(self, message: InboundMessage) -> None:
        task = asyncio.create_task(
            self._run_turn(message),
            name=f"scheduled-turn:{message.session_key}",
        )
        self._running[message.session_key] = task

    async def _run_turn(self, message: InboundMessage) -> None:
        try:
            await self._start_turn(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 调度器不能因一个 Turn wrapper 的意外异常停止晋升后续会话。
            logger.exception("调度 Turn 失败: session_key=%s", message.session_key)
        finally:
            await self._finish(message.session_key)

    async def _finish(self, session_key: str) -> None:
        positions: list[QueuePosition]
        current = asyncio.current_task()
        async with self._lock:
            task = self._running.get(session_key)
            if task is current:
                self._running.pop(session_key, None)
            if self._accepting:
                while self._waiting and len(self._running) < self._max_running:
                    message = self._waiting.popleft()
                    self._queued_by_session.pop(message.session_key, None)
                    self._start_locked(message)
            positions = self._positions_locked()
        await self._notify_positions(positions)

    def _positions_locked(self) -> list[QueuePosition]:
        return [
            QueuePosition(message, index)
            for index, message in enumerate(self._waiting, start=1)
        ]

    async def _notify_positions(self, positions: list[QueuePosition] | None) -> None:
        if positions is None or self._on_queue_positions is None:
            return
        try:
            await self._on_queue_positions(positions)
        except Exception:
            logger.exception("广播 Turn 排队位置失败")

    async def _notify_queued_cancelled(self, message: InboundMessage) -> None:
        if self._on_queued_cancelled is None:
            return
        try:
            await self._on_queued_cancelled(message)
        except Exception:
            logger.exception("清理已取消排队 Turn 失败: session_key=%s", message.session_key)


__all__ = [
    "CancelResult",
    "QueuePosition",
    "SubmitResult",
    "TurnScheduler",
]
