"""Windows ACL 沙箱的策略和真实进程边界测试。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from sandbox.policy import SandboxPolicyResolver
from sandbox.runtime import SandboxProcessRuntime
from sandbox.windows.acl import temp_write_sid, workspace_write_sid
from session.store import SessionStore


def test_capability_sids_are_stable_and_domain_separated(tmp_path: Path) -> None:
    workspace = workspace_write_sid(tmp_path)

    assert workspace == workspace_write_sid(tmp_path / ".")
    assert workspace.startswith("S-1-4-")
    assert temp_write_sid(tmp_path) != workspace
    assert temp_write_sid(tmp_path).endswith("-1")


def test_policy_without_workspace_uses_private_temp(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    runtime_root = tmp_path / "runtime-temp"
    store = SessionStore(data_root / "sessions.db")
    try:
        store.create_session("web:empty")
        resolver = SandboxPolicyResolver(
            store,
            data_root=data_root,
            runtime_temp_root=runtime_root,
        )

        policy = resolver.resolve("web:empty")

        assert policy.mode == "read-only"
        assert policy.workspace_path is None
        assert policy.cwd == policy.temp_dir
        assert policy.cwd.is_relative_to(runtime_root)
        assert not policy.cwd.is_relative_to(data_root)
        resolver.close()
        assert not runtime_root.exists()
    finally:
        store.close()


def test_policy_rejects_workspace_overlapping_data_root(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    runtime_root = tmp_path / "runtime-temp"
    store = SessionStore(data_root / "sessions.db")
    resolver = SandboxPolicyResolver(
        store,
        data_root=data_root,
        runtime_temp_root=runtime_root,
    )
    try:
        with pytest.raises(RuntimeError, match="数据目录重叠"):
            resolver.validate_workspace(data_root)
    finally:
        resolver.close()
        store.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL 后端只在 Windows 验证")
@pytest.mark.asyncio
async def test_windows_runtime_reads_but_read_only_cannot_write_workspace(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    project = tmp_path / "project"
    runtime_root = tmp_path / "runtime-temp"
    project.mkdir()
    store = SessionStore(data_root / "sessions.db")
    workspace = store.create_workspace(str(project))
    store.create_session("web:readonly", workspace_id=workspace["id"])
    resolver = SandboxPolicyResolver(
        store,
        data_root=data_root,
        runtime_temp_root=runtime_root,
    )
    runtime = SandboxProcessRuntime(resolver)
    policy = resolver.resolve("web:readonly")
    target = project / "blocked.txt"
    try:
        readable = await runtime.run(
            policy,
            [sys.executable, "-c", "print('sandbox-ok')"],
        )
        blocked = await runtime.run(
            policy,
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(target)!r}).write_text('bad')",
            ],
        )
    finally:
        await runtime.close()
        store.close()

    assert readable.exit_code == 0
    assert readable.stdout.strip() == b"sandbox-ok"
    assert blocked.exit_code != 0
    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL 后端只在 Windows 验证")
@pytest.mark.asyncio
async def test_windows_runtime_workspace_write_stays_inside_workspace(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    runtime_root = tmp_path / "runtime-temp"
    project.mkdir()
    outside.mkdir()
    existing_file = project / "existing.txt"
    existing_file.write_text("before", encoding="utf-8")
    store = SessionStore(data_root / "sessions.db")
    workspace = store.create_workspace(str(project))
    store.create_session(
        "web:writable",
        workspace_id=workspace["id"],
        sandbox_mode="workspace-write",
    )
    resolver = SandboxPolicyResolver(
        store,
        data_root=data_root,
        runtime_temp_root=runtime_root,
    )
    runtime = SandboxProcessRuntime(resolver)
    policy = resolver.resolve("web:writable")
    inside_file = project / "inside.txt"
    outside_file = outside / "outside.txt"
    try:
        inside = await runtime.run(
            policy,
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(inside_file)!r}).write_text('ok')",
            ],
        )
        edited = await runtime.run(
            policy,
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(existing_file)!r}).write_text('after')",
            ],
        )
        escaped = await runtime.run(
            policy,
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(outside_file)!r}).write_text('bad')",
            ],
        )
    finally:
        await runtime.close()
        store.close()

    assert inside.exit_code == 0
    assert inside_file.read_text(encoding="utf-8") == "ok"
    assert edited.exit_code == 0
    assert existing_file.read_text(encoding="utf-8") == "after"
    assert escaped.exit_code != 0
    assert not outside_file.exists()
