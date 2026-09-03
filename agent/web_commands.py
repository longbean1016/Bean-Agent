"""与具体 Web 传输无关的命令校验和执行服务。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from agent.agent_loop import InterruptResult
from agent.message_bus import InboundMessage, MessageBus

MAX_MESSAGE_LENGTH = 32 * 1024
MAX_MEDIA_COUNT = 8


class InterruptController(Protocol):
    async def request_interrupt(self, session_key: str) -> InterruptResult: ...


class WebCommandError(Exception):
    """可由任意 Web 传输稳定转换为协议错误的命令异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class PreparedWebMessage:
    """已完成校验和会话准备、但尚未发布的入站消息。"""

    session_key: str
    request_id: str
    created_session: bool
    message: InboundMessage


class WebCommandService:
    """执行可被 WebSocket、HTTP 等传输复用的 Web 命令。"""

    def __init__(
        self,
        bus: MessageBus,
        interrupt_controller: InterruptController,
        *,
        media_root: Path | None = None,
        ensure_session: Callable[[str], Awaitable[object]] | None = None,
    ) -> None:
        self._bus = bus
        self._interrupt = interrupt_controller
        self._media_root = media_root.resolve() if media_root is not None else None
        self._ensure_session = ensure_session

    async def create_session(self) -> str:
        """先持久化空会话，使创建响应后的查询不会短暂返回 404。"""

        session_key = f"web:{uuid4().hex}"
        if self._ensure_session is not None:
            await self._ensure_session(session_key)
        return session_key

    async def prepare_message(
        self,
        *,
        request_id: str,
        session_id: object,
        text: object,
        media: object,
    ) -> PreparedWebMessage:
        content = str(text or "")
        attachments = _normalize_media(media)
        if not content.strip() and not attachments:
            raise WebCommandError("empty_message", "消息不能为空")
        if len(content) > MAX_MESSAGE_LENGTH or len(attachments) > MAX_MEDIA_COUNT:
            raise WebCommandError("message_too_large", "消息或媒体数量超过限制")
        if not self._media_is_allowed(attachments):
            raise WebCommandError("invalid_media", "附件路径无效或不属于当前 workspace")

        session_key = normalize_web_session_id(session_id)
        created_session = session_key is None
        if session_key is None:
            # 校验全部完成后才创建，避免非法首轮请求留下空会话。
            session_key = await self.create_session()
        chat_id = session_key.split(":", 1)[1]
        message = InboundMessage(
            "web",
            "web",
            chat_id,
            content,
            attachments,
            {"request_id": request_id},
        )
        return PreparedWebMessage(session_key, request_id, created_session, message)

    async def submit_message(self, prepared: PreparedWebMessage) -> None:
        await self._bus.publish_inbound(prepared.message)

    async def stop_turn(self, session_key: str) -> InterruptResult:
        return await self._interrupt.request_interrupt(session_key)

    def get_active_turn_snapshot(self, session_key: str) -> dict[str, Any] | None:
        snapshotter = getattr(self._interrupt, "get_active_turn_snapshot", None)
        if not callable(snapshotter):
            return None
        snapshot = snapshotter(session_key)
        return dict(snapshot) if snapshot else None

    def _media_is_allowed(self, media: list[str]) -> bool:
        if not media or self._media_root is None:
            return True
        for value in media:
            try:
                path = Path(value).expanduser().resolve()
                path.relative_to(self._media_root)
            except (OSError, ValueError):
                return False
            if not path.is_file():
                return False
        return True


def _normalize_media(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    ]


def normalize_web_session_id(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("web:") and text[4:]:
        return text
    if ":" not in text:
        return f"web:{text}"
    return None


__all__ = [
    "InterruptController",
    "PreparedWebMessage",
    "WebCommandError",
    "WebCommandService",
    "normalize_web_session_id",
]
