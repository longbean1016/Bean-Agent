"""在 Restricted Token 内调用当前平台的系统 Shell。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("bean-sandbox-shell: 请求参数无效", file=sys.stderr)
        return 127
    try:
        command = Path(sys.argv[1]).read_text(encoding="utf-8")
        # worker 已处于受限 Token 和 Job 中，系统 Shell 及其后代会继承两者。
        return int(subprocess.run(command, shell=True, check=False).returncode)
    except Exception as error:
        print(f"bean-sandbox-shell: {error}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
