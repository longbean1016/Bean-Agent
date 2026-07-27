"""主动消息的唯一提交入口：先持久化，再交给 Channel 实时投递。"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from agent.message_bus import MessageBus, OutboundMessage
from proactive.models import DeliveryResult
from proactive.store import ProactiveStore
from session.manager import SessionManager


class ProactiveTurnService:
    """提交单条主动 assistant 消息，不伪造 user Turn 或普通 TurnCommitted。"""

    def __init__(self, store: ProactiveStore, sessions: SessionManager, bus: MessageBus) -> None:
        self._store = store
        self._sessions = sessions
        self._bus = bus

    async def deliver(
        self,
        *,
        session_key: str,
        content: str,
        source: str,
        delivery_key: str,
        source_id: str,
        tool_chain: list[dict[str, Any]] | None = None,
    ) -> DeliveryResult:
        """按 delivery_key 幂等提交；离线 Web 不影响已落库消息。"""

        text = str(content).strip()
        if not text:
            return DeliveryResult(False, reason="empty_content")
        channel, separator, chat_id = str(session_key).partition(":")
        if not separator or not channel or not chat_id:
            raise ValueError("主动消息 session_key 无效")
        message_id = uuid4().hex
        reserved = await asyncio.to_thread(
            self._store.reserve_delivery,
            delivery_key,
            session_key,
            message_id,
        )
        if not reserved:
            return DeliveryResult(False, reason="duplicate")
        metadata = {
            "proactive": True,
            "source": source,
            "source_id": source_id,
            "delivery_key": delivery_key,
            "message_id": message_id,
        }
        try:
            session = await self._sessions.get_or_create(session_key)
            message = session.add_message(
                "assistant",
                text,
                proactive=True,
                metadata=metadata,
                tool_chain=list(tool_chain or []),
                status="ok",
            )
            await self._sessions.append_messages(session, [message])
            stable_id = str(message.get("id") or message_id)
            metadata["message_id"] = stable_id
            await self._bus.publish_outbound(
                OutboundMessage(channel, chat_id, text, metadata=metadata)
            )
            return DeliveryResult(True, stable_id, metadata=metadata)
        except Exception:
            # delivery key 只能代表已经落库的消息；持久化失败必须允许调度器重试。
            await asyncio.to_thread(self._store.release_delivery, delivery_key)
            raise


__all__ = ["ProactiveTurnService"]
