"""单次越权审批的并发状态机。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from agent.tool_projection import project_tool_arguments
from sandbox.errors import ApprovalUnavailable, SandboxAccessDenied

ApprovalState = Literal[
    "pending",
    "allowed-once",
    "rejected",
    "cancelled",
    "expired",
    "unavailable",
]
ApprovalDecision = Literal["allowed-once", "rejected"]

_LOCAL_TZ = ZoneInfo("Asia/Shanghai")

logger = logging.getLogger(__name__)


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
    expires_at: str | None = None

    def to_wire(self) -> dict[str, object]:
        return asdict(self)


ApprovalPublisher = Callable[[ApprovalRequest], Awaitable[None]]
# 终态回执发布器允许旧的四参数回调继续工作；新回调可再接收
# ``client_request_id``，用于把 approval.decide 与 approval.resolved 精确关联。
# 使用宽泛 Callable 是为了兼容旧插件/测试替身，实际调用由
# ``_publish_resolution`` 根据签名选择四参数或五参数形式。
ApprovalResolutionPublisher = Callable[..., Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ApprovalResolution:
    """审批首次收敛时保留的不可变元数据。"""

    request_id: str
    session_id: str
    turn_id: str
    call_id: str
    state: ApprovalState
    decided_at: str
    decision: ApprovalDecision | None = None
    error_code: str | None = None
    client_request_id: str | None = None
    requested_at: str = ""
    approval_wait_ms: int | None = None


class ApprovalCoordinator:
    """拥有 pending Future、幂等决议、可用性和取消边界。"""

    def __init__(
        self,
        store: ApprovalAuditStore,
        *,
        publisher: ApprovalPublisher | None = None,
        resolution_publisher: ApprovalResolutionPublisher | None = None,
        timeout_seconds: float = 300.0,
        resolution_timeout_seconds: float = 2.0,
    ) -> None:
        self._store = store
        self._publisher = publisher
        self._resolution_publisher = resolution_publisher
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        # UI 回执是观察面，不能阻塞已经收敛的执行 Future；保留一个较短
        # 的可配置上限，测试和嵌入式调用方可使用更小值快速验证故障路径。
        self._resolution_timeout_seconds = max(
            0.01,
            float(resolution_timeout_seconds),
        )
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[ApprovalState]]] = {}
        # 审批等待必须使用单调时钟；created_at/decided_at 只用于展示和审计，
        # 系统时间跳变不能把等待时长算成负数或超过工具总耗时。
        self._pending_started_monotonic: dict[str, float] = {}
        # 终态保留完整身份和时间，迟到/重复 decision 可以安全返回原结果，
        # 同时不会再次发布 approval.resolved。
        self._resolved: dict[str, ApprovalResolution] = {}
        self._available_sessions: set[str] = set()
        self._lock = asyncio.Lock()

    def set_publisher(self, publisher: ApprovalPublisher) -> None:
        self._publisher = publisher

    def set_resolution_publisher(
        self,
        publisher: ApprovalResolutionPublisher | None,
    ) -> None:
        """设置审批终态回执出口；同一请求只会按首次收敛发送一次。"""

        self._resolution_publisher = publisher

    @property
    def has_resolution_publisher(self) -> bool:
        return self._resolution_publisher is not None

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
            await self._finish(request_id, "unavailable", error_code="unavailable")

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
        expires_at = (
            datetime.fromisoformat(now) + timedelta(seconds=self._timeout_seconds)
        ).isoformat()
        request = ApprovalRequest(
            id=uuid4().hex,
            session_id=session_id,
            turn_id=turn_id,
            call_id=call_id,
            tool_name=tool_name,
            operation=operation,
            arguments=_display_arguments(tool_name, arguments),
            reason=reason,
            requested_mode=requested_mode,
            fingerprint=fingerprint,
            state="pending",
            created_at=now,
            expires_at=expires_at,
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalState] = loop.create_future()
        started_monotonic = loop.time()
        async with self._lock:
            if session_id not in self._available_sessions or self._publisher is None:
                decided_at = datetime.now(tz=_LOCAL_TZ).isoformat()
                try:
                    self._store.create_sandbox_approval(
                        {
                            **request.to_wire(),
                            "state": "unavailable",
                            "decided_at": decided_at,
                        }
                    )
                except Exception as error:
                    logger.exception(
                        "写入 unavailable 审批审计失败 request_id=%s",
                        request.id,
                    )
                    raise ApprovalUnavailable("审批审计不可用，已拒绝越权操作") from error
                # 没有可用 UI 时不会产生 approval.requested，但审计行仍然
                # 已经是终态，不能留下永远没有 decided_at 的 unavailable 记录。
                try:
                    self._store.resolve_sandbox_approval(
                        request.id,
                        "unavailable",
                        decided_at,
                    )
                except Exception:
                    logger.exception(
                        "写入 unavailable 审批审计失败 request_id=%s",
                        request.id,
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
            # 先确认审计事实可写，再放入 pending；否则 create 失败会留下
            # 一个没有审计行、也无法被用户决议的幽灵审批。
            try:
                self._store.create_sandbox_approval(request.to_wire())
            except Exception as error:
                logger.exception(
                    "写入审批申请审计失败 request_id=%s",
                    request.id,
                )
                raise ApprovalUnavailable("审批审计不可用，已拒绝越权操作") from error
            self._pending[request.id] = (request, future)
            self._pending_started_monotonic[request.id] = started_monotonic

        deadline = loop.time() + self._timeout_seconds
        try:
            # 发布请求本身也属于审批等待的一部分；连接卡死不能绕过
            # expires_at 无限占用工具执行协程。
            await asyncio.wait_for(
                self._publisher(request),
                timeout=max(0.0, deadline - loop.time()),
            )
        except asyncio.TimeoutError:
            outcome = await self._finish(request.id, "expired", error_code="timeout")
            return outcome or "expired"
        except asyncio.CancelledError:
            await self._finish(request.id, "cancelled", error_code="cancelled")
            raise
        except Exception as error:
            outcome = await self._finish(
                request.id,
                "unavailable",
                error_code="unavailable",
            )
            # 如果发布器抛错与用户点击并发，已收敛的用户决定优先于
            # “发送失败”兜底，避免把一次明确允许误报成 unavailable。
            if outcome in {
                "allowed-once",
                "rejected",
                "cancelled",
                "expired",
                "unavailable",
            } and outcome != "unavailable":
                return outcome
            raise ApprovalUnavailable("审批请求发送失败，已拒绝越权操作") from error
        try:
            # shield 防止 wait_for 超时/调用方取消时先把内部 Future 标记为
            # cancelled；_finish 需要拥有唯一的终态收敛权并设置可观察结果。
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=max(0.0, deadline - loop.time()),
            )
        except asyncio.TimeoutError:
            outcome = await self._finish(request.id, "expired", error_code="timeout")
            return outcome or "expired"
        except asyncio.CancelledError:
            await self._finish(request.id, "cancelled", error_code="cancelled")
            raise

    async def decide(
        self,
        request_id: str,
        session_id: str,
        decision: ApprovalDecision,
        *,
        client_request_id: str | None = None,
    ) -> ApprovalState:
        if decision not in ("allowed-once", "rejected"):
            raise ValueError("审批结果只能是 allowed-once 或 rejected")
        async with self._lock:
            current = self._pending.get(request_id)
            if current is None:
                resolved = self._resolved.get(request_id)
                if resolved is None:
                    raise KeyError("审批请求不存在或已经结束")
                if resolved.session_id != session_id:
                    raise PermissionError("审批请求不属于当前会话")
                # 所有终态都幂等返回，包括 expired/cancelled/unavailable；
                # 调用方可据此停止重试而不会把旧审批重新打开。
                return resolved.state
            request, _ = current
            if request.session_id != session_id:
                raise PermissionError("审批请求不属于当前会话")
        # 把 request_id 一起交给 _finish；最终抢到锁的决定才会写入回执，
        # 并发点击不会出现“状态由 A 决定、request_id 却来自 B”的错配。
        outcome = await self._finish(
            request_id,
            decision,
            client_request_id=client_request_id,
        )
        if outcome in ("allowed-once", "rejected"):
            return outcome
        # 竞态下可能由 timeout/disconnect 先收敛；返回其真实终态，保持
        # approval.decide 的幂等语义，不把迟到点击伪装成协议错误。
        if outcome in ("cancelled", "expired", "unavailable"):
            return outcome
        raise KeyError("审批请求已经失效")

    async def pending_for_session(self, session_id: str) -> list[ApprovalRequest]:
        async with self._lock:
            return [
                request
                for request, _ in self._pending.values()
                if request.session_id == session_id
            ]

    async def timing_for_call(
        self,
        session_id: str,
        turn_id: str,
        call_id: str,
    ) -> dict[str, object]:
        """返回当前工具调用的审批时间线，不暴露审批参数或内部指纹。

        一个工具在重试路径上可能经历多次一次性审批；请求时间取最早值，
        解决时间取最后一个终态，等待时长按各段审批等待累加。仍在等待时
        只返回 requested_at，避免用当前时间伪造历史耗时。
        """

        if not session_id or not turn_id or not call_id:
            return {}
        async with self._lock:
            pending = [
                request
                for request, _ in self._pending.values()
                if request.session_id == session_id
                and request.turn_id == turn_id
                and request.call_id == call_id
            ]
            resolved = [
                item
                for item in self._resolved.values()
                if item.session_id == session_id
                and item.turn_id == turn_id
                and item.call_id == call_id
            ]
        requested_values = [request.created_at for request in pending]
        requested_values.extend(item.requested_at for item in resolved if item.requested_at)
        if not requested_values:
            return {}
        # pending/resolved 均按 coordinator 的插入顺序收集；不要用 wall-clock
        # 排序推断先后，系统时间回拨时仍保持稳定的展示起点。
        requested_at = requested_values[0]
        result: dict[str, object] = {"approval_requested_at": requested_at}
        resolved_values = [item for item in resolved if item.decided_at]
        if resolved_values:
            latest = resolved_values[-1]
            result["approval_resolved_at"] = latest.decided_at
            waits = [
                item.approval_wait_ms
                for item in resolved_values
                if item.approval_wait_ms is not None and item.approval_wait_ms >= 0
            ]
            if waits:
                result["approval_wait_ms"] = sum(waits)
        return result

    async def cancel_session(self, session_id: str) -> None:
        async with self._lock:
            request_ids = [
                request_id
                for request_id, (request, _) in self._pending.items()
                if request.session_id == session_id
            ]
        for request_id in request_ids:
            await self._finish(request_id, "cancelled", error_code="cancelled")

    async def close(self) -> None:
        async with self._lock:
            request_ids = list(self._pending)
            self._available_sessions.clear()
        for request_id in request_ids:
            await self._finish(request_id, "unavailable", error_code="unavailable")

    async def _finish(
        self,
        request_id: str,
        state: ApprovalState,
        *,
        error_code: str | None = None,
        client_request_id: str | None = None,
    ) -> ApprovalState | None:
        resolution: tuple[ApprovalRequest, ApprovalResolution] | None = None
        async with self._lock:
            current = self._pending.pop(request_id, None)
            if current is None:
                resolved = self._resolved.get(request_id)
                return resolved.state if resolved is not None else None
            request, future = current
            started_monotonic = self._pending_started_monotonic.pop(request_id, None)
            decided_at = datetime.now(tz=_LOCAL_TZ).isoformat()
            effective_state = state
            effective_error_code = error_code or ("user_rejected" if state == "rejected" else None)
            try:
                persisted = self._store.resolve_sandbox_approval(request_id, state, decided_at)
                if not persisted and state == "allowed-once":
                    raise RuntimeError("审批审计行未更新")
            except Exception:
                # 审计写入失败不能让 pending Future 永久悬挂或把权限放开；
                # 内存状态仍按 fail-closed 终态收敛，并保留日志供诊断。
                logger.exception(
                    "写入审批终态审计失败 request_id=%s state=%s",
                    request_id,
                    state,
                )
                # 审计事实无法确认时绝不能继续放行一次性越权操作；其它拒绝类
                # 终态本身已经是安全收敛，保留原状态但记录错误码。
                if state == "allowed-once":
                    effective_state = "unavailable"
                effective_error_code = effective_error_code or "audit_unavailable"
            approval_wait_ms: int | None = None
            if started_monotonic is not None:
                approval_wait_ms = max(
                    0,
                    int(round((asyncio.get_running_loop().time() - started_monotonic) * 1000)),
                )
            resolution_value = ApprovalResolution(
                request_id=request.id,
                session_id=request.session_id,
                turn_id=request.turn_id,
                call_id=request.call_id,
                state=effective_state,
                decided_at=decided_at,
                requested_at=request.created_at,
                approval_wait_ms=approval_wait_ms,
                decision=effective_state if effective_state in ("allowed-once", "rejected") else None,
                error_code=effective_error_code,
                client_request_id=client_request_id,
            )
            self._resolved[request_id] = resolution_value
            # 终态只服务于短期迟到/重复决议幂等；限制内存窗口，避免长时间运行的
            # Agent 因历史审批数量无限增长。审计事实仍完整保存在 sandbox_approvals。
            if len(self._resolved) > 1024:
                oldest = next(iter(self._resolved))
                self._resolved.pop(oldest, None)
            if not future.done():
                future.set_result(effective_state)
            resolution = (request, resolution_value)
        # 回执不能在锁内发送，否则断线/订阅处理再次访问 coordinator 时会死锁。
        if resolution is not None and self._resolution_publisher is not None:
            request, resolution_value = resolution
            try:
                await asyncio.wait_for(
                    self._publish_resolution(request, resolution_value),
                    timeout=self._resolution_timeout_seconds,
                )
            except asyncio.TimeoutError:
                # 决定、审计和执行 Future 已经完成；回执超时只影响 UI，不能
                # 回滚或重新打开权限。wait_for 会取消底层 publisher 协程。
                logger.warning(
                    "审批终态回执发布超时 request_id=%s state=%s",
                    request.id,
                    resolution_value.state,
                )
        return resolution[1].state if resolution is not None else state

    async def _publish_resolution(
        self,
        request: ApprovalRequest,
        resolution: ApprovalResolution,
    ) -> None:
        """调用四/五参数回调并隔离 UI 传输故障。"""

        publisher = self._resolution_publisher
        if publisher is None:
            return
        try:
            # 老版本 callback 是四参数；新版本可能声明第五个位置参数、
            # keyword-only ``client_request_id`` 或 ``*args``/``**kwargs``。
            # 仅根据签名选择一次调用，避免 callback 内部 TypeError 导致
            # 重试而产生重复副作用。
            accepts_client_id = False
            accepts_client_id_keyword = False
            try:
                signature = inspect.signature(publisher)
            except (TypeError, ValueError):
                signature = None
            if signature is not None:
                positional = [
                    parameter
                    for parameter in signature.parameters.values()
                    if parameter.kind
                    in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                ]
                keyword_only = signature.parameters.get("client_request_id")
                accepts_client_id_keyword = bool(
                    keyword_only is not None
                    and keyword_only.kind == inspect.Parameter.KEYWORD_ONLY
                )
                accepts_client_id = any(
                    parameter.kind == inspect.Parameter.VAR_POSITIONAL
                    for parameter in signature.parameters.values()
                ) or len(positional) >= 5 or accepts_client_id_keyword
                if not accepts_client_id and any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                ):
                    accepts_client_id_keyword = True
            if accepts_client_id_keyword and len(positional) < 5:
                await publisher(
                    request,
                    resolution.state,
                    resolution.decided_at,
                    resolution.error_code,
                    client_request_id=resolution.client_request_id,
                )
            elif accepts_client_id:
                await publisher(
                    request,
                    resolution.state,
                    resolution.decided_at,
                    resolution.error_code,
                    resolution.client_request_id,
                )
            else:
                await publisher(
                    request,
                    resolution.state,
                    resolution.decided_at,
                    resolution.error_code,
                )
        except Exception:
            # 审批决定已经落库并唤醒执行方；UI 回执失败不能重新打开权限。
            return


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


def _elapsed_iso_ms(started_at: str, ended_at: str) -> int | None:
    """仅用已记录的两端时间计算审批等待，不以读取时刻补算。"""

    try:
        started = datetime.fromisoformat(str(started_at))
        ended = datetime.fromisoformat(str(ended_at))
    except (TypeError, ValueError):
        return None
    return max(0, int(round((ended - started).total_seconds() * 1000)))


def _display_arguments(
    tool_name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    """审批只展示执行边界，不把待写正文复制到 UI 和审计表。"""

    return project_tool_arguments(tool_name, arguments)


__all__ = [
    "ApprovalCoordinator",
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalResolution",
    "ApprovalResolutionPublisher",
    "ApprovalState",
    "operation_fingerprint",
]
