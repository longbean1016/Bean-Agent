"""单次越权审批的并发状态机。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from sandbox.errors import ApprovalUnavailable, SandboxAccessDenied

ApprovalState = Literal[
    "pending",
    "allowed-once",
    "rejected",
    "cancelled",
    "unavailable",
]
ApprovalDecision = Literal["allowed-once", "rejected"]

_LOCAL_TZ = ZoneInfo("Asia/Shanghai")


class ApprovalAuditStore(Protocol):
    def create_sandbox_approval(self, request: dict[str, object]) -> None: ...
    def resolve_sandbox_approval(
        self,
        request_id: str,
        state: str,
        decided_at: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    id: str
    session_id: str
    turn_id: str
    call_id: str
    tool_name: str
    operation: str
    arguments: dict[str, object]
    reason: str
    requested_mode: str
    fingerprint: str
    state: ApprovalState
    created_at: str

    def to_wire(self) -> dict[str, object]:
        return asdict(self)


ApprovalPublisher = Callable[[ApprovalRequest], Awaitable[None]]


class ApprovalCoordinator:
    """拥有 pending Future、幂等决议、可用性和取消边界。"""

    def __init__(
        self,
        store: ApprovalAuditStore,
        *,
        publisher: ApprovalPublisher | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._store = store
        self._publisher = publisher
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[ApprovalState]]] = {}
        self._available_sessions: set[str] = set()
        self._lock = asyncio.Lock()

    def set_publisher(self, publisher: ApprovalPublisher) -> None:
        self._publisher = publisher

    async def set_session_available(self, session_id: str, available: bool) -> None:
        async with self._lock:
            if available:
                self._available_sessions.add(session_id)
                return
            self._available_sessions.discard(session_id)
            pending_ids = [
                request_id
                for request_id, (request, _) in self._pending.items()
                if request.session_id == session_id
            ]
        for request_id in pending_ids:
            await self._finish(request_id, "unavailable")

    async def request(
        self,
        *,
        session_id: str,
        turn_id: str,
        call_id: str,
        tool_name: str,
        operation: str,
        arguments: dict[str, object],
        reason: str,
        requested_mode: str = "danger-full-access",
    ) -> ApprovalState:
        fingerprint = operation_fingerprint(tool_name, arguments, requested_mode)
        now = datetime.now(tz=_LOCAL_TZ).isoformat()
        request = ApprovalRequest(
            id=uuid4().hex,
            session_id=session_id,
            turn_id=turn_id,
            call_id=call_id,
            tool_name=tool_name,
            operation=operation,
            arguments=dict(arguments),
            reason=reason,
            requested_mode=requested_mode,
            fingerprint=fingerprint,
            state="pending",
            created_at=now,
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalState] = loop.create_future()
        async with self._lock:
            if session_id not in self._available_sessions or self._publisher is None:
                self._store.create_sandbox_approval(
                    {**request.to_wire(), "state": "unavailable"}
                )
                raise ApprovalUnavailable("当前没有可用的审批界面，已拒绝越权操作")
            duplicate = any(
                current.session_id == session_id
                and current.turn_id == turn_id
                and current.call_id == call_id
                for current, _ in self._pending.values()
            )
            if duplicate:
                raise SandboxAccessDenied("同一工具调用已经存在待处理审批")
            self._pending[request.id] = (request, future)
            self._store.create_sandbox_approval(request.to_wire())

        await self._publisher(request)
        try:
            return await asyncio.wait_for(future, timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            await self._finish(request.id, "cancelled")
            return "cancelled"
        except asyncio.CancelledError:
            await self._finish(request.id, "cancelled")
            raise

    async def decide(
        self,
        request_id: str,
        session_id: str,
        decision: ApprovalDecision,
    ) -> ApprovalState:
        if decision not in ("allowed-once", "rejected"):
            raise ValueError("审批结果只能是 allowed-once 或 rejected")
        async with self._lock:
            current = self._pending.get(request_id)
            if current is None:
                raise KeyError("审批请求不存在或已经结束")
            request, _ = current
            if request.session_id != session_id:
                raise PermissionError("审批请求不属于当前会话")
        await self._finish(request_id, decision)
        return decision

    async def pending_for_session(self, session_id: str) -> list[ApprovalRequest]:
        async with self._lock:
            return [
                request
                for request, _ in self._pending.values()
                if request.session_id == session_id
            ]

    async def cancel_session(self, session_id: str) -> None:
        async with self._lock:
            request_ids = [
                request_id
                for request_id, (request, _) in self._pending.items()
                if request.session_id == session_id
            ]
        for request_id in request_ids:
            await self._finish(request_id, "cancelled")

    async def close(self) -> None:
        async with self._lock:
            request_ids = list(self._pending)
            self._available_sessions.clear()
        for request_id in request_ids:
            await self._finish(request_id, "unavailable")

    async def _finish(self, request_id: str, state: ApprovalState) -> None:
        async with self._lock:
            current = self._pending.pop(request_id, None)
            if current is None:
                return
            _, future = current
            decided_at = datetime.now(tz=_LOCAL_TZ).isoformat()
            self._store.resolve_sandbox_approval(request_id, state, decided_at)
            if not future.done():
                future.set_result(state)


def operation_fingerprint(
    tool_name: str,
    arguments: dict[str, object],
    requested_mode: str,
) -> str:
    """稳定绑定工具名、规范化参数和本次申请的执行模式。"""

    payload = json.dumps(
        {
            "tool": str(tool_name),
            "arguments": arguments,
            "requested_mode": str(requested_mode),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "ApprovalCoordinator",
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalState",
    "operation_fingerprint",
]
