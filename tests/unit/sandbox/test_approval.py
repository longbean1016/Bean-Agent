"""单次越权审批状态机测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import sandbox.approval as approval_module
from sandbox.approval import ApprovalCoordinator, ApprovalRequest
from sandbox.errors import ApprovalUnavailable


class AuditStore:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.resolved: list[tuple[str, str, str]] = []

    def create_sandbox_approval(self, request: dict[str, object]) -> None:
        self.created.append(dict(request))

    def resolve_sandbox_approval(
        self,
        request_id: str,
        state: str,
        decided_at: str,
    ) -> bool:
        self.resolved.append((request_id, state, decided_at))
        return True


class CreateFailStore(AuditStore):
    def create_sandbox_approval(self, request: dict[str, object]) -> None:
        raise RuntimeError("database unavailable")


class ResolveFailStore(AuditStore):
    def resolve_sandbox_approval(
        self,
        request_id: str,
        state: str,
        decided_at: str,
    ) -> bool:
        raise RuntimeError("database unavailable")


async def _wait_for_request(items: list[ApprovalRequest]) -> ApprovalRequest:
    for _ in range(100):
        if items:
            return items[0]
        await asyncio.sleep(0)
    raise AssertionError("审批请求没有发布")


@pytest.mark.asyncio
async def test_approval_allowed_once_is_bound_and_idempotent() -> None:
    store = AuditStore()
    published: list[ApprovalRequest] = []

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)

    coordinator = ApprovalCoordinator(store, publisher=publish)
    await coordinator.set_session_available("web:a", True)
    waiting = asyncio.create_task(coordinator.request(
        session_id="web:a",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="shell",
        operation="执行完整 Shell 命令",
        arguments={"command": "echo ok", "description": "测试"},
        reason="需要单次授权",
    ))
    request = await _wait_for_request(published)

    assert request.category == "临时权限"
    assert request.action == "执行完整 Shell 命令"
    assert request.scope_kind == "exact"
    assert request.display_scope == "仅此操作"

    first, duplicate = await asyncio.gather(
        coordinator.decide(request.id, "web:a", "allowed-once"),
        coordinator.decide(request.id, "web:a", "rejected"),
    )

    assert first == duplicate == await waiting
    assert first in {"allowed-once", "rejected"}
    assert len(store.resolved) == 1


@pytest.mark.asyncio
async def test_approval_rejects_when_ui_is_unavailable() -> None:
    store = AuditStore()
    coordinator = ApprovalCoordinator(store, publisher=lambda _request: asyncio.sleep(0))

    with pytest.raises(ApprovalUnavailable, match="审批界面"):
        await coordinator.request(
            session_id="web:offline",
            turn_id="turn-1",
            call_id="call-1",
            tool_name="write_file",
            operation="写入文件",
            arguments={"path": "D:/work/a.txt", "content": "secret"},
            reason="只读会话",
        )

    assert store.created[0]["state"] == "unavailable"
    assert store.created[0]["arguments"] == {
        "path": "D:/work/a.txt",
        "content_length": 6,
    }


@pytest.mark.asyncio
async def test_disconnect_marks_pending_request_unavailable() -> None:
    store = AuditStore()
    published: list[ApprovalRequest] = []

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)

    coordinator = ApprovalCoordinator(store, publisher=publish)
    await coordinator.set_session_available("web:a", True)
    waiting = asyncio.create_task(coordinator.request(
        session_id="web:a",
        turn_id="turn-1",
        call_id="call-1",
        tool_name="edit_file",
        operation="精确编辑文件",
        arguments={
            "path": "D:/work/a.txt",
            "old_text": "private-before",
            "new_text": "private-after",
        },
        reason="只读会话",
    ))
    request = await _wait_for_request(published)

    await coordinator.set_session_available("web:a", False)

    assert await waiting == "unavailable"
    assert request.arguments == {
        "path": "D:/work/a.txt",
        "replace_all": False,
        "old_text_length": 14,
        "new_text_length": 13,
    }
    assert store.resolved[0][1] == "unavailable"


@pytest.mark.asyncio
async def test_publisher_failure_fails_closed() -> None:
    store = AuditStore()

    async def fail(_request: ApprovalRequest) -> None:
        raise RuntimeError("socket failed")

    coordinator = ApprovalCoordinator(store, publisher=fail)
    await coordinator.set_session_available("web:a", True)

    with pytest.raises(ApprovalUnavailable, match="发送失败"):
        await coordinator.request(
            session_id="web:a",
            turn_id="turn-1",
            call_id="call-1",
            tool_name="shell",
            operation="执行完整 Shell 命令",
            arguments={"command": "echo ok"},
            reason="需要单次授权",
        )

    assert store.resolved[0][1] == "unavailable"


@pytest.mark.asyncio
async def test_audit_create_failure_does_not_leave_pending_approval() -> None:
    store = CreateFailStore()
    published: list[ApprovalRequest] = []

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)

    coordinator = ApprovalCoordinator(store, publisher=publish)
    await coordinator.set_session_available("web:a", True)

    with pytest.raises(ApprovalUnavailable, match="审批审计不可用"):
        await coordinator.request(
            session_id="web:a",
            turn_id="turn-create-fail",
            call_id="call-create-fail",
            tool_name="shell",
            operation="执行命令",
            arguments={"command": "echo blocked"},
            reason="需要授权",
        )

    assert published == []
    assert await coordinator.pending_for_session("web:a") == []


@pytest.mark.asyncio
async def test_allowed_once_audit_failure_is_fail_closed() -> None:
    store = ResolveFailStore()
    published: list[ApprovalRequest] = []

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)

    coordinator = ApprovalCoordinator(store, publisher=publish)
    await coordinator.set_session_available("web:a", True)
    waiting = asyncio.create_task(coordinator.request(
        session_id="web:a",
        turn_id="turn-resolve-fail",
        call_id="call-resolve-fail",
        tool_name="shell",
        operation="执行命令",
        arguments={"command": "echo blocked"},
        reason="需要授权",
    ))
    request = await _wait_for_request(published)

    assert await coordinator.decide(request.id, "web:a", "allowed-once") == "unavailable"
    assert await waiting == "unavailable"
    assert await coordinator.pending_for_session("web:a") == []


@pytest.mark.asyncio
async def test_approval_wait_uses_monotonic_duration_when_wall_clock_moves_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_datetime = approval_module.datetime
    wall_clock_values = iter(
        (
            real_datetime(2026, 9, 20, 12, 0, 1, tzinfo=approval_module._LOCAL_TZ),
            real_datetime(2026, 9, 20, 11, 59, 1, tzinfo=approval_module._LOCAL_TZ),
        )
    )

    class BackwardClock(real_datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "BackwardClock":
            value = next(wall_clock_values)
            if tz is not None:
                value = value.astimezone(tz)
            return cls.fromtimestamp(value.timestamp(), tz=value.tzinfo)

    monkeypatch.setattr(approval_module, "datetime", BackwardClock)
    store = AuditStore()
    published: list[ApprovalRequest] = []

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)

    coordinator = ApprovalCoordinator(store, publisher=publish)
    await coordinator.set_session_available("web:a", True)
    waiting = asyncio.create_task(coordinator.request(
        session_id="web:a",
        turn_id="turn-monotonic",
        call_id="call-monotonic",
        tool_name="shell",
        operation="执行命令",
        arguments={"command": "echo ok"},
        reason="需要授权",
    ))
    request = await _wait_for_request(published)
    assert await coordinator.decide(request.id, "web:a", "rejected") == "rejected"
    assert await waiting == "rejected"

    timing = await coordinator.timing_for_call(
        "web:a",
        "turn-monotonic",
        "call-monotonic",
    )
    assert timing["approval_wait_ms"] >= 0


@pytest.mark.asyncio
async def test_resolution_publisher_includes_call_identity_and_is_idempotent() -> None:
    store = AuditStore()
    published: list[ApprovalRequest] = []
    resolutions: list[tuple[str, str, str, str, str | None]] = []

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)

    async def publish_resolution(
        request: ApprovalRequest,
        state: str,
        decided_at: str,
        error_code: str | None,
    ) -> None:
        resolutions.append((request.id, request.turn_id, request.call_id, state, error_code))

    coordinator = ApprovalCoordinator(
        store,
        publisher=publish,
        resolution_publisher=publish_resolution,
    )
    await coordinator.set_session_available("web:a", True)
    waiting = asyncio.create_task(coordinator.request(
        session_id="web:a",
        turn_id="turn-resolution",
        call_id="call-resolution",
        tool_name="shell",
        operation="执行命令",
        arguments={"command": "echo ok"},
        reason="需要授权",
    ))
    request = await _wait_for_request(published)

    assert await coordinator.decide(request.id, "web:a", "rejected") == "rejected"
    assert await waiting == "rejected"
    # 迟到的重复 decision 只返回原结果，不重复发送 resolved 回执。
    assert await coordinator.decide(request.id, "web:a", "allowed-once") == "rejected"
    assert resolutions == [(request.id, "turn-resolution", "call-resolution", "rejected", "user_rejected")]


@pytest.mark.asyncio
async def test_disconnect_resolution_publisher_marks_unavailable() -> None:
    store = AuditStore()
    published: list[ApprovalRequest] = []
    resolutions: list[tuple[str, str | None]] = []

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)

    async def publish_resolution(
        _request: ApprovalRequest,
        state: str,
        _decided_at: str,
        error_code: str | None,
    ) -> None:
        resolutions.append((state, error_code))

    coordinator = ApprovalCoordinator(
        store,
        publisher=publish,
        resolution_publisher=publish_resolution,
    )
    await coordinator.set_session_available("web:a", True)
    waiting = asyncio.create_task(coordinator.request(
        session_id="web:a",
        turn_id="turn-disconnect",
        call_id="call-disconnect",
        tool_name="write_file",
        operation="写入文件",
        arguments={"path": "a.txt", "content": "x"},
        reason="只读会话",
    ))
    await _wait_for_request(published)
    await coordinator.set_session_available("web:a", False)

    assert await waiting == "unavailable"
    assert resolutions == [("unavailable", "unavailable")]


@pytest.mark.asyncio
async def test_timeout_is_explicitly_expired_and_late_decision_is_idempotent() -> None:
    store = AuditStore()
    published: list[ApprovalRequest] = []
    resolutions: list[tuple[str, str | None, str | None]] = []

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)

    async def publish_resolution(
        request: ApprovalRequest,
        state: str,
        _decided_at: str,
        error_code: str | None,
        client_request_id: str | None,
    ) -> None:
        resolutions.append((state, error_code, client_request_id))
        assert request.expires_at

    coordinator = ApprovalCoordinator(
        store,
        publisher=publish,
        resolution_publisher=publish_resolution,
        timeout_seconds=1.0,
    )
    await coordinator.set_session_available("web:a", True)
    waiting = asyncio.create_task(coordinator.request(
        session_id="web:a",
        turn_id="turn-expire",
        call_id="call-expire",
        tool_name="shell",
        operation="执行命令",
        arguments={"command": "echo wait"},
        reason="需要授权",
    ))
    request = await _wait_for_request(published)

    assert await waiting == "expired"
    # 超时后的迟到点击只返回已收敛状态，不能重新触发回执或放行。
    assert await coordinator.decide(
        request.id,
        "web:a",
        "allowed-once",
        client_request_id="late-decision",
    ) == "expired"
    assert resolutions == [("expired", "timeout", None)]
    assert store.resolved[0][1] == "expired"


@pytest.mark.asyncio
async def test_five_argument_resolution_callback_receives_client_request_id() -> None:
    store = AuditStore()
    published: list[ApprovalRequest] = []
    client_ids: list[str | None] = []

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)

    async def publish_resolution(
        _request: ApprovalRequest,
        _state: str,
        _decided_at: str,
        _error_code: str | None,
        client_request_id: str | None,
    ) -> None:
        client_ids.append(client_request_id)

    coordinator = ApprovalCoordinator(
        store,
        publisher=publish,
        resolution_publisher=publish_resolution,
    )
    await coordinator.set_session_available("web:a", True)
    waiting = asyncio.create_task(coordinator.request(
        session_id="web:a",
        turn_id="turn-client-id",
        call_id="call-client-id",
        tool_name="shell",
        operation="执行命令",
        arguments={"command": "echo ok"},
        reason="需要授权",
    ))
    request = await _wait_for_request(published)
    assert await coordinator.decide(
        request.id,
        "web:a",
        "allowed-once",
        client_request_id="decision-42",
    ) == "allowed-once"
    assert await waiting == "allowed-once"
    assert client_ids == ["decision-42"]


@pytest.mark.asyncio
async def test_session_grant_is_reused_in_memory_and_cleared_explicitly() -> None:
    store = AuditStore()
    published: list[ApprovalRequest] = []
    coordinator: ApprovalCoordinator

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)
        await coordinator.decide(request.id, request.session_id, "allowed-session")

    coordinator = ApprovalCoordinator(store, publisher=publish)
    await coordinator.set_session_available("web:a", True)

    async def request(call_id: str) -> str:
        return await coordinator.request(
            session_id="web:a",
            turn_id=f"turn-{call_id}",
            call_id=call_id,
            tool_name="shell",
            operation="删除文件",
            arguments={"command": "del D:\\test\\a.txt"},
            reason="删除测试文件",
            grant_key="delete:D:/test",
            scope="删除文件 · 目标目录：D:\\test",
        )

    assert await request("call-1") == "allowed-session"
    assert await request("call-2") == "allowed-session"
    assert len(published) == 1
    assert published[0].allow_session is True
    assert published[0].to_wire()["scope"] == "删除文件 · 目标目录：D:\\test"
    assert "grant_key" not in published[0].to_wire()

    await coordinator.clear_session_grants("web:a")
    assert await request("call-3") == "allowed-session"
    assert len(published) == 2


@pytest.mark.asyncio
async def test_session_grant_is_not_restored_by_new_coordinator() -> None:
    store = AuditStore()
    first: ApprovalCoordinator

    async def allow_session(request: ApprovalRequest) -> None:
        await first.decide(request.id, request.session_id, "allowed-session")

    first = ApprovalCoordinator(store, publisher=allow_session)
    await first.set_session_available("web:a", True)
    assert await first.request(
        session_id="web:a", turn_id="turn-1", call_id="call-1", tool_name="shell",
        operation="执行命令", arguments={"command": "git pull"}, reason="更新代码",
        grant_key="prefix:git-pull", scope="命令前缀：git pull",
    ) == "allowed-session"

    restarted_requests: list[ApprovalRequest] = []
    restarted: ApprovalCoordinator

    async def allow_once(request: ApprovalRequest) -> None:
        restarted_requests.append(request)
        await restarted.decide(request.id, request.session_id, "allowed-once")

    restarted = ApprovalCoordinator(store, publisher=allow_once)
    await restarted.set_session_available("web:a", True)
    assert await restarted.request(
        session_id="web:a", turn_id="turn-2", call_id="call-2", tool_name="shell",
        operation="执行命令", arguments={"command": "git pull"}, reason="更新代码",
        grant_key="prefix:git-pull", scope="命令前缀：git pull",
    ) == "allowed-once"
    assert len(restarted_requests) == 1


@pytest.mark.asyncio
async def test_keyword_only_resolution_callback_receives_client_request_id() -> None:
    store = AuditStore()
    published: list[ApprovalRequest] = []
    client_ids: list[str | None] = []

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)

    async def publish_resolution(
        _request: ApprovalRequest,
        _state: str,
        _decided_at: str,
        _error_code: str | None,
        *,
        client_request_id: str | None,
    ) -> None:
        client_ids.append(client_request_id)

    coordinator = ApprovalCoordinator(
        store,
        publisher=publish,
        resolution_publisher=publish_resolution,
    )
    await coordinator.set_session_available("web:a", True)
    waiting = asyncio.create_task(coordinator.request(
        session_id="web:a",
        turn_id="turn-keyword-client-id",
        call_id="call-keyword-client-id",
        tool_name="shell",
        operation="执行命令",
        arguments={"command": "echo ok"},
        reason="需要授权",
    ))
    request = await _wait_for_request(published)
    assert await coordinator.decide(
        request.id,
        "web:a",
        "rejected",
        client_request_id="decision-keyword",
    ) == "rejected"
    assert await waiting == "rejected"
    assert client_ids == ["decision-keyword"]


@pytest.mark.asyncio
async def test_blocked_resolution_publisher_cannot_block_decision() -> None:
    store = AuditStore()
    published: list[ApprovalRequest] = []
    blocker = asyncio.Event()

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)

    async def blocked_resolution(
        _request: ApprovalRequest,
        _state: str,
        _decided_at: str,
        _error_code: str | None,
        _client_request_id: str | None,
    ) -> None:
        await blocker.wait()

    coordinator = ApprovalCoordinator(
        store,
        publisher=publish,
        resolution_publisher=blocked_resolution,
        resolution_timeout_seconds=0.05,
    )
    await coordinator.set_session_available("web:a", True)
    waiting = asyncio.create_task(coordinator.request(
        session_id="web:a",
        turn_id="turn-blocked-publisher",
        call_id="call-blocked-publisher",
        tool_name="shell",
        operation="执行命令",
        arguments={"command": "echo ok"},
        reason="需要授权",
    ))
    request = await _wait_for_request(published)

    # 即使 UI 事件处理器永久等待，决定也必须在超时窗口内返回，且工具
    # Future 已收到明确终态。
    assert await coordinator.decide(
        request.id,
        "web:a",
        "allowed-once",
        client_request_id="decision-blocked",
    ) == "allowed-once"
    assert await waiting == "allowed-once"
