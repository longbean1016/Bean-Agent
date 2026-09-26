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
async def test_file_session_grant_reuses_same_tool_and_directory(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    store = SessionStore(data_root / "sessions.db")
    store.create_session("web:readonly")
    resolver = SandboxPolicyResolver(
        store,
        data_root=data_root,
        runtime_temp_root=tmp_path / "runtime-temp",
    )
    runtime = SandboxProcessRuntime(resolver)
    approvals = ApprovalCoordinator(store)
    published: list[ApprovalRequest] = []

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)
        await approvals.decide(request.id, request.session_id, "allowed-session")

    approvals.set_publisher(publish)
    await approvals.set_session_available("web:readonly", True)
    tool = WriteFileTool(
        sandbox_guard=SandboxGuard(resolver, approvals),
        mutation_broker=FilesystemMutationBroker(resolver, runtime),
    )
    try:
        first = await tool.execute(
            str(outside / "first.txt"), "first", session_key="web:readonly",
            turn_id="turn-1", call_id="call-1",
        )
        second = await tool.execute(
            str(outside / "second.txt"), "second", session_key="web:readonly",
            turn_id="turn-2", call_id="call-2",
        )
    finally:
        await approvals.close()
        await runtime.close()
        store.close()

    assert "已写入" in first and "已写入" in second
    assert len(published) == 1
    assert published[0].allow_session is True


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


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL 后端只在 Windows 验证")
@pytest.mark.asyncio
async def test_shell_preapproves_delete_and_reuses_session_scope(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "delete-me"
    second_target = outside / "delete-me-too"
    target.mkdir()
    second_target.mkdir()
    store = SessionStore(data_root / "sessions.db")
    store.create_session("web:readonly")
    resolver = SandboxPolicyResolver(
        store,
        data_root=data_root,
        runtime_temp_root=tmp_path / "runtime-temp",
    )
    runtime = SandboxProcessRuntime(resolver)
    approvals = ApprovalCoordinator(store)
    published: list[ApprovalRequest] = []

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)
        # 目标在真实临时目录中，验证同操作同目录的会话授权只询问一次。
        await approvals.decide(request.id, request.session_id, "allowed-session")

    approvals.set_publisher(publish)
    await approvals.set_session_available("web:readonly", True)
    tool = ShellTool(
        sandbox_guard=SandboxGuard(resolver, approvals),
        sandbox_runtime=runtime,
    )
    try:
        result = json.loads(await tool.execute(
            command=f'rmdir "{target}" 2>&1 & echo === & dir /b "{outside}" 2>&1',
            description="删除第一个临时测试目录",
            session_key="web:readonly",
            turn_id="turn-delete",
            call_id="call-delete",
        ))
        second_result = json.loads(await tool.execute(
            command=f'rmdir "{second_target}" 2>&1 & echo === & dir /b "{outside}" 2>&1',
            description="删除同一目录树下的临时测试目录",
            session_key="web:readonly",
            turn_id="turn-delete-2",
            call_id="call-delete-2",
        ))
    finally:
        await approvals.close()
        await runtime.close()
        store.close()

    assert result["exit_code"] == 0
    assert second_result["exit_code"] == 0
    assert not target.exists() and not second_target.exists()
    assert len(published) == 1
    assert published[0].operation == "删除文件"
    assert published[0].allow_session is True
    assert str(outside) in str(published[0].scope)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL 后端只在 Windows 验证")
@pytest.mark.asyncio
async def test_shell_preapproves_create_and_reuses_parent_scope(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    first_target = outside / "created-one"
    second_target = outside / "created-two"
    store = SessionStore(data_root / "sessions.db")
    store.create_session("web:readonly-create")
    resolver = SandboxPolicyResolver(
        store,
        data_root=data_root,
        runtime_temp_root=tmp_path / "runtime-temp",
    )
    runtime = SandboxProcessRuntime(resolver)
    approvals = ApprovalCoordinator(store)
    published: list[ApprovalRequest] = []

    async def publish(request: ApprovalRequest) -> None:
        published.append(request)
        await approvals.decide(request.id, request.session_id, "allowed-session")

    approvals.set_publisher(publish)
    await approvals.set_session_available("web:readonly-create", True)
    tool = ShellTool(
        sandbox_guard=SandboxGuard(resolver, approvals),
        sandbox_runtime=runtime,
    )
    try:
        first = json.loads(await tool.execute(
            command=f'mkdir "{first_target}" 2>&1 & echo === & dir /b "{outside}" 2>&1',
            description="创建第一个测试目录",
            session_key="web:readonly-create",
            turn_id="turn-create-1",
            call_id="call-create-1",
        ))
        second = json.loads(await tool.execute(
            command=f'mkdir "{second_target}" 2>&1 & echo === & dir /b "{outside}" 2>&1',
            description="创建第二个测试目录",
            session_key="web:readonly-create",
            turn_id="turn-create-2",
            call_id="call-create-2",
        ))
    finally:
        await approvals.close()
        await runtime.close()
        store.close()

    assert first["exit_code"] == second["exit_code"] == 0
    assert first_target.is_dir() and second_target.is_dir()
    assert len(published) == 1
    assert published[0].category == "文件变更"
    assert published[0].action == "创建/编辑"
    assert published[0].scope_kind == "paths"
    assert str(outside) in str(published[0].display_scope)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL 后端只在 Windows 验证")
@pytest.mark.asyncio
async def test_shell_keeps_workspace_write_and_full_access_baselines(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    inside_target = project / "inside.txt"
    outside_target = outside / "outside.txt"
    inside_target.write_text("inside", encoding="utf-8")
    outside_target.write_text("outside", encoding="utf-8")
    store = SessionStore(data_root / "sessions.db")
    workspace = store.create_workspace(str(project))
    store.create_session("web:workspace", workspace_id=workspace["id"], sandbox_mode="workspace-write")
    store.create_session("web:full", sandbox_mode="danger-full-access")
    resolver = SandboxPolicyResolver(
        store,
        data_root=data_root,
        runtime_temp_root=tmp_path / "runtime-temp",
    )
    runtime = SandboxProcessRuntime(resolver)
    approvals = ApprovalCoordinator(store)
    tool = ShellTool(
        sandbox_guard=SandboxGuard(resolver, approvals),
        sandbox_runtime=runtime,
    )
    try:
        inside = json.loads(await tool.execute(
            command=f'del /q "{inside_target}"', description="删除工作区测试文件",
            session_key="web:workspace", turn_id="turn-inside", call_id="call-inside",
        ))
        outside_result = json.loads(await tool.execute(
            command=f'del /q "{outside_target}"', description="删除完全访问测试文件",
            session_key="web:full", turn_id="turn-outside", call_id="call-outside",
        ))
    finally:
        await approvals.close()
        await runtime.close()
        store.close()

    assert inside["exit_code"] == 0
    assert outside_result["exit_code"] == 0
    assert not inside_target.exists()
    assert not outside_target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL 后端只在 Windows 验证")
@pytest.mark.asyncio
async def test_environment_probe_uses_real_read_only_sandbox(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    store = SessionStore(data_root / "sessions.db")
    store.create_session("web:probe")
    resolver = SandboxPolicyResolver(store, data_root=data_root, runtime_temp_root=tmp_path / "runtime")
    runtime = SandboxProcessRuntime(resolver)
    approvals = ApprovalCoordinator(store)
    tool = ShellTool(sandbox_guard=SandboxGuard(resolver, approvals), sandbox_runtime=runtime)
    protected = tmp_path / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    try:
        text = await tool.environment_context("web:probe")
        assert "cmd.exe" in text
        assert "已验证 python" in text
        assert json.dumps(str(resolver.resolve("web:probe").cwd), ensure_ascii=False) in text
        deletion = json.loads(await tool.execute(
            command=f'del /q "{protected}"', description="验证受限删除", session_key="web:probe",
            turn_id="probe-turn", call_id="delete-call",
        ))
        assert deletion["diagnostic_code"] == "shell_sandbox_error"
        assert protected.read_text(encoding="utf-8") == "keep"
        assert "审批界面" in deletion["error"]
    finally:
        await approvals.close()
        await runtime.close()
        store.close()
