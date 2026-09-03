"""文件和 Shell 工具接入真实 Windows 沙箱的闭环测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sandbox.approval import ApprovalCoordinator, ApprovalRequest
from sandbox.filesystem import FilesystemMutationBroker
from sandbox.guard import SandboxGuard
from sandbox.policy import SandboxPolicyResolver
from sandbox.runtime import SandboxProcessRuntime
from session.store import SessionStore
from tools.filesystem import WriteFileTool
from tools.shell import ShellTool


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL 后端只在 Windows 验证")
@pytest.mark.asyncio
async def test_filesystem_worker_honors_workspace_and_single_use_approval(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    runtime_root = tmp_path / "runtime-temp"
    project.mkdir()
    outside.mkdir()
    store = SessionStore(data_root / "sessions.db")
    workspace = store.create_workspace(str(project))
    store.create_session(
        "web:writable",
        workspace_id=workspace["id"],
        sandbox_mode="workspace-write",
    )
    store.create_session("web:readonly")
    resolver = SandboxPolicyResolver(
        store,
        data_root=data_root,
        runtime_temp_root=runtime_root,
    )
    runtime = SandboxProcessRuntime(resolver)
    approvals = ApprovalCoordinator(store)
    published: list[ApprovalRequest] = []

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)
        decision = "allowed-once" if len(published) == 1 else "rejected"
        await approvals.decide(request.id, request.session_id, decision)

    approvals.set_publisher(publish)
    await approvals.set_session_available("web:readonly", True)
    guard = SandboxGuard(resolver, approvals)
    broker = FilesystemMutationBroker(resolver, runtime)
    tool = WriteFileTool(sandbox_guard=guard, mutation_broker=broker)
    try:
        inside_result = await tool.execute(
            "inside.txt",
            "workspace",
            session_key="web:writable",
            turn_id="turn-1",
            call_id="call-inside",
        )
        approved_result = await tool.execute(
            str(outside / "approved.txt"),
            "private-content",
            session_key="web:readonly",
            turn_id="turn-2",
            call_id="call-approved",
        )
        rejected_result = await tool.execute(
            str(outside / "rejected.txt"),
            "blocked-content",
            session_key="web:readonly",
            turn_id="turn-2",
            call_id="call-rejected",
        )
    finally:
        await approvals.close()
        await runtime.close()
        store.close()

    assert "已写入" in inside_result
    assert (project / "inside.txt").read_text(encoding="utf-8") == "workspace"
    assert "已写入" in approved_result
    assert (outside / "approved.txt").read_text(encoding="utf-8") == "private-content"
    assert "用户拒绝" in rejected_result
    assert not (outside / "rejected.txt").exists()
    assert len(published) == 2
    assert published[0].arguments == {
        "path": str(outside / "approved.txt"),
        "content_length": 15,
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL 后端只在 Windows 验证")
@pytest.mark.asyncio
async def test_shell_retries_exact_command_after_allowed_once(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    outside = tmp_path / "outside"
    runtime_root = tmp_path / "runtime-temp"
    outside.mkdir()
    target = outside / "approved.txt"
    store = SessionStore(data_root / "sessions.db")
    store.create_session("web:readonly")
    resolver = SandboxPolicyResolver(
        store,
        data_root=data_root,
        runtime_temp_root=runtime_root,
    )
    runtime = SandboxProcessRuntime(resolver)
    approvals = ApprovalCoordinator(store)
    published: list[ApprovalRequest] = []

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)
        await approvals.decide(request.id, request.session_id, "allowed-once")

    approvals.set_publisher(publish)
    await approvals.set_session_available("web:readonly", True)
    tool = ShellTool(
        sandbox_guard=SandboxGuard(resolver, approvals),
        sandbox_runtime=runtime,
    )
    command = f'echo approved>"{target}"'
    try:
        raw_result = await tool.execute(
            command=command,
            description="写入外部文件",
            session_key="web:readonly",
            turn_id="turn-1",
            call_id="call-1",
        )
    finally:
        await approvals.close()
        await runtime.close()
        store.close()

    result = json.loads(raw_result)
    assert result["exit_code"] == 0
    assert target.read_text(encoding="utf-8").strip() == "approved"
    assert len(published) == 1
    assert published[0].arguments["command"] == command
