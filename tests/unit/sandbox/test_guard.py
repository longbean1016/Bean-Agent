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
