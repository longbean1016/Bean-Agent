"""前端通道与 AgentLoop 共享的消息类型及进程内队列。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeAlias

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class InboundMessage:
    channel: str
    sender: str
    chat_id: str
    content: str
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        return f"{self.channel}:{self.chat_id}"


@dataclass(slots=True)
class OutboundMessage:
    channel: str
    chat_id: str
    content: str
    thinking: str = ""
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PipelineResult:
    content: str
    thinking: str = ""
    media: list[str] = field(default_factory=list)
    tool_chain: list[dict[str, Any]] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)


OutboundCallback: TypeAlias = Callable[[OutboundMessage], Awaitable[None] | None]


class MessageBus:
    """单 AgentLoop 消费的无界消息队列。"""

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._subscribers: dict[str, list[OutboundCallback]] = {}

    async def publish_inbound(self, message: InboundMessage) -> None:
        await self._inbound.put(message)

    async def consume_inbound(self) -> InboundMessage:
        return await self._inbound.get()

    def complete_inbound(self) -> None:
        self._inbound.task_done()

    async def publish_outbound(self, message: OutboundMessage) -> None:
        await self._outbound.put(message)
        for callback in list(self._subscribers.get(message.channel, ())):
            try:
                result = callback(message)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # 一个 WebSocket 发送失败不能阻止同渠道其它连接收到最终消息。
                logger.exception("出站消息订阅者执行失败: channel=%s", message.channel)

    async def consume_outbound(self) -> OutboundMessage:
        return await self._outbound.get()

    def complete_outbound(self) -> None:
        self._outbound.task_done()

    def subscribe_outbound(self, channel: str, callback: OutboundCallback) -> None:
        self._subscribers.setdefault(channel, []).append(callback)

    def unsubscribe_outbound(self, channel: str, callback: OutboundCallback) -> None:
        callbacks = self._subscribers.get(channel)
        if not callbacks:
            return
        try:
            callbacks.remove(callback)
        except ValueError:
            return
        if not callbacks:
            self._subscribers.pop(channel, None)


__all__ = ["InboundMessage", "MessageBus", "OutboundMessage", "PipelineResult"]
