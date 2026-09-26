"""Shell 环境快照的有界缓存与模型可见摘要，不负责权限提升。"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sandbox.errors import SandboxError
from sandbox.runtime import SandboxRunResult


@dataclass(frozen=True, slots=True)
class ShellEnvironmentSnapshot:
    text: str


class ShellEnvironmentCache:
    def __init__(self, *, ttl: float = 60, capacity: int = 64) -> None:
        self._ttl = ttl
        self._capacity = capacity
        self._cache: OrderedDict[tuple[str, ...], tuple[float, ShellEnvironmentSnapshot]] = OrderedDict()

    async def get(self, key: tuple[str, ...], probe: Callable[[], Awaitable[SandboxRunResult]]) -> ShellEnvironmentSnapshot:
        cached = self._cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < self._ttl:
            self._cache.move_to_end(key)
            return cached[1]
        try:
            result = await probe()
            if result.exit_code != 0 or result.interrupted or len(result.stdout) > 8192:
                raise ValueError("环境探测未完成")
            data = json.loads(result.stdout)
            lines = ["## 命令执行环境（以本轮为准）"]
            for field, label in (("os", "系统"), ("shell", "实际 Shell"), ("cwd", "工作目录")):
                value = data[field]
                if not isinstance(value, str):
                    raise ValueError("环境事实格式无效")
                lines.append(f"- {label}: {json.dumps(value[:1024], ensure_ascii=False)}")
            for name in ("python", "curl"):
                tool = data.get(name)
                if isinstance(tool, dict) and isinstance(tool.get("path"), str):
                    detail = {k: str(tool[k])[:1024] for k in ("path", "source", "version") if k in tool}
                    lines.append(f"- 已验证 {name}: {json.dumps(detail, ensure_ascii=False)}")
                else:
                    lines.append(f"- {name}: 未找到已验证的可用程序，不要猜测命令或自动安装。")
            lines.append("- 以上只验证程序启动，不保证项目依赖可用。优先专用工具；解释器使用绝对路径，用户明确指定的环境优先。不要把应用备用环境当成项目依赖环境。")
            snapshot = ShellEnvironmentSnapshot("\n".join(lines))
        except (SandboxError, OSError, ValueError, KeyError, TypeError):
            # 探测失败不影响普通对话，也绝不改用宿主权限再次探测。
            snapshot = ShellEnvironmentSnapshot("## 命令执行环境\n当前权限下环境探测不可用；不要假定 python 可用，优先使用专用工具。")
        self._cache[key] = (time.monotonic(), snapshot)
        self._cache.move_to_end(key)
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        return snapshot
