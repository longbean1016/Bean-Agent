"""文件和 Shell 工具共用的策略与单次审批入口。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sandbox.approval import ApprovalCoordinator
from sandbox.errors import SandboxAccessDenied
from sandbox.policy import SandboxMode, SandboxPolicy, SandboxPolicyResolver


@dataclass(frozen=True, slots=True)
class AuthorizedExecution:
    policy: SandboxPolicy
    mode: SandboxMode


class SandboxGuard:
    """只负责授权决策，不执行文件或进程操作。"""

    def __init__(
        self,
        resolver: SandboxPolicyResolver,
        approvals: ApprovalCoordinator,
    ) -> None:
        self._resolver = resolver
        self._approvals = approvals

    def policy(self, session_key: str) -> SandboxPolicy:
        return self._resolver.resolve(session_key)

    async def authorize_file_mutation(
        self,
        *,
        session_key: str,
        turn_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        target: Path,
        operation: str,
    ) -> AuthorizedExecution:
        policy = self.policy(session_key)
        if policy.mode == "danger-full-access":
            return AuthorizedExecution(policy, "danger-full-access")
        if policy.mode == "workspace-write" and policy.contains_workspace_path(target):
            return AuthorizedExecution(policy, "workspace-write")
        reason = (
            "当前会话为只读，文件写入需要单次授权"
            if policy.mode == "read-only"
            else "目标位于工作区外，写入需要单次授权"
        )
        return await self._request_once(
            policy=policy,
            session_key=session_key,
            turn_id=turn_id,
            call_id=call_id,
            tool_name=tool_name,
            operation=operation,
            arguments=arguments,
            reason=reason,
        )

    async def authorize_shell_retry(
        self,
        *,
        policy: SandboxPolicy,
        turn_id: str,
        call_id: str,
        arguments: dict[str, Any],
    ) -> AuthorizedExecution:
        return await self._request_once(
            policy=policy,
            session_key=policy.session_key,
            turn_id=turn_id,
            call_id=call_id,
            tool_name="shell",
            operation="执行完整 Shell 命令",
            arguments=arguments,
            reason="命令在当前写入边界内被 Windows 拒绝，可仅对这次完整命令放宽限制",
        )

    async def _request_once(
        self,
        *,
        policy: SandboxPolicy,
        session_key: str,
        turn_id: str,
        call_id: str,
        tool_name: str,
        operation: str,
        arguments: dict[str, Any],
        reason: str,
    ) -> AuthorizedExecution:
        if not turn_id or not call_id:
            raise SandboxAccessDenied("缺少 Turn 或工具调用身份，不能申请越权授权")
        outcome = await self._approvals.request(
            session_id=session_key,
            turn_id=turn_id,
            call_id=call_id,
            tool_name=tool_name,
            operation=operation,
            arguments=dict(arguments),
            reason=reason,
        )
        if outcome != "allowed-once":
            messages = {
                "rejected": "用户拒绝了本次越权操作",
                "cancelled": "本次越权授权已取消或超时",
                "unavailable": "当前没有可用审批界面",
            }
            raise SandboxAccessDenied(messages.get(outcome, "本次越权操作未获授权"))
        return AuthorizedExecution(policy, "danger-full-access")


def resolve_tool_target(raw_path: str, policy: SandboxPolicy) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = policy.cwd / candidate
    return candidate.resolve()


__all__ = ["AuthorizedExecution", "SandboxGuard", "resolve_tool_target"]
