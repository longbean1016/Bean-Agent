"""PENDING 到 MEMORY/SELF 的两阶段后台整理。"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from memory.md_store import DEFAULT_SELF_MD, MarkdownMemoryStore


class LLMApi(Protocol):
    async def complete(self, messages: list[dict[str, str]], tools: list[dict[str, Any]] | None = None) -> Any: ...


class MemoryOptimizerBusy(RuntimeError):
    """同一 Workspace 的 Markdown 记忆正在整理。"""


class MemoryOptimizer:
    def __init__(self, store: MarkdownMemoryStore, provider: LLMApi) -> None:
        self._store = store
        self._provider = provider
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
        if not pending.strip():
            return {"pending_chars": 0, "memory_chars": len(self._store.read_long_term()), "self_chars": len(self._store.read_self())}
        try:
            current_memory = self._store.read_long_term()
            merged = await self._complete(
                "合并长期记忆。只保留会在未来新对话中影响回答方向的稳定事实、偏好、明确要求和助手操作上下文。\n"
                f"当前 MEMORY:\n{current_memory}\n\nPENDING:\n{pending}"
            )
            current_self = self._store.read_self().strip() or DEFAULT_SELF_MD
            updated_self = await self._complete(
                "更新 SELF，只允许人格与形象、我对当前用户的理解、我们关系的定义三个 section；不要写入工具规范或用户隐私清单。\n"
                f"当前 SELF:\n{current_self}\n\nPENDING 证据:\n{pending}"
            )
            if self._store.has_long_term_memory():
                self._store.backup_long_term()
            self._store.write_long_term(merged)
            self._store.write_self(updated_self)
            self._store.commit_pending_snapshot()
            return {"pending_chars": len(pending), "memory_chars": len(merged), "self_chars": len(updated_self)}
        except Exception:
            # 任一 LLM 或文件步骤失败都把 snapshot 合回新 PENDING，保证事实不丢失。
            self._store.rollback_pending_snapshot()
            raise

    async def _complete(self, prompt: str) -> str:
        response = await self._provider.complete([{"role": "user", "content": prompt}], tools=[])
        content = str(getattr(response, "content", response) or "").strip()
        if not content:
            raise ValueError("Optimizer LLM 返回空内容")
        return content


__all__ = ["MemoryOptimizer", "MemoryOptimizerBusy"]
