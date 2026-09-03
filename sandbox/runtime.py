"""平台无关的会话子进程运行入口。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sandbox.errors import SandboxUnavailable
from sandbox.policy import SandboxMode, SandboxPolicy, SandboxPolicyResolver
from sandbox.windows import (
    WindowsAclProvider,
    WindowsProcessLauncher,
    temp_write_sid,
    workspace_write_sid,
)


@dataclass(frozen=True, slots=True)
class SandboxRunResult:
    stdout: bytes
    stderr: bytes
    exit_code: int
    interrupted: bool


class SandboxProcessRuntime:
    """Windows-first Runtime；受限模式在非 Windows 上始终 fail closed。"""

    def __init__(
        self,
        resolver: SandboxPolicyResolver,
        *,
        acl: WindowsAclProvider | None = None,
        launcher: WindowsProcessLauncher | None = None,
    ) -> None:
        self._resolver = resolver
        self._acl = acl or WindowsAclProvider()
        self._launcher = launcher or WindowsProcessLauncher()
        self._prepared_temps: dict[str, tuple[Path, str]] = {}
        self._prepare_locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    async def run(
        self,
        policy: SandboxPolicy,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 60.0,
        execution_mode: SandboxMode | None = None,
    ) -> SandboxRunResult:
        if self._closed:
            raise SandboxUnavailable("沙箱 Runtime 已关闭")
        mode = execution_mode or policy.mode
        actual_cwd = (cwd or policy.cwd).expanduser().resolve(strict=True)
        if not actual_cwd.is_dir():
            raise SandboxUnavailable(f"子进程 cwd 不是目录：{actual_cwd}")
        workspace_sid: str | None = None
        private_temp_sid: str | None = None
        if mode != "danger-full-access":
            if os.name != "nt":
                raise SandboxUnavailable("当前平台不支持 Windows ACL 沙箱")
            if mode == "workspace-write":
                if policy.workspace_path is None:
                    raise SandboxUnavailable("工作区可写模式缺少工作目录")
                workspace_sid, private_temp_sid = await self._prepare_workspace(policy)

        process_env = os.environ.copy()
        process_env.update(env or {})
        process_env["TMP"] = str(policy.temp_dir)
        process_env["TEMP"] = str(policy.temp_dir)
        result = await self._launcher.run(
            list(argv),
            cwd=str(actual_cwd),
            env=process_env,
            timeout=timeout,
            mode=mode,
            workspace_sid=workspace_sid,
            temp_sid=private_temp_sid,
        )
        return SandboxRunResult(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            interrupted=result.interrupted,
        )

    async def close_session(self, session_key: str) -> None:
        prepared = self._prepared_temps.pop(session_key, None)
        if prepared is not None:
            path, _ = prepared
            await asyncio.to_thread(self._acl.revoke_temp, path)
        self._prepare_locks.pop(session_key, None)
        await asyncio.to_thread(self._resolver.close_session, session_key)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[BaseException] = []
        for session_key in list(self._prepared_temps):
            try:
                await self.close_session(session_key)
            except BaseException as error:
                failures.append(error)
        try:
            await asyncio.to_thread(self._acl.close)
        except BaseException as error:
            failures.append(error)
        try:
            await asyncio.to_thread(self._resolver.close)
        except BaseException as error:
            failures.append(error)
        if failures:
            raise SandboxUnavailable(f"沙箱清理失败：{failures[0]}") from failures[0]

    async def _prepare_workspace(self, policy: SandboxPolicy) -> tuple[str, str]:
        lock = self._prepare_locks.setdefault(policy.session_key, asyncio.Lock())
        async with lock:
            workspace_sid = workspace_write_sid(policy.workspace_path)
            await asyncio.to_thread(
                self._acl.grant_workspace,
                policy.workspace_path,
                workspace_sid,
            )
            prepared = self._prepared_temps.get(policy.session_key)
            if prepared is not None and prepared[0] == policy.temp_dir:
                return workspace_sid, prepared[1]
            private_temp_sid = temp_write_sid(policy.temp_dir)
            # DACL 修改可能递归传播，放在线程池中避免阻塞 WebSocket 事件循环。
            try:
                await asyncio.to_thread(
                    self._acl.grant_temp,
                    policy.temp_dir,
                    private_temp_sid,
                )
            except Exception:
                # workspace ACE 是跨会话复用缓存，失败时保持；temp ACE 必须可撤销。
                await asyncio.to_thread(self._acl.revoke_temp, policy.temp_dir)
                raise
            self._prepared_temps[policy.session_key] = (
                policy.temp_dir,
                private_temp_sid,
            )
            return workspace_sid, private_temp_sid


def create_runtime_temp_root() -> Path:
    """每次进程使用全新的系统 temp 根，旧残留不会获得新会话能力。"""

    root = (
        Path(tempfile.gettempdir())
        / "beanagent"
        / f"runtime-{uuid4().hex}"
    ).resolve()
    root.mkdir(parents=True, exist_ok=False)
    return root


__all__ = ["SandboxProcessRuntime", "SandboxRunResult", "create_runtime_temp_root"]
