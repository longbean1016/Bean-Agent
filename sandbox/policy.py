"""按持久化会话状态解析文件副作用策略。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from sandbox.errors import SandboxAccessDenied

SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]
SANDBOX_MODES: tuple[SandboxMode, ...] = (
    "read-only",
    "workspace-write",
    "danger-full-access",
)


class SessionPolicyStore(Protocol):
    def get_session_sandbox(self, session_key: str) -> dict[str, object] | None: ...


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """一次工具调用使用的不可变会话策略快照。"""

    session_key: str
    mode: SandboxMode
    workspace_id: str | None
    workspace_path: Path | None
    cwd: Path
    temp_dir: Path
    backend: str = "windows-acl"
    capability: str = "partial"

    @property
    def workspace_available(self) -> bool:
        return self.workspace_id is not None and self.workspace_path is not None

    def contains_workspace_path(self, path: Path) -> bool:
        if self.workspace_path is None:
            return False
        return _contains(self.workspace_path, path)


class SandboxPolicyResolver:
    """只从服务端会话关系生成策略，不接收客户端临时根目录。"""

    def __init__(
        self,
        store: SessionPolicyStore,
        *,
        data_root: Path,
        runtime_temp_root: Path,
    ) -> None:
        self._store = store
        self._data_root = data_root.expanduser().resolve()
        self._runtime_temp_root = runtime_temp_root.expanduser().resolve()
        self._runtime_temp_root.mkdir(parents=True, exist_ok=True)

    def resolve(self, session_key: str) -> SandboxPolicy:
        snapshot = self._store.get_session_sandbox(session_key)
        if snapshot is None:
            raise SandboxAccessDenied(f"会话不存在：{session_key}")
        raw_mode = str(snapshot.get("sandbox_mode") or "read-only")
        if raw_mode not in SANDBOX_MODES:
            raise SandboxAccessDenied(f"会话沙箱权限无效：{raw_mode}")

        temp_dir = self._session_temp_dir(session_key)
        workspace_path: Path | None = None
        raw_path = str(snapshot.get("workspace_path") or "").strip()
        workspace_id = str(snapshot.get("workspace_id") or "").strip() or None
        if raw_path and workspace_id:
            candidate = Path(raw_path).expanduser().resolve(strict=True)
            if not candidate.is_dir():
                raise SandboxAccessDenied(f"工作目录已失效：{candidate}")
            self._assert_disjoint(candidate, temp_dir)
            workspace_path = candidate

        mode = cast(SandboxMode, raw_mode)
        if mode == "workspace-write" and workspace_path is None:
            raise SandboxAccessDenied("没有工作目录时不能使用工作区可写权限")
        cwd = workspace_path if workspace_path is not None else temp_dir
        return SandboxPolicy(
            session_key=session_key,
            mode=mode,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            cwd=cwd,
            temp_dir=temp_dir,
        )

    def close_session(self, session_key: str) -> None:
        """只删除本次运行创建的私有临时目录。"""

        import shutil

        target = self._session_temp_dir(session_key, create=False)
        if target.exists():
            shutil.rmtree(target)

    def _session_temp_dir(self, session_key: str, *, create: bool = True) -> Path:
        import hashlib

        digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:24]
        target = (self._runtime_temp_root / digest).resolve()
        target.relative_to(self._runtime_temp_root)
        if create:
            target.mkdir(parents=True, exist_ok=True)
        return target

    def _assert_disjoint(self, workspace: Path, temp_dir: Path) -> None:
        if _contains(workspace, self._data_root) or _contains(self._data_root, workspace):
            raise SandboxAccessDenied(
                f"工作目录不能与 Bean 数据目录重叠：{workspace}"
            )
        if _contains(workspace, temp_dir) or _contains(temp_dir, workspace):
            raise SandboxAccessDenied(
                f"工作目录不能与会话临时目录重叠：{workspace}"
            )


def canonical_directory(path: str | Path) -> Path:
    """按当前平台语义返回真实绝对目录。"""

    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"路径不是目录：{resolved}")
    return resolved


def canonical_path_key(path: str | Path) -> str:
    return os.path.normcase(str(canonical_directory(path)))


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


__all__ = [
    "SANDBOX_MODES",
    "SandboxMode",
    "SandboxPolicy",
    "SandboxPolicyResolver",
    "canonical_directory",
    "canonical_path_key",
]
