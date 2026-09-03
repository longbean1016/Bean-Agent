"""把主进程文件写操作转交给受限 helper。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from sandbox.errors import SandboxError
from sandbox.policy import SandboxMode, SandboxPolicyResolver
from sandbox.runtime import SandboxProcessRuntime


class FilesystemMutationBroker:
    """序列化一次写请求，并通过统一 Runtime 启动无状态 helper。"""

    def __init__(
        self,
        resolver: SandboxPolicyResolver,
        runtime: SandboxProcessRuntime,
    ) -> None:
        self._resolver = resolver
        self._runtime = runtime
        self._worker = Path(__file__).with_name("filesystem_worker.py").resolve()

    async def execute(
        self,
        *,
        session_key: str,
        operation: str,
        arguments: dict[str, Any],
        execution_mode: SandboxMode | None = None,
    ) -> str:
        policy = self._resolver.resolve(session_key)
        raw_path = str(arguments.get("path") or "")
        target = Path(raw_path).expanduser()
        if not target.is_absolute():
            target = policy.cwd / target
        target = target.resolve()
        payload = {
            **arguments,
            "operation": operation,
            "path": str(target),
            "display_path": raw_path,
        }
        request_path = policy.temp_dir / f"file-request-{uuid4().hex}.json"
        await asyncio.to_thread(
            request_path.write_text,
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            result = await self._runtime.run(
                policy,
                [sys.executable, str(self._worker), str(request_path)],
                cwd=policy.cwd,
                timeout=60.0,
                execution_mode=execution_mode,
            )
        finally:
            request_path.unlink(missing_ok=True)
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        try:
            response = json.loads(stdout.splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as error:
            detail = stderr or stdout or f"exit code {result.exit_code}"
            raise SandboxError(f"文件 helper 返回无效结果：{detail}") from error
        if not response.get("ok"):
            raise SandboxError(str(response.get("error") or stderr or "文件操作失败"))
        return str(response.get("result") or "")


__all__ = ["FilesystemMutationBroker"]
