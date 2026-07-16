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
        return f"""你是记忆检索决策器。根据近期对话和当前用户消息，判断是否需要检索用户个人事实或历史事件，并输出查询。

近期对话：
{recent_history.strip() or '（无）'}

当前用户消息：
{user_msg}

规则：
- NO_RETRIEVE：打招呼、闲聊、确认当前轮内容、通用知识问答、简单回应“好/嗯/继续”、用户提出新的服务偏好或执行规则。
- RETRIEVE：询问过去发生的事、用户个人信息、用户是否告诉过某事。
- 新偏好或规则仍是 NO_RETRIEVE；只交给独立 procedure/preference query 处理。
- “都有哪些/列举/所有/一共/总共/历史上”等聚合问题必须 RETRIEVE，并改写为覆盖主题的宽泛 query。
- “他/她/它/这个/那个/这东西/这玩意”等指示词优先结合近期对话消解实际实体。
- “你还记得吗/你知道我的/我跟你说过”等元问题是在查事实，history_query 要贴近记忆 summary。
- 快递、物流、包裹、到货若指向用户购买行为，应查购买历史；纯工具查询可以不查。
- 身体症状、药、复查若指向用户健康状态，应查健康档案或历史记录。

<example id="new_service_rule_not_history">
用户消息：以后讲复杂问题先给我一个贯穿始终的例子
输出：<decision>NO_RETRIEVE</decision><history_query></history_query>
</example>
<example id="external_resource_not_history">
用户消息：【视频标题-示例站点】 https://short.example/item
输出：<decision>NO_RETRIEVE</decision><history_query></history_query>
</example>
<example id="memory_question_history">
用户消息：你还记得我用的是哪个 Fitbit 吗
输出：<decision>RETRIEVE</decision><history_query>用户使用的 Fitbit 设备型号</history_query>
</example>

只输出 XML，不要解释：
<decision>RETRIEVE|NO_RETRIEVE</decision>
<history_query>...</history_query>"""

    @staticmethod
    def _procedure_prompt(user_msg: str) -> str:
        return f"""只输出一行检索 query，不要解释。

把用户消息改写成 preference/procedure 库能命中的 summary 风格查询：
- 用户希望 agent 怎样服务、讲解、推荐。
- agent 在某类请求下必须怎么做、用什么工具。
- 用户发来外部资源、文件、图片、链接时 agent 应如何处理。
不要抽一次性标题词，要写可复用场景。丢弃一次性标题、情绪词、短链路径；保留平台、资源类型、输入形态、用户动作和 agent 执行对象。不得用“某平台”“某资源”占位。

<example id="procedure_explicit_command">把这个资源下载下来 → 用户要求 agent 下载外部资源</example>
<example id="procedure_direct_action">帮我把这个内容整理成表格 → 用户要求 agent 整理内容</example>
<example id="procedure_resource_share">【视频标题-哔哩哔哩】短链 → 用户发送哔哩哔哩视频链接时 agent 应如何处理</example>
<example id="procedure_document_link">这个文档链接你看一下 → 用户发送文档链接时 agent 应如何处理</example>
<example id="procedure_attachment">这是文件，帮我处理一下 → 用户发送文件并要求 agent 处理</example>
<example id="procedure_media">帮我看看这张图 → 用户发送图片并要求 agent 分析</example>
<example id="preference_service_style">以后讲复杂问题先给贯穿始终的例子 → 用户希望 agent 讲解复杂问题时提供贯穿示例</example>
<example id="preference_future_rule">以后先给结论再解释 → 用户希望 agent 回答时先给结论再解释</example>
<example id="memory_answer_rule">你还记得我之前告诉你的设备型号吗 → 用户询问记忆内容时 agent 应如何查找依据</example>
<example id="answer_style_rule">解释两个协议区别 → 用户询问知识问题时 agent 应如何组织回答</example>

用户消息：{user_msg}
输出："""


__all__ = ["GateDecision", "QueryRewriter"]
