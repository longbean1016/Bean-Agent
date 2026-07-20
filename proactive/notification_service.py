"""提醒与 soft 结果的独立通知出口，不写入 Agent Session。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from agent.message_bus import MessageBus, OutboundMessage
from proactive.models import ProactiveNotification
from proactive.store import ProactiveStore


class NotificationService:
    """先持久化通知，再尝试实时广播；浏览器离线时由 WebChannel 补发。"""

    def __init__(self, store: ProactiveStore, bus: MessageBus) -> None:
        self._store = store
        self._bus = bus

    async def deliver(
        self,
        *,
        session_key: str,
        content: str,
        source: str,
        source_id: str,
        scheduled_at: datetime,
        recurring: bool,
    ) -> ProactiveNotification:
        """保存独立通知并发布实时帧；发布成功不代表浏览器当前在线。"""

        channel, separator, chat_id = str(session_key).partition(":")
        if not separator or not channel or not chat_id:
            raise ValueError("通知 session_key 无效")
        item = ProactiveNotification(
            session_key=session_key,
            source=source,  # type: ignore[arg-type]
            source_id=source_id,
            content=content,
            scheduled_at=scheduled_at.astimezone(timezone.utc),
            recurring=recurring,
        )
        saved = await asyncio.to_thread(self._store.enqueue_notification, item)
        await self._bus.publish_outbound(OutboundMessage(
            channel,
            chat_id,
            saved.content,
            metadata=notification_metadata(saved),
        ))
        return saved


def notification_metadata(item: ProactiveNotification) -> dict[str, object]:
    """生成 WebSocket 与历史 API 共用的稳定通知字段。"""

    return {
        "notification": True,
        "notification_id": item.id,
        "message_id": item.id,
        "source": item.source,
        "source_id": item.source_id,
        "scheduled_at": item.scheduled_at.isoformat(),
        "generated_at": item.generated_at.isoformat(),
        "recurring": item.recurring,
    }


__all__ = ["NotificationService", "notification_metadata"]
