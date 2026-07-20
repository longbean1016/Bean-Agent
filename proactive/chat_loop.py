"""基于最近未完成话题的低频主动聊天循环。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

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
        *,
        is_session_busy: Callable[[str], bool],
        now_fn: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._store = store
        self._sessions = session_store
        self._provider = provider
        self._delivery = delivery
        self._is_session_busy = is_session_busy
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._rng = rng or random.Random()
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
        rows, _ = await asyncio.to_thread(self._sessions.list_chat_messages, session_key, limit=500, offset=0)
        recent = [row for row in rows if row.get("role") in {"user", "assistant"}][-12:]
        if not recent or recent[-1].get("role") != "assistant":
            # 用户尚在等待普通回复时绝不把它包装成主动消息。
            await self._record_skip(state, now, "unfinished_passive_turn")
            return
        verdict = await self._judge(recent, policy.judge_send_threshold)
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

    async def _judge(self, recent: list[dict[str, Any]], threshold: float) -> dict[str, str]:
        transcript = "\n".join(f"{row['role']}: {str(row.get('content') or '')[:1200]}" for row in recent)
        prompt = (
            "你是主动聊天判定器。仅当最近对话存在明确未完成、且现在继续有实际帮助的话题时才发送。"
            "普通问答已经完整回答、工具结果、寒暄、无新信息的追问必须跳过。"
            f"发送把握阈值为 {threshold:.2f}。只输出 JSON："
            '{"action":"reply|skip","topic":"简短主题","message":"主动消息或空字符串","reason":"原因"}。\n'
            + transcript
        )
        response = await self._provider.complete(
            [{"role": "system", "content": prompt}],
            tools=None,
            max_tokens=400,
            disable_thinking=True,
        )
        return _parse_verdict(str(response.content or ""))

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


def _parse_verdict(content: str) -> dict[str, str]:
    """严格解析判定结果；格式异常一律视为跳过，避免意外打扰用户。"""

    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match is None:
        return {"action": "skip", "topic": "", "message": "", "reason": "invalid_json"}
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"action": "skip", "topic": "", "message": "", "reason": "invalid_json"}
    action = str(payload.get("action") or "skip")
    return {
        "action": action if action in {"reply", "skip"} else "skip",
        "topic": str(payload.get("topic") or "").strip(),
        "message": str(payload.get("message") or "").strip(),
        "reason": str(payload.get("reason") or "").strip(),
    }


__all__ = ["ProactiveChatLoop"]
