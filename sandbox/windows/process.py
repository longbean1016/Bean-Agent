"""Restricted Token、匿名管道与 Job Object 子进程启动。"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from typing import Literal

from sandbox.errors import SandboxUnavailable

_LUA_TOKEN = 0x4
_WRITE_RESTRICTED = 0x8
_SE_GROUP_LOGON_ID = 0xC0000000
_ERROR_BROKEN_PIPE = 109
_FILE_ALL_ACCESS = 0x001F01FF


@dataclass(frozen=True, slots=True)
class ProcessResult:
    stdout: bytes
    stderr: bytes
    exit_code: int
    interrupted: bool


class WindowsProcessLauncher:
    """先挂入 kill-on-close Job，再运行受限或普通 Windows 子进程。"""

    async def run(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        timeout: float,
        mode: Literal["read-only", "workspace-write", "danger-full-access"],
        workspace_sid: str | None = None,
        temp_sid: str | None = None,
    ) -> ProcessResult:
        if os.name != "nt":
            raise SandboxUnavailable("Windows 进程沙箱在当前平台不可用")
        if not argv:
            raise SandboxUnavailable("沙箱子进程命令不能为空")
        token = None
        try:
            if mode != "danger-full-access":
                token = _create_restricted_token(mode, workspace_sid, temp_sid)
            process = _spawn_suspended(argv, cwd=cwd, env=env, token=token)
        except Exception as error:
            if isinstance(error, SandboxUnavailable):
                raise
            raise SandboxUnavailable(f"Windows 沙箱启动失败：{error}") from error
        finally:
            if token is not None:
                token.Close()

        try:
            return await process.communicate(timeout)
        except asyncio.CancelledError:
            process.terminate()
            await process.wait()
            raise
        finally:
            process.close()


class _WindowsChild:
    def __init__(
        self,
        process_handle: object,
        job_handle: object,
        stdout_read: object,
        stderr_read: object,
    ) -> None:
        self._process = process_handle
        self._job = job_handle
        self._stdout = stdout_read
        self._stderr = stderr_read
        self._closed = False

    async def communicate(self, timeout: float) -> ProcessResult:
        stdout_task = asyncio.create_task(asyncio.to_thread(_drain_pipe, self._stdout))
        stderr_task = asyncio.create_task(asyncio.to_thread(_drain_pipe, self._stderr))
        interrupted = False
        try:
            completed = await asyncio.wait_for(self.wait(), timeout=max(0.1, timeout))
            if not completed:
                interrupted = True
                self.terminate()
                await self.wait()
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        except BaseException:
            self.terminate()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        return ProcessResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=-1 if interrupted else _exit_code(self._process),
            interrupted=interrupted,
        )

    async def wait(self) -> bool:
        import win32event

        status = await asyncio.to_thread(
            win32event.WaitForSingleObject,
            self._process,
            win32event.INFINITE,
        )
        return status == win32event.WAIT_OBJECT_0

    def terminate(self) -> None:
        import win32job

        try:
            win32job.TerminateJobObject(self._job, 1)
        except Exception:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for handle in (self._stdout, self._stderr, self._process, self._job):
            try:
                handle.Close()
            except Exception:
                pass


def _create_restricted_token(
    mode: str,
    workspace_sid: str | None,
    temp_sid: str | None,
) -> object:
    try:
        import win32api
        import win32con
        import win32security
    except ImportError as error:
        raise SandboxUnavailable(
            "缺少 Windows 沙箱依赖 pywin32，已拒绝不受限执行"
        ) from error

    if mode == "workspace-write" and not workspace_sid:
        raise SandboxUnavailable("工作区可写 Token 缺少 workspace SID")
    current = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32con.TOKEN_QUERY
        | win32con.TOKEN_DUPLICATE
        | win32con.TOKEN_ADJUST_DEFAULT
        | win32con.TOKEN_ASSIGN_PRIMARY,
    )
    try:
        groups = win32security.GetTokenInformation(current, win32security.TokenGroups)
        logon_sid = next(
            (sid for sid, attributes in groups if int(attributes) & _SE_GROUP_LOGON_ID == _SE_GROUP_LOGON_ID),
            None,
        )
        if logon_sid is None:
            raise SandboxUnavailable("当前 Windows Token 没有 logon SID")
        world_sid = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
        restricting = [(logon_sid, 0), (world_sid, 0)]
        default_grant_sid = world_sid
        if mode == "workspace-write":
            workspace = win32security.ConvertStringSidToSid(workspace_sid)
            restricting.append((workspace, 0))
            default_grant_sid = workspace
            if temp_sid:
                private_temp = win32security.ConvertStringSidToSid(temp_sid)
                restricting.append((private_temp, 0))
                default_grant_sid = private_temp
        restricted = win32security.CreateRestrictedToken(
            current,
            win32security.DISABLE_MAX_PRIVILEGE | _LUA_TOKEN | _WRITE_RESTRICTED,
            [],
            [],
            restricting,
        )
    finally:
        current.Close()

    try:
        dacl = win32security.GetTokenInformation(
            restricted, win32security.TokenDefaultDacl
        )
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS,
            0,
            _FILE_ALL_ACCESS,
            default_grant_sid,
        )
        win32security.SetTokenInformation(
            restricted, win32security.TokenDefaultDacl, dacl
        )
    except Exception:
        restricted.Close()
        raise
    return restricted


def _spawn_suspended(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    token: object | None,
) -> _WindowsChild:
    import win32api
    import win32con
    import win32job
    import win32pipe
    import win32process

    stdin_read, stdin_write = win32pipe.CreatePipe(None, 0)
    stdout_read, stdout_write = win32pipe.CreatePipe(None, 0)
    stderr_read, stderr_write = win32pipe.CreatePipe(None, 0)
    child_handles = (stdin_read, stdout_write, stderr_write)
    host_handles = (stdin_write, stdout_read, stderr_read)
    for handle in child_handles:
        win32api.SetHandleInformation(
            handle, win32con.HANDLE_FLAG_INHERIT, win32con.HANDLE_FLAG_INHERIT
        )
    for handle in host_handles:
        win32api.SetHandleInformation(handle, win32con.HANDLE_FLAG_INHERIT, 0)

    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation
    )
    info["BasicLimitInformation"]["LimitFlags"] |= (
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    win32job.SetInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation, info
    )

    startup = win32process.STARTUPINFO()
    startup.dwFlags |= win32process.STARTF_USESTDHANDLES
    startup.hStdInput = stdin_read
    startup.hStdOutput = stdout_write
    startup.hStdError = stderr_write
    command_line = subprocess.list2cmdline(argv)
    # 子进程必须先进入 kill-on-close Job，再允许执行任何目标代码。
    flags = win32process.CREATE_UNICODE_ENVIRONMENT | win32process.CREATE_SUSPENDED
    process_handle = None
    thread_handle = None
    try:
        if token is None:
            process_handle, thread_handle, _, _ = win32process.CreateProcess(
                None,
                command_line,
                None,
                None,
                True,
                flags,
                env,
                cwd,
                startup,
            )
        else:
            process_handle, thread_handle, _, _ = win32process.CreateProcessAsUser(
                token,
                None,
                command_line,
                None,
                None,
                True,
                flags,
                env,
                cwd,
                startup,
            )
        win32job.AssignProcessToJobObject(job, process_handle)
        if win32process.ResumeThread(thread_handle) == -1:
            raise SandboxUnavailable("ResumeThread 失败")
    except Exception:
        if process_handle is not None:
            try:
                win32process.TerminateProcess(process_handle, 1)
            except Exception:
                pass
        for handle in (*child_handles, *host_handles, thread_handle, process_handle, job):
            if handle is not None:
                try:
                    handle.Close()
                except Exception:
                    pass
        raise

    thread_handle.Close()
    stdin_read.Close()
    stdout_write.Close()
    stderr_write.Close()
    stdin_write.Close()
    return _WindowsChild(process_handle, job, stdout_read, stderr_read)


def _drain_pipe(handle: object) -> bytes:
    import pywintypes
    import win32file

    chunks: list[bytes] = []
    while True:
        try:
            _, data = win32file.ReadFile(handle, 64 * 1024)
        except pywintypes.error as error:
            if error.winerror == _ERROR_BROKEN_PIPE:
                break
            raise
        if not data:
            break
        chunks.append(bytes(data))
    return b"".join(chunks)


def _exit_code(process_handle: object) -> int:
    import win32process

    return int(win32process.GetExitCodeProcess(process_handle))


__all__ = ["ProcessResult", "WindowsProcessLauncher"]
