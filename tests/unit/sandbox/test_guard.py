"""SandboxGuard 的一次性审批终态文案与 fail-closed 行为测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sandbox.errors import SandboxAccessDenied
from sandbox.guard import SandboxGuard


class Resolver:
    def resolve(self, session_key: str) -> SimpleNamespace:
        return SimpleNamespace(
            session_key=session_key,
            mode="read-only",
            contains_workspace_path=lambda _target: False,
        )


class Approvals:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome

    async def request(self, **_kwargs: object) -> str:
        return self.outcome


class RecordingApprovals(Approvals):
    def __init__(self) -> None:
        super().__init__("allowed-session")
        self.requests: list[dict[str, object]] = []

    async def request(self, **kwargs: object) -> str:
        self.requests.append(dict(kwargs))
        return self.outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        ("rejected", "用户拒绝"),
        ("cancelled", "已取消"),
        ("expired", "已超时"),
        ("unavailable", "审批界面"),
    ],
)
async def test_approval_terminal_states_have_distinct_fail_closed_messages(
    outcome: str,
    message: str,
) -> None:
    guard = SandboxGuard(Resolver(), Approvals(outcome))

    with pytest.raises(SandboxAccessDenied, match=message):
        await guard.authorize_file_mutation(
            session_key="web:test",
            turn_id="turn-1",
            call_id="call-1",
            tool_name="write_file",
            arguments={"path": "outside.txt", "content": "x"},
            target=Path("outside.txt"),
            operation="写入文件",
        )


@pytest.mark.asyncio
async def test_file_session_scope_isolated_by_tool_and_target_directory(tmp_path: Path) -> None:
    approvals = RecordingApprovals()
    guard = SandboxGuard(Resolver(), approvals)
    first = tmp_path / "one" / "a.txt"
    second = tmp_path / "one" / "b.txt"
    other = tmp_path / "two" / "c.txt"

    for call_id, tool_name, target in (
        ("call-1", "write_file", first),
        ("call-2", "write_file", second),
        ("call-3", "edit_file", second),
        ("call-4", "write_file", other),
    ):
        authorized = await guard.authorize_file_mutation(
            session_key="web:test",
            turn_id="turn-1",
            call_id=call_id,
            tool_name=tool_name,
            arguments={"path": str(target), "content": "x"},
            target=target,
            operation="写入文件",
        )
        assert authorized.mode == "danger-full-access"

    keys = [str(request["grant_key"]) for request in approvals.requests]
    assert keys[0] == keys[1]
    assert keys[0] != keys[2]
    assert keys[0] != keys[3]
    assert approvals.requests[0]["category"] == "文件变更"
    assert approvals.requests[0]["action"] == "创建/编辑"
    assert approvals.requests[0]["scope_kind"] == "paths"
    assert approvals.requests[0]["display_scope"] == approvals.requests[0]["scope"]
