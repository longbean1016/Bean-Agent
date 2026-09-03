"""Checkpoint outbox worker 的顺序、并发和关闭边界测试。"""

from __future__ import annotations

import asyncio

import pytest

from memory.compaction_worker import CompactionOutboxWorker


@pytest.mark.asyncio
async def test_worker_serializes_same_session_but_runs_other_sessions() -> None:
    first_started = asyncio.Event()
    other_started = asyncio.Event()
    release_first = asyncio.Event()
    started: list[str] = []

    async def process(payload: dict[str, object]) -> bool:
        source_ref = str(payload["source_ref"])
        started.append(source_ref)
        if source_ref == "a-1":
            first_started.set()
            await release_first.wait()
        if source_ref == "b-1":
            other_started.set()
        return True

    worker = CompactionOutboxWorker(process, max_concurrency=2)
    try:
        assert await worker.submit({"source_ref": "a-1", "session_key": "a"})
        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        assert not await worker.submit({"source_ref": "a-1", "session_key": "a"})
        assert await worker.submit({"source_ref": "a-2", "session_key": "a"})
        assert await worker.submit({"source_ref": "b-1", "session_key": "b"})

        await asyncio.wait_for(other_started.wait(), timeout=0.5)
        assert "a-2" not in started
        release_first.set()
        await worker.drain()
    finally:
        release_first.set()
        await worker.close()

    assert started.index("a-1") < started.index("a-2")
    assert started.index("b-1") < started.index("a-2")


@pytest.mark.asyncio
async def test_worker_keeps_failed_job_retryable() -> None:
    calls = 0

    async def process(_payload: dict[str, object]) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary failure")
        return True

    worker = CompactionOutboxWorker(process)
    payload = {"source_ref": "retry-me", "session_key": "web:c"}
    try:
        assert await worker.submit(payload)
        await worker.drain()
        # worker 不拥有 durable outbox；失败只结束本次 job，调用方可以从持久化
        # outbox 重放同一 source_ref，而不会被内存状态永久挡住。
        assert await worker.submit(payload)
        await worker.drain()
    finally:
        await worker.close()

    assert calls == 2


@pytest.mark.asyncio
async def test_worker_close_without_drain_cancels_running_jobs() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def process(_payload: dict[str, object]) -> bool:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    worker = CompactionOutboxWorker(process)
    await worker.submit({"source_ref": "close-me", "session_key": "web:c"})
    await asyncio.wait_for(started.wait(), timeout=0.5)
    await worker.close(drain=False)

    assert cancelled.is_set()
    assert not worker._job_tasks
