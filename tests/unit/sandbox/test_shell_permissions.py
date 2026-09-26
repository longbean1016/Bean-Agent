"""Shell 执行前授权范围必须可解释、隔离且不能被字符串前缀绕过。"""

from pathlib import Path

import pytest

from sandbox.approval import SessionGrant
from sandbox.shell_permissions import classify_shell_permission, requires_preapproval


def test_delete_scope_isolated_by_operation_and_parent_directory(tmp_path: Path) -> None:
    first = classify_shell_permission(
        f'del "{tmp_path / "a.txt"}"',
        tmp_path,
        windows=True,
    )
    second = classify_shell_permission(
        f'del "{tmp_path / "b.txt"}"',
        tmp_path,
        windows=True,
    )
    renamed = classify_shell_permission(
        f'ren "{tmp_path / "a.txt"}" "c.txt"',
        tmp_path,
        windows=True,
    )

    assert first.operation_code == "delete"
    assert first.grant_key == second.grant_key
    assert first.grant_key != renamed.grant_key
    assert first.scope_kind == "paths"


def test_workspace_write_only_preapproves_targets_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    inside_scope = classify_shell_permission(
        f'del "{workspace / "inside.txt"}"',
        workspace,
        windows=True,
    )
    outside_scope = classify_shell_permission(
        f'del "{outside / "outside.txt"}"',
        workspace,
        windows=True,
    )

    assert not requires_preapproval(inside_scope, mode="workspace-write", workspace=workspace)
    assert requires_preapproval(outside_scope, mode="workspace-write", workspace=workspace)
    assert requires_preapproval(inside_scope, mode="read-only", workspace=workspace)
    assert not requires_preapproval(outside_scope, mode="danger-full-access", workspace=workspace)


def test_create_scope_reuses_parent_without_inheriting_delete(tmp_path: Path) -> None:
    first = classify_shell_permission(
        f'mkdir "{tmp_path / "one"}" 2>&1 & echo === & dir /b "{tmp_path}" 2>&1',
        tmp_path,
        windows=True,
    )
    second = classify_shell_permission(
        f'md "{tmp_path / "two"}"',
        tmp_path,
        windows=True,
    )
    deleting = classify_shell_permission(
        f'rmdir "{tmp_path / "two"}"',
        tmp_path,
        windows=True,
    )

    assert first.operation_code == second.operation_code == "write"
    assert first.category == "文件变更"
    assert first.action == "创建/编辑"
    assert first.scope_kind == second.scope_kind == "paths"
    approved = SessionGrant(
        key=first.grant_key,
        tool_name="shell",
        operation=first.operation_code,
        scope_kind=first.scope_kind,
        roots=tuple(str(path) for path in first.grant_roots),
        targets=tuple(str(path) for path in first.target_paths),
    )
    create_candidate = SessionGrant(
        key=second.grant_key,
        tool_name="shell",
        operation=second.operation_code,
        scope_kind=second.scope_kind,
        roots=tuple(str(path) for path in second.grant_roots),
        targets=tuple(str(path) for path in second.target_paths),
    )
    delete_candidate = SessionGrant(
        key=deleting.grant_key,
        tool_name="shell",
        operation=deleting.operation_code,
        scope_kind=deleting.scope_kind,
        roots=tuple(str(path) for path in deleting.grant_roots),
        targets=tuple(str(path) for path in deleting.target_paths),
    )

    assert approved.allows(create_candidate)
    assert not approved.allows(delete_candidate)


def test_literal_powershell_directory_creation_gets_path_scope(tmp_path: Path) -> None:
    target = tmp_path / "created"
    scope = classify_shell_permission(
        f'powershell -NoProfile -Command "New-Item -ItemType Directory -LiteralPath \'{target}\'"',
        tmp_path,
        windows=True,
    )

    assert scope.operation_code == "write"
    assert scope.target_paths == (target.resolve(),)
    assert scope.grant_roots == (tmp_path.resolve(),)
    assert scope.scope_kind == "paths"


def test_prefix_rule_matches_argument_boundaries_and_rejects_runners(tmp_path: Path) -> None:
    scope = classify_shell_permission(
        "git pull origin main",
        tmp_path,
        prefix_rule=["git", "pull"],
        windows=True,
    )
    assert scope.scope_kind == "prefix"
    assert scope.display_scope == "命令前缀：git pull"

    with pytest.raises(ValueError, match="参数边界"):
        classify_shell_permission(
            "git pull origin main",
            tmp_path,
            prefix_rule=["git", "push"],
            windows=True,
        )
    with pytest.raises(ValueError, match="解释器"):
        classify_shell_permission(
            'powershell -Command "Get-ChildItem"',
            tmp_path,
            prefix_rule=["powershell"],
            windows=True,
        )
    with pytest.raises(ValueError, match="不能申请"):
        classify_shell_permission(
            "del file.txt",
            tmp_path,
            prefix_rule=["del"],
            windows=True,
        )


def test_dynamic_delete_falls_back_to_exact_mutating_scope(tmp_path: Path) -> None:
    scope = classify_shell_permission("del %TARGET%", tmp_path, windows=True)
    assert scope.mutating is True
    assert scope.scope_kind == "exact"
    assert requires_preapproval(scope, mode="workspace-write", workspace=tmp_path)


def test_directory_delete_scope_uses_parent_without_authorizing_parent_deletion(
    tmp_path: Path,
) -> None:
    target = tmp_path / "delete-me"
    sibling = tmp_path / "delete-me-too"
    target.mkdir()
    sibling.mkdir()

    scope = classify_shell_permission(
        f'rd /s /q "{target}"',
        tmp_path,
        windows=True,
    )

    assert scope.scope_kind == "paths"
    assert scope.display_scope == f"删除文件 · 目标目录：{tmp_path}"

    sibling_scope = classify_shell_permission(
        f'rd /s /q "{sibling}"',
        tmp_path,
        windows=True,
    )
    approved = SessionGrant(
        key=scope.grant_key,
        tool_name="shell",
        operation=scope.operation_code,
        scope_kind=scope.scope_kind,
        roots=tuple(str(path) for path in scope.grant_roots),
        targets=tuple(str(path) for path in scope.target_paths),
    )
    candidate = SessionGrant(
        key=sibling_scope.grant_key,
        tool_name="shell",
        operation=sibling_scope.operation_code,
        scope_kind=sibling_scope.scope_kind,
        roots=tuple(str(path) for path in sibling_scope.grant_roots),
        targets=tuple(str(path) for path in sibling_scope.target_paths),
    )

    assert approved.allows(candidate)


def test_move_scope_uses_existing_destination_directory(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "destination"
    source_dir.mkdir()
    destination_dir.mkdir()
    source = source_dir / "a.txt"
    source.write_text("a", encoding="utf-8")

    first = classify_shell_permission(
        f'move "{source}" "{destination_dir}"',
        tmp_path,
        windows=True,
    )
    second = classify_shell_permission(
        f'move "{source_dir / "b.txt"}" "{destination_dir}"',
        tmp_path,
        windows=True,
    )

    assert str(destination_dir) in first.display_scope
    assert first.grant_key == second.grant_key
    approved = SessionGrant(
        key=first.grant_key,
        tool_name="shell",
        operation=first.operation_code,
        scope_kind=first.scope_kind,
        roots=tuple(str(path) for path in first.grant_roots),
        targets=tuple(str(path) for path in first.target_paths),
    )
    candidate = SessionGrant(
        key=second.grant_key,
        tool_name="shell",
        operation=second.operation_code,
        scope_kind=second.scope_kind,
        roots=tuple(str(path) for path in second.grant_roots),
        targets=tuple(str(path) for path in second.target_paths),
    )
    assert approved.allows(candidate)


def test_compound_delete_keeps_path_scope_when_remaining_segments_are_read_only(
    tmp_path: Path,
) -> None:
    target = tmp_path / "delete-me.txt"
    child_directory = tmp_path / "12214214"
    target.write_text("temporary", encoding="utf-8")
    child_directory.mkdir()

    first = classify_shell_permission(
        f'del "{target}" 2>&1 & echo === & dir /b "{tmp_path}" 2>&1',
        tmp_path,
        windows=True,
    )
    second = classify_shell_permission(
        f'rmdir "{child_directory}" 2>&1 & echo === & dir /b "{tmp_path}" 2>&1',
        tmp_path,
        windows=True,
    )

    assert first.scope_kind == second.scope_kind == "paths"
    assert first.operation_code == second.operation_code == "delete"
    assert first.grant_roots == (tmp_path.resolve(),)
    assert second.target_paths == (child_directory.resolve(),)

    approved = SessionGrant(
        key=first.grant_key,
        tool_name="shell",
        operation=first.operation_code,
        scope_kind=first.scope_kind,
        roots=tuple(str(path) for path in first.grant_roots),
        targets=tuple(str(path) for path in first.target_paths),
    )
    candidate = SessionGrant(
        key=second.grant_key,
        tool_name="shell",
        operation=second.operation_code,
        scope_kind=second.scope_kind,
        roots=tuple(str(path) for path in second.grant_roots),
        targets=tuple(str(path) for path in second.target_paths),
    )
    assert approved.allows(candidate)


def test_path_tree_grant_does_not_cover_root_or_another_operation(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    approved = SessionGrant(
        key="approved",
        tool_name="shell",
        operation="delete",
        scope_kind="paths",
        roots=(str(root),),
        targets=(str(root / "a.txt"),),
    )
    delete_root = SessionGrant(
        # 路径范围键按根目录生成，删除根本身可能与删除根下文件得到同一个键；
        # 匹配时仍必须检查本次真实目标，不能只看键相等。
        key="approved",
        tool_name="shell",
        operation="delete",
        scope_kind="paths",
        roots=(str(root),),
        targets=(str(root),),
    )
    rename_child = SessionGrant(
        key="rename-child",
        tool_name="shell",
        operation="rename",
        scope_kind="paths",
        roots=(str(root),),
        targets=(str(root / "b.txt"),),
    )

    assert not approved.allows(delete_root)
    assert not approved.allows(rename_child)


def test_compound_delete_with_unknown_segment_falls_back_to_exact_scope(
    tmp_path: Path,
) -> None:
    scope = classify_shell_permission(
        f'del "{tmp_path / "a.txt"}" & custom-check "{tmp_path}"',
        tmp_path,
        windows=True,
    )

    assert scope.mutating is True
    assert scope.scope_kind == "exact"
