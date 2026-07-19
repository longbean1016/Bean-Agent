"""PENDING 到 MEMORY/SELF 的两阶段后台整理。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from memory.md_store import MarkdownMemoryStore


logger = logging.getLogger(__name__)


class LLMApi(Protocol):
    async def complete(self, messages: list[dict[str, str]], tools: list[dict[str, Any]] | None = None) -> Any: ...


class MemoryOptimizerBusy(RuntimeError):
    """同一 Workspace 的 Markdown 记忆正在整理。"""


class MemoryOptimizer:
    """串行整理 Markdown 记忆，并以 MEMORY 写入作为 Pending 提交边界。"""

    def __init__(self, store: MarkdownMemoryStore, provider: LLMApi, *, step_delay_seconds: float = 15) -> None:
        self._store = store
        self._provider = provider
        self._step_delay = max(0.0, float(step_delay_seconds))
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    async def optimize(self) -> dict[str, int]:
        if self._lock.locked():
            raise MemoryOptimizerBusy("memory optimizer 正在运行")
        # 锁必须覆盖 snapshot、两次 LLM 调用和文件提交；否则并发任务会把处理中
        # 的临时空 PENDING 误判为没有工作，并破坏快照事务的单写者约束。
        async with self._lock:
            return await self._optimize()

    async def _optimize(self) -> dict[str, int]:
        pending = self._store.snapshot_pending()
        current_memory = self._store.read_long_term()
        if not pending.strip() and not current_memory.strip():
            logger.info("记忆和 Pending 均为空，跳过优化")
            return {
                "pending_chars": 0,
                "memory_chars": 0,
                "self_chars": len(self._store.read_self()),
            }

        try:
            merged = await self._complete(
                "合并长期记忆。只保留会在未来新对话中影响回答方向的稳定事实、偏好、明确要求和助手操作上下文。\n"
                f"当前 MEMORY:\n{current_memory}\n\nPENDING:\n{pending}"
            )
            if self._store.has_long_term_memory():
                self._store.backup_long_term()
            self._store.write_long_term(merged)
            self._store.commit_pending_snapshot()
            logger.info(
                "长期记忆已合并: before_chars=%d after_chars=%d pending_chars=%d",
                len(current_memory),
                len(merged),
                len(pending),
            )
        except Exception:
            # MEMORY 是 Pending 的归档目标；只有该阶段失败才回滚快照，保证候选
            # 事实仍能在后续周期重试。
            self._store.rollback_pending_snapshot()
            logger.exception("长期记忆合并失败，Pending 快照已回滚")
            raise

        if self._step_delay:
            await asyncio.sleep(self._step_delay)

        current_self = self._store.read_self().strip()
        if not current_self:
            logger.info("SELF.md 为空，跳过自我认知更新")
            return {"pending_chars": len(pending), "memory_chars": len(merged), "self_chars": 0}

        try:
            updated_self = await self._complete(
                "更新 SELF，只允许人格与形象、我对当前用户的理解、我们关系的定义三个 section；"
                "不要写入工具规范或用户隐私清单。\n"
                f"当前 SELF:\n{current_self}\n\nPENDING 证据:\n{pending}"
            )
            self._store.write_self(updated_self)
            logger.info("SELF.md 已更新")
            self_chars = len(updated_self)
        except Exception:
            # MEMORY 已经安全提交，SELF 的独立失败不能重新放回 Pending，否则同一批
            # 用户事实会在下一周期重复合并。
            logger.exception("SELF.md 更新失败，保留现有内容")
            self_chars = len(current_self)

        return {
            "pending_chars": len(pending),
            "memory_chars": len(merged),
            "self_chars": self_chars,
        }

    async def _complete(self, prompt: str) -> str:
        response = await self._provider.complete([{"role": "user", "content": prompt}], tools=[])
        content = str(getattr(response, "content", response) or "").strip()
        if not content:
            raise ValueError("Optimizer LLM 返回空内容")
        return content


__all__ = ["MemoryOptimizer", "MemoryOptimizerBusy"]
