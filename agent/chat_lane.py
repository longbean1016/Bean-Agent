"""按会话协调普通回复与主动消息的最终发送顺序。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

_T = TypeVar("_T")


@dataclass(slots=True)
class _LaneState:
    """单个会话的发送状态；不同会话不能共享 Condition。"""

    condition: asyncio.Condition
    passive_turns: int = 0
    passive_sends: int = 0
    sending: bool = False
    next_non_passive_ticket: int = 0
    serving_non_passive_ticket: int = 0
    cancelled_non_passive_tickets: set[int] = field(default_factory=set)


class ChatLaneManager:
    """普通回复优先，提醒和主动聊天按进入顺序串行发送。"""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], _LaneState] = {}

    def _state(self, channel: str, chat_id: str) -> _LaneState:
        key = (str(channel), str(chat_id))
        state = self._states.get(key)
        if state is None:
            state = _LaneState(asyncio.Condition())
            self._states[key] = state
        return state

    @staticmethod
    def _skip_cancelled(state: _LaneState) -> None:
        # 被取消的等待者仍占有票号；必须显式跨过，否则后续主动消息会永久等待。
        while state.serving_non_passive_ticket in state.cancelled_non_passive_tickets:
            state.cancelled_non_passive_tickets.remove(
                state.serving_non_passive_ticket
            )
            state.serving_non_passive_ticket += 1

    async def mark_passive_pending(self, channel: str, chat_id: str) -> None:
        state = self._state(channel, chat_id)
        async with state.condition:
            state.passive_turns += 1
            state.condition.notify_all()

    async def mark_passive_done(self, channel: str, chat_id: str) -> None:
        state = self._state(channel, chat_id)
        async with state.condition:
            if state.passive_turns > 0:
                state.passive_turns -= 1
            state.condition.notify_all()

    async def mark_passive_send_pending(self, channel: str, chat_id: str) -> None:
        state = self._state(channel, chat_id)
        async with state.condition:
            state.passive_sends += 1
            state.condition.notify_all()

    async def run_passive(
        self,
        channel: str,
        chat_id: str,
        send: Callable[[], Awaitable[_T]],
    ) -> _T:
        state = self._state(channel, chat_id)
        async with state.condition:
            while state.sending:
                await state.condition.wait()
            state.sending = True
        try:
            return await send()
        finally:
            async with state.condition:
                if state.passive_sends > 0:
                    state.passive_sends -= 1
                state.sending = False
                state.condition.notify_all()

    async def run_non_passive(
        self,
        channel: str,
        chat_id: str,
        send: Callable[[], Awaitable[_T]],
    ) -> _T:
        state = self._state(channel, chat_id)
        ticket = -1
        acquired = False
        try:
            async with state.condition:
                ticket = state.next_non_passive_ticket
                state.next_non_passive_ticket += 1
                self._skip_cancelled(state)
                while (
                    state.sending
                    or state.passive_turns > 0
                    or state.passive_sends > 0
                    or ticket != state.serving_non_passive_ticket
                ):
                    await state.condition.wait()
                    self._skip_cancelled(state)
                state.sending = True
                acquired = True
            return await send()
        finally:
            async with state.condition:
                if ticket >= 0:
                    if acquired:
                        state.sending = False
                        state.serving_non_passive_ticket += 1
                    else:
                        state.cancelled_non_passive_tickets.add(ticket)
                    self._skip_cancelled(state)
                state.condition.notify_all()


__all__ = ["ChatLaneManager"]
