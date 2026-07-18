"""通过假设性记忆摘要扩充空召回结果，并为所有失败提供原样降级。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)


class ProviderApi(Protocol):
    async def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, object]] | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class HyDEAugmentResult:
    """保留增强过程信息，供 MemoryEngine 写入检索 trace。"""

    items: list[dict[str, object]]
    used_hyde: bool
    hypothesis: str | None


class HyDEEnhancer:
    """生成第三人称假想条目，并只追加原召回中不存在的记忆。"""

    def __init__(self, provider: ProviderApi, *, timeout_s: float = 2.0) -> None:
        self._provider = provider
        self._timeout_s = max(0.01, float(timeout_s))

    async def augment(
        self,
        *,
        query: str,
        context: str,
        raw_items: list[dict[str, object]],
        retrieve_fn: Callable[[str], Awaitable[list[dict[str, object]]]],
    ) -> HyDEAugmentResult:
        """使用 HyDE 追加候选；任一步失败都返回未经修改的原始列表。"""

        hypothesis = await self.generate_hypothesis(query, context)
        if not hypothesis:
            return HyDEAugmentResult(raw_items, False, None)
        try:
            hyde_items = await retrieve_fn(hypothesis)
        except Exception as error:
            logger.warning("HyDE 二次记忆检索失败，保留原始结果: %s", error)
            return HyDEAugmentResult(raw_items, False, hypothesis)

        merged = _union_dedup(raw_items, hyde_items)
        return HyDEAugmentResult(merged, len(merged) > len(raw_items), hypothesis)

    async def generate_hypothesis(self, query: str, context: str) -> str | None:
        """生成数据库中可能存在的记忆摘要，失败或超时返回空值。"""

        prompt = _build_prompt(query, context)
        try:
            response = await asyncio.wait_for(
                self._provider.complete([{"role": "user", "content": prompt}], tools=[]),
                timeout=self._timeout_s,
            )
        except Exception as error:
            logger.warning("HyDE 假想记忆生成失败，跳过增强: %s", error)
            return None
        value = str(getattr(response, "content", response) or "").strip()
        return value or None


def _union_dedup(
    raw_items: list[dict[str, object]],
    hyde_items: list[dict[str, object]],
) -> list[dict[str, object]]:
    """保持原结果对象和顺序，只按 ID 追加 HyDE 独有条目。"""

    seen = {str(item.get("id") or "") for item in raw_items if item.get("id")}
    merged = list(raw_items)
    for item in hyde_items:
        item_id = str(item.get("id") or "")
        if item_id and item_id in seen:
            continue
        merged.append(item)
        if item_id:
            seen.add(item_id)
    return merged


def _build_prompt(query: str, context: str) -> str:
    context_block = f"\n近期对话背景：\n{context.strip()}\n" if context.strip() else ""
    return f"""你是个人助手的记忆系统。根据用户提问，生成一条如果信息存在于记忆库中会呈现的假想条目。
{context_block}
规则：
- 使用第三人称“用户”开头的简洁事实陈述。
- 不回答问题，不添加问题中没有暗示的新信息。
- 只输出一条文本，不要解释。

用户提问：{query}
假想记忆条目："""


__all__ = ["HyDEAugmentResult", "HyDEEnhancer"]
