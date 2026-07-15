"""记忆检索 Gate 与 history/procedure 双查询改写。"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol


class LLMApi(Protocol):
    async def complete(self, messages: list[dict[str, str]], tools: list[dict[str, Any]] | None = None) -> Any: ...


@dataclass(frozen=True, slots=True)
class GateDecision:
    needs_episodic: bool
    episodic_query: str
    latency_ms: int
    procedure_query: str = ""


class QueryRewriter:
    def __init__(self, llm_client: LLMApi, *, model: str = "", max_tokens: int = 220, timeout_ms: int = 800) -> None:
        self._llm = llm_client
        self._model = model
        self._max_tokens = max(64, int(max_tokens))
        self._timeout = max(0.01, float(timeout_ms) / 1000)

    async def decide(self, user_msg: str, recent_history: str = "") -> GateDecision:
        started = time.perf_counter()
        fallback = self._decision(started, user_msg, True, user_msg, "")
        main = asyncio.create_task(self._call(self._history_prompt(user_msg, recent_history)))
        procedure = asyncio.create_task(self._call(self._procedure_prompt(user_msg)))
        done, pending = await asyncio.wait({main, procedure}, timeout=self._timeout)
        for task in pending:
            task.cancel()
        if not done:
            return fallback

        raw_history = ""
        raw_procedure = ""
        if main in done:
            try:
                raw_history = main.result()
            except Exception:
                pass
        if procedure in done:
            try:
                raw_procedure = self._clean_procedure(procedure.result())
            except Exception:
                pass
        parsed = self._parse(raw_history)
        if parsed is None:
            # fail-open：宁可多做一次本地检索，也不能因 Gate 服务失败漏掉用户事实。
            return self._decision(started, user_msg, True, user_msg, raw_procedure)
        return self._decision(started, user_msg, parsed[0], parsed[1], raw_procedure)

    async def _call(self, prompt: str) -> str:
        response = await self._llm.complete([{"role": "user", "content": prompt}], tools=[])
        return str(getattr(response, "content", response) or "")

    @staticmethod
    def _parse(raw: str) -> tuple[bool, str] | None:
        decision = QueryRewriter._tag(raw, "decision").upper()
        if decision not in {"RETRIEVE", "NO_RETRIEVE"}:
            return None
        return decision == "RETRIEVE", QueryRewriter._tag(raw, "history_query")

    @staticmethod
    def _tag(raw: str, name: str) -> str:
        match = re.search(rf"<{name}>\s*(.*?)\s*</{name}>", raw, re.I | re.S)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _clean_procedure(raw: str) -> str:
        value = re.sub(r"\s+", " ", str(raw or "")).strip("。 .")
        return "" if value.lower() in {"", "空", "无", "none", "null", "n/a", "(empty)"} else value

    @staticmethod
    def _decision(started: float, user_msg: str, needs: bool, episodic: str, procedure: str) -> GateDecision:
        return GateDecision(
            needs_episodic=needs,
            episodic_query=episodic.strip() or user_msg.strip(),
            procedure_query=procedure.strip(),
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
        )

    @staticmethod
    def _history_prompt(user_msg: str, recent_history: str) -> str:
        return f"""你是记忆检索决策器。判断当前消息是否需要检索用户个人事实或历史事件。
近期对话：{recent_history.strip() or '（无）'}
当前用户消息：{user_msg}
RETRIEVE：过去事件、个人信息、是否说过某事、聚合历史问题。
NO_RETRIEVE：闲聊、通用知识、当前轮确认、新服务偏好或执行规则。
只输出：<decision>RETRIEVE|NO_RETRIEVE</decision><history_query>改写查询</history_query>"""

    @staticmethod
    def _procedure_prompt(user_msg: str) -> str:
        return f"""只输出一行 preference/procedure 检索 query，不要解释。
把消息改写为可复用的服务偏好、工具要求或处理场景；保留平台和资源类型，丢弃一次性标题与短链。
用户消息：{user_msg}
输出："""


__all__ = ["GateDecision", "QueryRewriter"]
