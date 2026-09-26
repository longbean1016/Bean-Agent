"""在实际执行权限内收集少量环境事实；可由隔离 Python 直接运行。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path


def shell_executable() -> str:
    """与 Python 系统 Shell 的默认选择保持一致，不隐式切换命令语法。"""
    if os.name == "nt":
        return os.environ.get("COMSPEC") or str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe")
    return "/bin/sh"


def _version(path: Path, kind: str) -> str | None:
    # 应用执行别名可能拉起商店；不能把“找到文件”误报为可用解释器。
    if os.name == "nt" and (path.suffix.lower() != ".exe" or path.parent.name.lower() == "windowsapps"):
        return None
    args = ["-I", "-S", "-c", "import sys; print('python '+'.'.join(map(str,sys.version_info[:3])))"] if kind == "python" else ["-q", "--version"]
    try:
        # 探测不能依赖写临时文件的权限；有限回读与超时共同限制异常程序。
        process = subprocess.Popen(
            [str(path), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
        chunks: list[bytes] = []
        def read() -> None:
            try:
                if process.stdout is not None:
                    chunks.append(process.stdout.read(4097))
            except OSError:
                pass

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            reader.join(0.8)
            if reader.is_alive() or not chunks or len(chunks[0]) > 4096:
                return None
            process.wait(timeout=0.1)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=0.2)
            reader.join(0.2)
            if not reader.is_alive() and process.stdout is not None:
                process.stdout.close()
        raw = chunks[0]
        if process.returncode != 0:
            return None
        first = raw.decode("utf-8", errors="replace").splitlines()[0]
        return first[:100] if first.startswith(kind + " ") else None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


def discover() -> dict[str, object]:
    cwd = Path.cwd()
    executable = Path("Scripts/python.exe" if os.name == "nt" else "bin/python")
    candidates: list[tuple[str, Path]] = []
    for directory in (".venv", "venv"):
        path = cwd / directory / executable
        if path.is_file():
            candidates.append(("项目虚拟环境", path))
    active = os.environ.get("VIRTUAL_ENV")
    if active:
        candidates.append(("继承的虚拟环境（需核对项目）", Path(active) / executable))
    for name in ("python", "python3"):
        inherited = shutil.which(name)
        if inherited:
            candidates.append(("PATH", Path(inherited)))
    python = None
    seen: set[str] = set()
    for source, path in candidates[:5]:
        path = path.absolute()
        identity = os.path.normcase(str(path))
        if identity in seen:
            continue
        seen.add(identity)
        version = _version(path, "python")
        if version:
            python = {"path": str(path), "source": source, "version": version}
            break
    fallback = None
    if python is None:
        version = _version(Path(sys.executable), "python")
        if version:
            fallback = {"path": sys.executable, "source": "应用备用：仅标准库任务，不代表项目依赖可用", "version": version}
    curl_path = shutil.which("curl.exe" if os.name == "nt" else "curl")
    if curl_path:
        curl_path = str(Path(curl_path).absolute())
    curl_version = _version(Path(curl_path), "curl") if curl_path else None
    return {
        "os": "Windows" if os.name == "nt" else sys.platform,
        "shell": shell_executable(), "cwd": str(cwd),
        "python": python or fallback,
        "curl": {"path": curl_path, "version": curl_version} if curl_version else None,
    }


if __name__ == "__main__":
    print(json.dumps(discover(), ensure_ascii=True))
