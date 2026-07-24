"""soft 定时任务执行时的身份、事实与上下文隔离测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.message_bus import PipelineResult
from proactive.models import ScheduledJob
from proactive.soft_executor import SoftTaskExecutor


class _CapturingPipeline:
    def __init__(self) -> None:
        self.message = None

    async def process(self, message, *, turn_id: str) -> PipelineResult:
        self.message = message
        assert turn_id.startswith("soft-")
        return PipelineResult(content="上海有雨，请带伞。")


@pytest.mark.asyncio
async def test_soft_executor_wraps_task_with_identity_and_evidence_constraints() -> None:
    pipeline = _CapturingPipeline()
    executor = SoftTaskExecutor(pipeline)  # type: ignore[arg-type]
    job = ScheduledJob(
        session_key="web:a",
        trigger="at",
        tier="soft",
        fire_at=datetime(2026, 7, 25, 0, tzinfo=timezone.utc),
        prompt="查询上海实时天气，下雨时提醒用户带伞。",
    )

    result = await executor.execute(job)

    assert result == "上海有雨，请带伞。"
    assert pipeline.message is not None
    content = pipeline.message.content
    assert "你是任务执行者，用户是结果接收者" in content
    assert "只有实际调用工具并获得结果后" in content
    assert "无法执行时必须如实说明，不得编造" in content
    assert "不得把用户需要完成的事情说成你自己要完成" in content
    assert "查询上海实时天气，下雨时提醒用户带伞。" in content
    assert pipeline.message.metadata == {
        "skip_history": True,
        "skip_memory_retrieval": True,
        "skip_post_memory": True,
        "suppress_stream_events": True,
        "allowed_tools": [
            "shell", "web_search", "web_fetch", "read_file", "list_dir", "load_skill",
        ],
    }
