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

RouteResolver = Callable[[str, dict[str, Any] | None], dict[str, Any] | None]


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
        ensure_session: Callable[..., Awaitable[object]] | None = None,
        sandbox_loader: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
        sandbox_mode_writer: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
        workspace_writer: Callable[[str, str | None], Awaitable[dict[str, Any]]] | None = None,
        approvals: Any | None = None,
        route_resolver: RouteResolver | None = None,
    ) -> None:
        self._bus = bus
        self._interrupt = interrupt_controller
        self._media_root = media_root.resolve() if media_root is not None else None
        self._ensure_session = ensure_session
        self._sandbox_loader = sandbox_loader
        self._sandbox_mode_writer = sandbox_mode_writer
        self._workspace_writer = workspace_writer
        self._approvals = approvals
        self._route_resolver = route_resolver

    async def create_session(
        self,
        *,
        workspace_id: str | None = None,
        sandbox_mode: str = "read-only",
        risk_confirmed: bool = False,
    ) -> str:
        """先持久化空会话，使创建响应后的查询不会短暂返回 404。"""

        _validate_sandbox_mode(sandbox_mode, risk_confirmed=risk_confirmed)
        session_key = f"web:{uuid4().hex}"
        if self._ensure_session is not None:
            if workspace_id is None and sandbox_mode == "read-only":
                await self._ensure_session(session_key)
            else:
                await self._ensure_session(
                    session_key,
                    workspace_id=workspace_id,
                    sandbox_mode=sandbox_mode,
                )
        return session_key

    async def prepare_message(
        self,
        *,
        request_id: str,
        session_id: object,
        text: object,
        media: object,
        workspace_id: str | None = None,
        sandbox_mode: str = "read-only",
        risk_confirmed: bool = False,
        model_route: object = None,
    ) -> PreparedWebMessage:
        content = str(text or "")
        attachments = _normalize_media(media)
        if not content.strip() and not attachments:
            raise WebCommandError("empty_message", "消息不能为空")
        if len(content) > MAX_MESSAGE_LENGTH or len(attachments) > MAX_MEDIA_COUNT:
            raise WebCommandError("message_too_large", "消息或媒体数量超过限制")
        if not self._media_is_allowed(attachments):
            raise WebCommandError("invalid_media", "附件路径无效或不属于当前 workspace")

        metadata: dict[str, Any] = {"request_id": request_id}
        if self._route_resolver is not None:
            if model_route is not None and not isinstance(model_route, dict):
                raise WebCommandError("invalid_model_route", "模型路由格式无效")
            requested: dict[str, Any] | None = None
            if isinstance(model_route, dict):
                # 只允许连接、模型和思考强度穿过消息边界，密钥等敏感字段由服务端解析。
                requested = {
                    "connection_id": str(model_route.get("connection_id") or "").strip(),
                    "model_id": str(model_route.get("model_id") or "").strip(),
                    "reasoning_effort": str(model_route.get("reasoning_effort") or "").strip() or None,
                }
                if not requested["connection_id"] or not requested["model_id"]:
                    raise WebCommandError("invalid_model_route", "请选择有效模型")

        session_key = normalize_web_session_id(session_id)
        created_session = session_key is None
        if session_key is None:
            # 校验全部完成后才创建，避免非法首轮请求留下空会话。
            session_key = await self.create_session(
                workspace_id=workspace_id,
                sandbox_mode=sandbox_mode,
                risk_confirmed=risk_confirmed,
            )
        chat_id = session_key.split(":", 1)[1]
        if self._route_resolver is not None:
            try:
                resolved_route = self._route_resolver(session_key, requested)
                if resolved_route is not None:
                    metadata["model_route"] = resolved_route
            except (LookupError, ValueError, RuntimeError) as error:
                raise WebCommandError("invalid_model_route", str(error)) from error
        message = InboundMessage(
            "web",
            "web",
            chat_id,
            content,
            attachments,
            metadata,
        )
        return PreparedWebMessage(session_key, request_id, created_session, message)

    async def submit_message(self, prepared: PreparedWebMessage) -> None:
        await self._bus.publish_inbound(prepared.message)

    async def stop_turn(self, session_key: str) -> InterruptResult:
        if self._approvals is not None:
            await self._approvals.cancel_session(session_key)
        return await self._interrupt.request_interrupt(session_key)

    async def sandbox_snapshot(self, session_key: str) -> dict[str, Any] | None:
        if self._sandbox_loader is None:
            return None
        return await self._sandbox_loader(session_key)

    async def pending_approvals(self, session_key: str) -> list[Any]:
        if self._approvals is None:
            return []
        return await self._approvals.pending_for_session(session_key)

    async def set_session_available(self, session_key: str, available: bool) -> None:
        if self._approvals is not None:
            await self._approvals.set_session_available(session_key, available)

    async def set_sandbox_mode(
        self,
        session_key: str,
        sandbox_mode: str,
        *,
        risk_confirmed: bool = False,
    ) -> dict[str, Any]:
        _validate_sandbox_mode(sandbox_mode, risk_confirmed=risk_confirmed)
        if self._sandbox_mode_writer is None:
            raise WebCommandError("sandbox_unavailable", "沙箱权限服务不可用")
        if self._session_busy(session_key):
            raise WebCommandError("session_busy", "会话运行或排队期间不能切换权限")
        try:
            return await self._sandbox_mode_writer(session_key, sandbox_mode)
        except (KeyError, ValueError) as error:
            raise WebCommandError("invalid_sandbox", str(error)) from error

    async def bind_workspace(
        self,
        session_key: str,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        if self._workspace_writer is None:
            raise WebCommandError("sandbox_unavailable", "工作区服务不可用")
        if self._session_busy(session_key):
            raise WebCommandError("session_busy", "会话运行期间不能切换工作目录")
        try:
            return await self._workspace_writer(session_key, workspace_id)
        except (KeyError, ValueError) as error:
            raise WebCommandError("invalid_workspace", str(error)) from error

    async def decide_approval(
        self,
        approval_id: str,
        session_key: str,
        decision: str,
    ) -> None:
        if self._approvals is None:
            raise WebCommandError("approval_unavailable", "审批服务不可用")
        if decision not in {"allowed-once", "rejected"}:
            raise WebCommandError("invalid_approval", "审批结果无效")
        try:
            await self._approvals.decide(approval_id, session_key, decision)
        except (KeyError, PermissionError, ValueError) as error:
            raise WebCommandError("invalid_approval", str(error)) from error

    def get_active_turn_snapshot(self, session_key: str) -> dict[str, Any] | None:
        snapshotter = getattr(self._interrupt, "get_active_turn_snapshot", None)
        if not callable(snapshotter):
            return None
        snapshot = snapshotter(session_key)
        return dict(snapshot) if snapshot else None

    def _session_busy(self, session_key: str) -> bool:
        checker = getattr(self._interrupt, "is_session_busy", None)
        return bool(checker(session_key)) if callable(checker) else False

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


def _validate_sandbox_mode(mode: str, *, risk_confirmed: bool) -> None:
    if mode not in {"read-only", "workspace-write", "danger-full-access"}:
        raise WebCommandError("invalid_sandbox", f"不支持的沙箱权限：{mode or 'empty'}")
    if mode == "danger-full-access" and not risk_confirmed:
        raise WebCommandError("invalid_sandbox", "完全访问必须完成风险确认")


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
    "RouteResolver",
    "PreparedWebMessage",
    "WebCommandError",
    "WebCommandService",
    "normalize_web_session_id",
]
