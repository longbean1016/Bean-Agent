"""每轮回复后异步检测并废弃错误的旧记忆规则。"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from memory.events import MemoryWritten, TurnIngested

logger = logging.getLogger(__name__)


class MemorizerApi(Protocol):
    def supersede_batch(self, ids: list[str]) -> object: ...


class RetrieverApi(Protocol):
    async def retrieve(self, query: str, memory_types: list[str] | None = None) -> list[dict[str, object]]: ...


class ProviderApi(Protocol):
    async def complete(self, messages: list[dict[str, str]], tools: list[dict[str, Any]] | None = None) -> Any: ...


class PostResponseMemoryWorker:
    """处理每个成功 Turn 的 invalidation，不在此处隐式新增记忆。"""

    SUPERSEDE_THRESHOLD = 0.82
    SUPERSEDE_CANDIDATE_K = 5
    TOKEN_BUDGET_PER_RUN = 1000
    TOKENS_EXTRACT_INVALIDATION = 96
    TOKENS_CHECK_INVALIDATE = 96

    def __init__(self, memorizer: MemorizerApi, retriever: RetrieverApi, provider: ProviderApi, event_callback: Callable[[MemoryWritten], Awaitable[None]] | None = None) -> None:
        self._memorizer = memorizer
        self._retriever = retriever
        self._provider = provider
        self._event_callback = event_callback

    async def handle(self, event: TurnIngested) -> None:
        await self.run(
            user_msg=event.user_message,
            agent_response=event.assistant_response,
            tool_chain=list(event.tool_chain),
            source_ref=event.source_ref,
            session_key=event.session_key,
            channel=event.channel,
            chat_id=event.chat_id,
        )

    async def run(self, user_msg: str, agent_response: str, tool_chain: list[dict[str, object]], source_ref: str, session_key: str = "", channel: str = "", chat_id: str = "") -> None:
        try:
            # 显式 memorize 属于用户本轮刚确认的新记忆，即使文本与 invalidation 主题相近，
            # 也不能被同一轮后台任务误删，因此先从 tool_chain 收集保护 ID。
            _, protected_ids = self._collect_explicit_memorized(tool_chain)
            await self._handle_invalidations(
                user_msg,
                source_ref,
                protected_ids,
                self.TOKEN_BUDGET_PER_RUN,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
            )
        except Exception as error:
            # Turn 已经持久化且可能已发送给用户，后台记忆失败只能记录，不能向上传播回滚。
            logger.warning("post-response 记忆处理失败: %s", error)

    @staticmethod
    def _consume_budget(remaining: int, cost: int) -> tuple[bool, int]:
        if remaining < cost:
            return False, remaining
        return True, remaining - cost

    @staticmethod
    def _collect_explicit_memorized(tool_chain: list[dict[str, object]]) -> tuple[list[str], set[str]]:
        legacy = re.compile(r"(?:new|reinforced|merged):([A-Za-z0-9:_-]{1,128})")
        explicit = re.compile(r"item_id=([A-Za-z0-9:_-]{1,128})")
        summaries: list[str] = []
        protected: set[str] = set()
        for group in tool_chain:
            calls = group.get("calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict) or call.get("name") != "memorize":
                    continue
                arguments = call.get("arguments")
                if isinstance(arguments, dict):
                    summary = str(arguments.get("summary") or "").strip()
                    if summary:
                        summaries.append(summary)
                result = str(call.get("result") or "")
                match = explicit.search(result) or legacy.search(result)
                if match:
                    protected.add(match.group(1))
        return summaries, protected

    async def _handle_invalidations(self, user_msg: str, source_ref: str, protected_ids: set[str], token_budget: int, *, session_key: str = "", channel: str = "", chat_id: str = "") -> int:
        topics, token_budget = await self._extract_invalidation_topics(user_msg, token_budget)
        for topic in topics:
            candidates = await self._retriever.retrieve(
                topic,
                memory_types=["procedure", "preference"],
            )
            relevant = [
                item for item in candidates
                if float(item.get("score", 0) or 0) >= self.SUPERSEDE_THRESHOLD
                and str(item.get("id") or "") not in protected_ids
            ][: self.SUPERSEDE_CANDIDATE_K]
            if not relevant:
                continue
            superseded, token_budget = await self._check_invalidate(topic, relevant, token_budget)
            if not superseded:
                continue
            self._memorizer.supersede_batch(superseded)
            if self._event_callback is not None and session_key:
                await self._event_callback(MemoryWritten(
                    session_key=session_key,
                    channel=channel,
                    chat_id=chat_id,
                    action="supersede",
                    source_ref=source_ref,
                    superseded_ids=superseded,
                ))
        return token_budget

    async def _extract_invalidation_topics(self, user_msg: str, token_budget: int) -> tuple[list[str], int]:
        allowed, token_budget = self._consume_budget(token_budget, self.TOKENS_EXTRACT_INVALIDATION)
        if not allowed:
            return [], token_budget
        prompt = f"""判断用户消息是否在明确声明 agent 某个现有行为/流程有误，且希望废弃它。

用户消息：{user_msg}

【必须同时满足才触发】
1. 有明确否定、纠错或废弃意图，如“错了/不对/不要再/忘掉/废弃/过时/改掉”。
2. 否定对象是 agent 的操作行为，不是用户自己的事或第三方信息。

【以下情况绝对不触发，返回 []】
- 用户在询问/确认 agent 的流程，如“你的流程是什么”“你怎么做的”。
- 用户在描述/回顾自己的操作。
- 用户提问句、疑问句，即使涉及 agent 行为。
- 含“也许/可能/猜测”等不确定措辞且无明确废弃指令。

若触发，只返回受影响行为主题的 JSON 字符串数组；大多数消息应返回 []。
受影响的行为主题："""
        try:
            return _string_list(await self._complete(prompt)), token_budget
        except Exception as error:
            logger.warning("提取 invalidation 主题失败: %s", error)
            return [], token_budget

    async def _check_invalidate(self, topic: str, candidates: list[dict[str, object]], token_budget: int) -> tuple[list[str], int]:
        allowed, token_budget = self._consume_budget(token_budget, self.TOKENS_CHECK_INVALIDATE)
        if not allowed:
            return [], token_budget
        block = "\n".join(f"- id={item['id']} | {item.get('summary', '')}" for item in candidates)
        prompt = f"""用户明确要求废弃与“{topic}”相关的旧 agent 行为。
从候选规则中选出确实描述该行为的 ID，只返回 JSON 数组：
{block}"""
        try:
            requested = _string_list(await self._complete(prompt))
        except Exception as error:
            logger.warning("确认 invalidation 候选失败: %s", error)
            return [], token_budget
        valid = {str(item["id"]) for item in candidates}
        return [item_id for item_id in requested if item_id in valid], token_budget

    async def _complete(self, prompt: str) -> str:
        response = await self._provider.complete([{"role": "user", "content": prompt}], tools=[])
        return str(getattr(response, "content", response) or "").strip()


def _string_list(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    value = json.loads(text)
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


__all__ = ["PostResponseMemoryWorker"]
