"""Checkpoint outbox 的后台消费器。

worker 只负责调度和生命周期；具体的 source_ref 处理由 MemoryEngine 提供，
这样 outbox 的持久化、提取和幂等写入仍由同一个 owner 管理。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

Payload = dict[str, object]
ProcessPayload = Callable[[Payload], Awaitable[bool]]


class CompactionOutboxWorker:
    """并发消费 checkpoint outbox，并保持同一 session 的 generation 顺序。"""

    def __init__(
        self,
        process_payload: ProcessPayload,
        *,
        max_concurrency: int = 2,
    ) -> None:
        self._process_payload = process_payload
        self._max_concurrency = max(1, int(max_concurrency))
        self._queue: asyncio.Queue[Payload] = asyncio.Queue()
        self._start_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._loop_tasks: set[asyncio.Task[None]] = set()
        self._job_tasks: set[asyncio.Task[None]] = set()
        self._session_tails: dict[str, asyncio.Task[None]] = {}
        self._queued_refs: set[str] = set()
        self._active_refs: set[str] = set()
        self._accepting = True
        self._closed = False

    async def start(self) -> None:
        """惰性启动固定数量的消费者，避免在事件循环外创建常驻任务。"""

        async with self._start_lock:
            if self._closed:
                raise RuntimeError("CompactionOutboxWorker 已关闭")
            if self._loop_tasks:
                return
            self._loop_tasks = {
                asyncio.create_task(
                    self._consume_loop(index),
                    name=f"memory-compaction-worker-{index}",
                )
                for index in range(self._max_concurrency)
            }

    async def submit(self, payload: Payload) -> bool:
        """提交一份 durable payload；同一 source_ref 在内存中只排队一次。"""

        if self._closed or not self._accepting:
            return False
        source_ref = str(payload.get("source_ref") or "").strip()
        if not source_ref:
            raise ValueError("compaction outbox source_ref 不能为空")
        await self.start()
        if source_ref in self._queued_refs or source_ref in self._active_refs:
            return False
        self._queued_refs.add(source_ref)
        self._queue.put_nowait(dict(payload))
        return True

    async def submit_many(self, payloads: list[Payload]) -> int:
        submitted = 0
        for payload in payloads:
            if await self.submit(payload):
                submitted += 1
        return submitted

    async def drain(self) -> None:
        """等待已接收的 outbox job 完成；失败 job 会继续留在 durable outbox。"""

        while True:
            await self._queue.join()
            job_tasks = tuple(self._job_tasks)
            if not job_tasks:
                return
            await asyncio.gather(*job_tasks, return_exceptions=True)
            # gather 可能观察到 job 已完成，但 done callback 尚未从集合移除它；
            # 在重新检查集合前让出事件循环，避免已完成任务造成忙等死循环。
            await asyncio.sleep(0)

    async def close(self, *, drain: bool = True) -> None:
        if self._closed:
            return
        self._accepting = False
        if drain:
            await self.drain()
        self._closed = True
        loop_tasks = tuple(self._loop_tasks)
        for task in loop_tasks:
            task.cancel()
        if loop_tasks:
            await asyncio.gather(*loop_tasks, return_exceptions=True)
        self._loop_tasks.clear()
        # drain=False 只用于异常关闭；必须连同已出队 job 一起取消，避免
        # MemoryEngine 随后关闭 SQLite/Markdown 后，后台任务仍继续访问已关闭资源。
        job_tasks = tuple(self._job_tasks)
        if not drain:
            for task in job_tasks:
                task.cancel()
        if job_tasks:
            await asyncio.gather(*job_tasks, return_exceptions=True)
        self._job_tasks.clear()
        self._session_tails.clear()
        self._queued_refs.clear()
        self._active_refs.clear()

    async def _consume_loop(self, index: int) -> None:
        while True:
            payload = await self._queue.get()
            source_ref = str(payload.get("source_ref") or "")
            self._queued_refs.discard(source_ref)
            self._active_refs.add(source_ref)
            previous = self._session_tails.get(str(payload.get("session_key") or ""))
            task = asyncio.create_task(
                self._run_job(payload, previous),
                name=f"memory-compaction-job:{source_ref}",
            )
            self._job_tasks.add(task)
            session_key = str(payload.get("session_key") or "")
            if session_key:
                self._session_tails[session_key] = task
            task.add_done_callback(
                lambda completed, ref=source_ref, key=session_key: self._finish_job(
                    completed,
                    source_ref=ref,
                    session_key=key,
                )
            )
            self._queue.task_done()

    async def _run_job(
        self,
        payload: Payload,
        previous: asyncio.Task[None] | None,
    ) -> None:
        if previous is not None:
            try:
                await previous
            except asyncio.CancelledError:
                raise
            except Exception:
                # 前一代失败会保留自己的 outbox；后续 generation 仍可独立重试。
                pass
        try:
            async with self._semaphore:
                await self._process_payload(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 不删除 durable outbox，下一次启动或显式 replay 可以安全重放。
            logger.exception(
                "checkpoint outbox 后台处理失败: source_ref=%s",
                payload.get("source_ref"),
            )

    def _finish_job(
        self,
        task: asyncio.Task[None],
        *,
        source_ref: str,
        session_key: str,
    ) -> None:
        self._job_tasks.discard(task)
        self._active_refs.discard(source_ref)
        if session_key and self._session_tails.get(session_key) is task:
            self._session_tails.pop(session_key, None)


__all__ = ["CompactionOutboxWorker"]
