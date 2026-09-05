"""单次越权审批状态机测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

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
