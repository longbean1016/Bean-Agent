"""Low-frequency proactive chat loop based on recent completed conversation state."""

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

from proactive.agent_tools import (
    ProactiveToolError,
    ProactiveToolFactory,
    waiting_for_proactive_reply,
)
from proactive.models import ProactiveState
from proactive.policy import admission_probability, check_conversation_gate, next_tick_seconds, resolve_policy
from proactive.prompt_assembler import build_proactive_messages
from proactive.store import ProactiveStore

logger = logging.getLogger(__name__)

_AUDIT_TOOL_NAMES = frozenset({
    "recall_memory",
    "web_search",
    "web_fetch",
    "read_file",
    "list_dir",
    "load_skill",
})


class CompletionProvider(Protocol):
    async def complete(self, messages: list[dict[str, Any]], tools: object = None, **kwargs: Any) -> Any: ...


class ProactiveDeliveryApi(Protocol):
    async def deliver(
        self,
        *,
        session_key: str,
        content: str,
        source: str,
        delivery_key: str,
        source_id: str,
        tool_chain: list[dict[str, Any]] | None = None,
        tools_used: list[str] | None = None,
    ) -> object: ...


class ProactiveChatLoop:
    """Periodically checks enabled sessions and performs one bounded proactive judgment."""

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
        if self._closed:
            raise RuntimeError("ProactiveChatLoop is closed")
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="proactive-chat")

    async def _run(self) -> None:
        while not self._stop.is_set():
            delay = 900
            try:
                delay = await self.run_once()
            except Exception:
                logger.exception("proactive chat check failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(1, delay))
            except TimeoutError:
                pass

    async def run_once(self) -> int:
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

        rows = await asyncio.to_thread(_recent_rows, self._sessions, session_key)
        if waiting_for_proactive_reply(rows):
            await self._record_skip(state, now, "waiting_for_reply")
            return
        passive = [
            row for row in rows
            if row.get("role") in {"user", "assistant"} and not _is_proactive(row)
        ]
        if not passive or passive[-1].get("role") != "assistant":
            await self._record_skip(state, now, "unfinished_passive_turn")
            return
        last_message_ts = await asyncio.to_thread(
            _last_chat_message_timestamp,
            self._sessions,
            session_key,
            rows,
        )
        if last_message_ts is None:
            await self._record_skip(state, now, "missing_chat_activity")
            return
        idle_minutes = max(0.0, (now - last_message_ts.astimezone(timezone.utc)).total_seconds() / 60)
        policy = resolve_policy(settings)
        probability = admission_probability(policy, idle_minutes)
        if self._rng.random() > probability:
            await self._record_skip(state, now, "probability")
            return
        verdict = await self._judge(
            session_key,
            policy.judge_send_threshold,
            idle_minutes=idle_minutes,
            recent_rows=rows,
            now=now,
        )
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
            tool_chain=list(verdict.get("tool_chain") or []),
            tools_used=list(verdict.get("tools_used") or []),
        )
        local_date = now.astimezone(ZoneInfo(settings.timezone)).date().isoformat()
        state.last_checked_at = now.isoformat()
        state.last_delivered_at = now.isoformat()
        state.daily_count = state.daily_count + 1 if state.daily_date == local_date else 1
        state.daily_date = local_date
        state.last_skip_reason = ""
        state.recent_messages = [*state.recent_messages[-19:], fingerprint]
        await asyncio.to_thread(self._store.put_state, state)

    async def _judge(
        self,
        session_key: str,
        threshold: float,
        *,
        idle_minutes: float = 0.0,
        recent_rows: list[dict[str, Any]] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Run a bounded read-only tool loop; incomplete protocol is downgraded to skip."""

        del threshold, idle_minutes
        tool_session = self._tools.create(session_key)
        rows = recent_rows if recent_rows is not None else await asyncio.to_thread(
            _recent_rows,
            self._sessions,
            session_key,
        )
        messages = build_proactive_messages(
            workspace=str(getattr(self._tools, "workspace", "") or ""),
            session_key=session_key,
            memory=_prompt_memory(getattr(self._tools, "memory", None)),
            skills=getattr(self._tools, "skills", None),
            now=now or self._now_fn().astimezone(timezone.utc),
            recent_rows=rows,
        )
        schemas = tool_session.schemas()
        previous_calls = ""
        repeated_calls = 0
        tool_chain: list[dict[str, Any]] = []
        for iteration in range(1, self._max_iterations + 1):
            try:
                response = await self._complete_with_argument_retry(session_key, messages, schemas)
                if response is None:
                    return _skip("invalid_tool_arguments", iteration)
                calls = list(getattr(response, "tool_calls", ()) or ())
                if not calls:
                    return _skip("missing_terminal_tool", iteration)
                call_signature = json.dumps(
                    [(call.name, call.arguments) for call in calls],
                    ensure_ascii=False,
                    sort_keys=True,
                )
                repeated_calls = repeated_calls + 1 if call_signature == previous_calls else 1
                previous_calls = call_signature
                if repeated_calls >= 3:
                    logger.warning("proactive agent repeated tool call: session=%s", session_key)
                    return _skip("repeated_tool_call", iteration)
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
                trace_calls: list[dict[str, Any]] = []
                for call_index, call in enumerate(calls):
                    result = await tool_session.execute(call.name, dict(call.arguments))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    })
                    if call.name in _AUDIT_TOOL_NAMES:
                        trace_calls.append({
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": dict(call.arguments),
                            "result": result,
                            "status": "ok",
                        })
                    if tool_session.decision is not None:
                        if call_index != len(calls) - 1:
                            return _skip("calls_after_finish", iteration)
                        _append_tool_chain_group(
                            tool_chain,
                            iteration=iteration,
                            calls=trace_calls,
                        )
                        decision = tool_session.decision
                        return {
                            "action": decision.decision,
                            "topic": decision.topic,
                            "message": decision.message,
                            "reason": decision.reason,
                            "iterations": iteration,
                            "tool_chain": tool_chain,
                            "tools_used": _tool_names(tool_chain),
                        }
                _append_tool_chain_group(
                    tool_chain,
                    iteration=iteration,
                    calls=trace_calls,
                )
            except ProactiveToolError as error:
                logger.info("proactive agent protocol rejected: session=%s error=%s", session_key, error)
                return _skip("tool_protocol_error", iteration)
            except Exception:
                logger.exception("proactive agent single judgment failed: session=%s", session_key)
                return _skip("agent_error", iteration)
        return _skip("max_iterations", self._max_iterations)

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
                    "proactive agent tool argument parse failed: session=%s attempt=%d/2",
                    session_key,
                    attempt,
                )
        logger.warning("proactive agent ended: session=%s reason=invalid_tool_arguments", session_key)
        return None

    async def _record_skip(self, state: ProactiveState, now: datetime, reason: str) -> None:
        state.last_checked_at = now.isoformat()
        state.last_skip_reason = reason
        await asyncio.to_thread(self._store.put_state, state)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)


def _skip(reason: str, iterations: int = 0) -> dict[str, Any]:
    return {
        "action": "skip",
        "topic": "",
        "message": "",
        "reason": reason,
        "iterations": iterations,
    }


def _append_tool_chain_group(
    tool_chain: list[dict[str, Any]],
    *,
    iteration: int,
    calls: list[dict[str, Any]],
) -> None:
    if calls:
        tool_chain.append({
            "iteration": iteration,
            "text": "",
            "calls": calls,
        })


def _tool_names(tool_chain: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(
        str(call.get("name") or "")
        for group in tool_chain
        for call in group.get("calls", [])
        if str(call.get("name") or "")
    ))


def _recent_rows(session_store: Any, session_key: str) -> list[dict[str, Any]]:
    _head, total = session_store.list_chat_messages(session_key, limit=1, offset=0)
    rows, _ = session_store.list_chat_messages(
        session_key,
        limit=500,
        offset=max(0, total - 500),
    )
    return rows


def _last_chat_message_timestamp(
    session_store: Any,
    session_key: str,
    recent_rows: list[dict[str, Any]],
) -> datetime | None:
    getter = getattr(session_store, "get_last_chat_message_timestamp", None)
    raw = getter(session_key) if callable(getter) else None
    if raw is None:
        for row in reversed(recent_rows):
            if row.get("role") in {"user", "assistant"}:
                raw = row.get("timestamp") or row.get("created_at") or row.get("ts")
                break
    if raw is None:
        return None
    parsed = datetime.fromisoformat(str(raw))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _prompt_memory(memory: Any | None) -> Any | None:
    if memory is None:
        return None
    if all(callable(getattr(memory, name, None)) for name in ("read_self", "get_memory_context")):
        return memory
    return None


def _is_proactive(row: dict[str, Any]) -> bool:
    metadata = row.get("metadata")
    return bool(row.get("proactive")) or bool(
        metadata.get("proactive") if isinstance(metadata, dict) else False
    )


__all__ = ["ProactiveChatLoop"]
