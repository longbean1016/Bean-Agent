"""把完整 Shell 命令转交给已受限的轻量 worker。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

from sandbox.policy import SandboxMode, SandboxPolicy
from sandbox.runtime import SandboxProcessRuntime, SandboxRunResult


class SandboxShellBroker:
    """用请求文件绕开 cmd.exe 与通用 Windows argv 转义的不兼容。"""

    def __init__(self, runtime: SandboxProcessRuntime) -> None:
        self._runtime = runtime
        self._worker = Path(__file__).with_name("shell_worker.py").resolve()

    async def execute(
        self,
        policy: SandboxPolicy,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        execution_mode: SandboxMode | None = None,
    ) -> SandboxRunResult:
        request_path = policy.temp_dir / f"shell-request-{uuid4().hex}.txt"
        await asyncio.to_thread(request_path.write_text, command, encoding="utf-8")
        try:
            return await self._runtime.run(
                policy,
                [sys.executable, str(self._worker), str(request_path)],
                cwd=cwd,
                timeout=timeout,
                execution_mode=execution_mode,
            )
        finally:
            request_path.unlink(missing_ok=True)


__all__ = ["SandboxShellBroker"]
