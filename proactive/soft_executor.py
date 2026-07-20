"""Scheduler soft 任务的隔离 Agent 执行器。"""

from __future__ import annotations

from uuid import uuid4

from agent.message_bus import InboundMessage
from agent.pipeline import Pipeline
from proactive.models import ScheduledJob

SOFT_ALLOWED_TOOLS = (
    "shell",
    "web_search",
    "web_fetch",
    "read_file",
    "list_dir",
    "load_skill",
)


class SoftTaskExecutor:
    """用独立 scheduler session 运行 prompt，最终结果仍交给 Scheduler 发送。"""

    def __init__(self, pipeline: Pipeline) -> None:
        self._pipeline = pipeline

    async def execute(self, job: ScheduledJob) -> str:
        """关闭历史、记忆和流事件，只开放用户确认过的工具集合。"""

        message = InboundMessage(
            channel="scheduler",
            sender="scheduler",
            chat_id=job.id,
            content=job.prompt,
            metadata={
                "skip_history": True,
                "skip_memory_retrieval": True,
                "skip_post_memory": True,
                "suppress_stream_events": True,
                "allowed_tools": list(SOFT_ALLOWED_TOOLS),
            },
        )
        result = await self._pipeline.process(message, turn_id=f"soft-{uuid4().hex}")
        return result.content.strip()


__all__ = ["SOFT_ALLOWED_TOOLS", "SoftTaskExecutor"]
