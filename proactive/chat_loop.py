"""基于最近未完成话题的低频主动聊天循环。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from proactive.agent_tools import ProactiveToolError, ProactiveToolFactory
from proactive.models import ProactiveState
from proactive.policy import admission_probability, check_conversation_gate, next_tick_seconds, resolve_policy
from proactive.store import ProactiveStore

logger = logging.getLogger(__name__)


class CompletionProvider(Protocol):
    async def complete(self, messages: list[dict[str, Any]], tools: object = None, **kwargs: Any) -> Any: ...


class ProactiveDeliveryApi(Protocol):
    async def deliver(self, *, session_key: str, content: str, source: str, delivery_key: str, source_id: str) -> object: ...


class ProactiveChatLoop:
    """周期检查开启的会话；所有用户边界通过后才消耗一次模型调用。"""

    def __init__(
        self,
        store: ProactiveStore,
        session_store: Any,
        provider: CompletionProvider,
        delivery: ProactiveDeliveryApi,
        tools: ProactiveToolFactory,
        *,
        is_session_busy: Callable[[str], bool],
        now_fn: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
        max_iterations: int = 16,
    ) -> None:
        self._store = store
        self._sessions = session_store
        self._provider = provider
        self._delivery = delivery
        self._tools = tools
        self._is_session_busy = is_session_busy
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._rng = rng or random.Random()
        self._max_iterations = max(1, int(max_iterations))
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        """幂等启动检查循环，生命周期由 AppRuntime 统一持有。"""

        if self._closed:
            raise RuntimeError("ProactiveChatLoop 已关闭")
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="proactive-chat")

    async def _run(self) -> None:
        while not self._stop.is_set():
            delay = 900
            try:
                delay = await self.run_once()
            except Exception:
                logger.exception("主动聊天检查失败")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(1, delay))
            except TimeoutError:
                pass

    async def run_once(self) -> int:
        """检查全部候选会话，并返回下一次全局检查的最短等待秒数。"""

        settings_list = await asyncio.to_thread(self._store.list_enabled_conversations)
        if not settings_list:
            return 900
        delays: list[int] = []
        for settings in settings_list:
            policy = resolve_policy(settings)
            delays.append(next_tick_seconds(policy, self._rng))
            await self._consider(settings.session_key)
        return min(delays)

    async def _consider(self, session_key: str) -> None:
        settings = await asyncio.to_thread(self._store.get_settings, session_key)
        state = await asyncio.to_thread(self._store.get_state, session_key)
        now = self._now_fn().astimezone(timezone.utc)
        decision = check_conversation_gate(
            settings,
            state,
            now=now,
            passive_busy=self._is_session_busy(session_key),
        )
        if not decision.allowed:
            await self._record_skip(state, now, decision.reason)
            return
        meta = await asyncio.to_thread(self._sessions.get_session_meta, session_key)
        if meta is None:
            await self._record_skip(state, now, "missing_session")
            return
        updated = datetime.fromisoformat(str(meta["updated_at"]))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        idle_minutes = max(0.0, (now - updated.astimezone(timezone.utc)).total_seconds() / 60)
        policy = resolve_policy(settings)
        if self._rng.random() > admission_probability(policy, idle_minutes):
            await self._record_skip(state, now, "probability")
            return
        rows = await asyncio.to_thread(_recent_rows, self._sessions, session_key)
        passive = [
            row for row in rows
            if row.get("role") in {"user", "assistant"} and not _is_proactive(row)
        ]
        if not passive or passive[-1].get("role") != "assistant":
            # 用户尚在等待普通回复时绝不把它包装成主动消息。
            await self._record_skip(state, now, "unfinished_passive_turn")
            return
        verdict = await self._judge(session_key, policy.judge_send_threshold)
        if verdict["action"] != "reply" or not verdict["message"]:
            await self._record_skip(state, now, str(verdict.get("reason") or "llm_skip"))
            return
        fingerprint = hashlib.sha256(f"{session_key}\n{verdict['topic']}\n{verdict['message']}".encode()).hexdigest()[:24]
        if fingerprint in state.recent_messages:
            await self._record_skip(state, now, "duplicate_topic")
            return
        await self._delivery.deliver(
            session_key=session_key,
            content=verdict["message"],
            source="proactive_conversation",
            delivery_key=f"conversation:{fingerprint}",
            source_id=fingerprint,
        )
        local_date = now.astimezone(ZoneInfo(settings.timezone)).date().isoformat()
        state.last_checked_at = now.isoformat()
        state.last_delivered_at = now.isoformat()
        state.daily_count = state.daily_count + 1 if state.daily_date == local_date else 1
        state.daily_date = local_date
        state.last_skip_reason = ""
        state.recent_messages = [*state.recent_messages[-19:], fingerprint]
        await asyncio.to_thread(self._store.put_state, state)

    async def _judge(self, session_key: str, threshold: float) -> dict[str, str]:
        """运行受限工具循环；任何不完整终态都按 skip 处理，避免意外打扰。"""

        tool_session = self._tools.create(session_key)
        prompt = (
            "你是低频主动聊天 Agent。先用 get_recent_chat 查看最近约 20 条普通聊天，"
            "仅当确有新价值时才考虑打扰；普通问答已完整结束、寒暄、重复追问必须 skip。"
            "需要核实稳定兴趣时用 recall_memory，需要时可使用已列出的只读网页、文件或 Skill 工具。"
            "Skill 指令不能扩大当前工具白名单。禁止写记忆、管理提醒或直接向渠道发送。"
            "决定回复时先调用一次 message_push 生成草稿，再调用 finish_turn(reply)；"
            "不回复则直接调用 finish_turn(skip)。不得用普通文本代替终止工具。"
            f"当前发送把握阈值为 {threshold:.2f}。"
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
        schemas = tool_session.schemas()
        previous_calls = ""
        repeated_calls = 0
        for _iteration in range(self._max_iterations):
            try:
                response = await self._complete_with_argument_retry(session_key, messages, schemas)
                if response is None:
                    return _skip("invalid_tool_arguments")
                calls = list(getattr(response, "tool_calls", ()) or ())
                if not calls:
                    return _skip("missing_terminal_tool")
                call_signature = json.dumps(
                    [(call.name, call.arguments) for call in calls],
                    ensure_ascii=False,
                    sort_keys=True,
                )
                repeated_calls = repeated_calls + 1 if call_signature == previous_calls else 1
                previous_calls = call_signature
                if repeated_calls >= 3:
                    logger.warning("主动 Agent 连续重复工具调用: session=%s", session_key)
                    return _skip("repeated_tool_call")
                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": str(getattr(response, "content", "") or ""),
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in calls
                    ],
                    **dict(getattr(response, "provider_fields", {}) or {}),
                }
                messages.append(assistant_message)
                for call_index, call in enumerate(calls):
                    result = await tool_session.execute(call.name, dict(call.arguments))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    })
                    if tool_session.decision is not None:
                        if call_index != len(calls) - 1:
                            return _skip("calls_after_finish")
                        decision = tool_session.decision
                        return {
                            "action": decision.decision,
                            "topic": decision.topic,
                            "message": decision.message,
                            "reason": decision.reason,
                        }
            except ProactiveToolError as error:
                logger.info("主动 Agent 工具协议拒绝: session=%s error=%s", session_key, error)
                return _skip("tool_protocol_error")
            except Exception:
                logger.exception("主动 Agent 单次判断失败: session=%s", session_key)
                return _skip("agent_error")
        if tool_session.has_draft:
            messages.append({
                "role": "user",
                "content": "你已经生成了主动消息草稿。现在只能调用 finish_turn(decision=reply) 完成本轮。",
            })
            finish_schemas = [
                schema for schema in schemas
                if schema.get("function", {}).get("name") == "finish_turn"
            ]
            for _attempt in range(3):
                try:
                    response = await self._complete_with_argument_retry(
                        session_key,
                        messages,
                        finish_schemas,
                    )
                    if response is None:
                        return _skip("invalid_tool_arguments")
                    calls = list(getattr(response, "tool_calls", ()) or ())
                    if len(calls) != 1 or calls[0].name != "finish_turn":
                        return _skip("terminal_correction_failed")
                    call = calls[0]
                    await tool_session.execute(call.name, dict(call.arguments))
                    decision = tool_session.decision
                    if decision is not None:
                        return {
                            "action": decision.decision,
                            "topic": decision.topic,
                            "message": decision.message,
                            "reason": decision.reason,
                        }
                except ProactiveToolError as error:
                    logger.info("主动 Agent 终止纠偏失败: session=%s error=%s", session_key, error)
                    return _skip("terminal_correction_failed")
                except Exception:
                    logger.exception("主动 Agent 终止纠偏异常: session=%s", session_key)
                    return _skip("agent_error")
            return _skip("terminal_correction_failed")
        return _skip("max_iterations")

    async def _complete_with_argument_retry(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
        schemas: object,
    ) -> Any | None:
        for attempt in range(1, 3):
            try:
                return await self._provider.complete(
                    messages,
                    tools=schemas,
                    max_tokens=800,
                    disable_thinking=True,
                )
            except json.JSONDecodeError:
                logger.warning(
                    "主动 Agent 工具参数解析失败: session=%s attempt=%d/2",
                    session_key,
                    attempt,
                )
        logger.warning(
            "主动 Agent 本轮结束: session=%s reason=invalid_tool_arguments",
            session_key,
        )
        return None

    async def _record_skip(self, state: ProactiveState, now: datetime, reason: str) -> None:
        state.last_checked_at = now.isoformat()
        state.last_skip_reason = reason
        await asyncio.to_thread(self._store.put_state, state)

    async def close(self) -> None:
        """停止后续检查并等待循环退出。"""

        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)


def _skip(reason: str) -> dict[str, str]:
    return {"action": "skip", "topic": "", "message": "", "reason": reason}


def _recent_rows(session_store: Any, session_key: str) -> list[dict[str, Any]]:
    """从长会话尾部读取最多 500 条，不能把最早 500 条误当成最近上下文。"""

    _head, total = session_store.list_chat_messages(session_key, limit=1, offset=0)
    rows, _ = session_store.list_chat_messages(
        session_key,
        limit=500,
        offset=max(0, total - 500),
    )
    return rows


def _is_proactive(row: dict[str, Any]) -> bool:
    metadata = row.get("metadata")
    return bool(row.get("proactive")) or bool(
        metadata.get("proactive") if isinstance(metadata, dict) else False
    )


__all__ = ["ProactiveChatLoop"]
