"""受控的前台 Shell 命令执行工具。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlparse

from tools.base import Tool

_DEFAULT_TIMEOUT = 60
_MAX_TIMEOUT = 600
_MAX_OUTPUT = 30_000
_IS_WINDOWS = os.name == "nt"

_BANNED = frozenset(
    {
        "curlie", "axel", "aria2c", "nc", "telnet", "lynx", "w3m",
        "links", "http-prompt", "chrome", "firefox", "safari",
    }
)
_NETWORK_CMDS = frozenset({"curl", "wget", "http", "httpie", "xh"})
_NET_WRITE_FLAGS = frozenset(
    {
        "-o", "--output", "-O", "--remote-name", "-T", "--upload-file",
        "-F", "--form", "--form-string", "--output-document", "--post-file",
        "--download", "--offline", "@",
    }
)
_NET_WRITE_FLAGS_LOWER = frozenset(flag.lower() for flag in _NET_WRITE_FLAGS)
_RESTRICTED_META_CHARS = ("|", ";", "&", ">", "<", "`", "$(")
_RESTRICTED_SHELL_RUNNERS = frozenset(
    {"sh", "bash", "zsh", "fish", "python", "python3", "node", "perl", "ruby", "php", "lua"}
)


def _err(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def _split_command(command: str) -> list[str]:
    return [
        _strip_shell_quotes(token)
        for token in shlex.split(command, posix=not _IS_WINDOWS)
    ]


def _strip_shell_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return token[1:-1]
    return token


def _validate_url_target(url: str) -> str | None:
    """只允许公网 HTTP(S)，阻止 Shell 绕过 WebFetch 的 SSRF 边界。"""

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "仅允许 http:// 或 https:// URL"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return "URL 缺少主机名"
    try:
        address = ipaddress.ip_address(host)
        if (
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_reserved
        ):
            return f"禁止访问内网/本地地址：{host}"
    except ValueError:
        if host.endswith((".local", ".localhost")):
            return f"禁止访问本地域名：{host}"
    return None


def _validate_network_command(command: str) -> str | None:
    try:
        tokens = _split_command(command)
    except ValueError:
        return "命令解析失败，请检查引号是否匹配"
    if not tokens or tokens[0].lower() not in _NETWORK_CMDS:
        return None
    for token in tokens[1:]:
        lowered = token.lower()
        # 参数先统一小写；参考实现原集合含 -T/-O/-F，大写直接比较会漏检。
        if lowered in _NET_WRITE_FLAGS_LOWER or any(
            lowered.startswith(flag + "=") for flag in _NET_WRITE_FLAGS_LOWER
        ):
            return f"网络命令参数 '{token}' 不被允许（禁止上传/写文件）"
        if "=@" in token or token.startswith("@"):
            return f"网络命令参数 '{token}' 不被允许（禁止本地文件上传）"
    urls = [token for token in tokens[1:] if token.startswith(("http://", "https://"))]
    if not urls:
        return "网络命令必须显式提供 http:// 或 https:// URL"
    for url in urls:
        error = _validate_url_target(url)
        if error:
            return error
    return None


def _looks_like_path(token: str) -> bool:
    if token in {".", ".."}:
        return True
    return any(marker in token for marker in ("/", "\\")) or token.startswith("~")


def _validate_restricted_token(token: str, restricted_dir: Path) -> str | None:
    token = _strip_shell_quotes(token)
    if token.startswith("~"):
        return f"受限 shell 禁止访问任务目录外路径：{token}"
    if not _looks_like_path(token):
        return None
    parts = PureWindowsPath(token).parts if _IS_WINDOWS else Path(token).parts
    if ".." in parts:
        return f"受限 shell 禁止访问父级路径：{token}"
    path = Path(token)
    if path.is_absolute():
        resolved = path.resolve()
        root = restricted_dir.resolve()
        if resolved != root and root not in resolved.parents:
            return f"受限 shell 禁止访问任务目录外路径：{token}"
    return None


def _validate_command(
    command: str,
    *,
    allow_network: bool,
    restricted_dir: Path | None,
    cwd: Path | None = None,
) -> str | None:
    try:
        tokens = _split_command(command)
    except ValueError:
        return "命令解析失败，请检查引号是否匹配"
    if not tokens:
        return None
    base_command = tokens[0].lower()
    if not allow_network and base_command in _NETWORK_CMDS:
        return "当前 shell 配置禁止网络访问"

    if restricted_dir is not None:
        root = restricted_dir.resolve()
        if cwd is not None:
            resolved_cwd = cwd.resolve()
            if resolved_cwd != root and root not in resolved_cwd.parents:
                return f"受限 shell 禁止使用任务目录外工作目录：{cwd}"
        # 受限模式禁止 Shell 元字符，因为它们会产生无法逐 token 审计的第二条命令。
        if any(marker in command for marker in _RESTRICTED_META_CHARS):
            return "受限 shell 禁止管道、重定向或串联命令"
        if base_command in _RESTRICTED_SHELL_RUNNERS:
            return f"受限 shell 禁止启动解释器或二级 shell：{base_command}"
        for token in tokens[1:]:
            if token.startswith("-") or token == "--":
                continue
            error = _validate_restricted_token(token, root)
            if error:
                return error
    return _validate_network_command(command)


def _subprocess_options(cwd: Path | None, env: dict[str, str]) -> dict[str, Any]:
    options: dict[str, Any] = {
        "cwd": str(cwd) if cwd is not None else None,
        "env": env,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if _IS_WINDOWS:
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return options


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """超时和取消时终止整棵进程树，避免孙进程留在后台。"""

    if _IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        os.killpg(process.pid, signal.SIGKILL)


def _truncate(content: str) -> dict[str, Any]:
    """长输出保留尾部，因为退出结果和错误摘要通常位于末尾。"""

    if len(content) <= _MAX_OUTPUT:
        return {
            "text": content,
            "truncated": False,
            "strategy": "tail",
            "full_length": len(content),
            "returned_length": len(content),
            "omitted_lines": 0,
        }
    omitted = content[: len(content) - _MAX_OUTPUT]
    omitted_lines = omitted.count("\n")
    prefix = f"... [{omitted_lines} 行已省略] ...\n\n"
    tail = content[-max(0, _MAX_OUTPUT - len(prefix)) :]
    text = prefix + tail
    return {
        "text": text,
        "truncated": True,
        "strategy": "tail",
        "full_length": len(content),
        "returned_length": len(text),
        "omitted_lines": omitted_lines,
    }


def _write_full_output(content: str) -> str:
    descriptor, path = tempfile.mkstemp(prefix="beanagent-shell-", suffix=".log")
    os.close(descriptor)
    Path(path).write_text(content, encoding="utf-8")
    return path


class ShellTool(Tool):
    """在受控工作目录中执行前台命令并返回结构化结果。"""

    name = "shell"

    def __init__(
        self,
        *,
        allow_network: bool = True,
        working_dir: Path | None = None,
        restricted_dir: Path | None = None,
    ) -> None:
        self._allow_network = allow_network
        self._working_dir = working_dir
        self._restricted_dir = restricted_dir.resolve() if restricted_dir else None

    @property
    def description(self) -> str:
        return (
            "在 shell 中执行前台命令并返回结构化输出。使用绝对路径，避免依赖 cd。"
            "网络命令仅允许公网 HTTP(S) 且禁止上传和写文件；输出超过 30000 字符自动截断。"
            "不得用 shell 替代 read_file、web_fetch、list_dir 等专用工具。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "description": {
                    "type": "string",
                    "description": "用 5-10 字描述命令作用，便于审查和日志追踪",
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_TIMEOUT,
                    "default": _DEFAULT_TIMEOUT,
                },
                "cwd": {"type": "string", "description": "可选工作目录"},
            },
            "required": ["command", "description"],
        }

    async def execute(self, **kwargs: Any) -> str:
        command = str(kwargs.get("command", "")).strip()
        description = str(kwargs.get("description", ""))
        timeout = min(int(kwargs.get("timeout", _DEFAULT_TIMEOUT)), _MAX_TIMEOUT)
        if not command:
            return _err("命令不能为空")
        cwd = self._working_dir
        if kwargs.get("cwd") not in (None, ""):
            cwd = Path(str(kwargs["cwd"])).expanduser()
        if self._restricted_dir is not None and cwd is None:
            cwd = self._restricted_dir

        try:
            tokens = _split_command(command)
        except ValueError:
            return _err("命令解析失败，请检查引号是否匹配")
        base_command = tokens[0].lower() if tokens else ""
        if base_command in _BANNED:
            return _err(f"命令 '{base_command}' 不被允许（安全限制）")
        validation_error = _validate_command(
            command,
            allow_network=self._allow_network,
            restricted_dir=self._restricted_dir,
            cwd=cwd,
        )
        if validation_error:
            return _err(validation_error)

        start = time.monotonic()
        process = await asyncio.create_subprocess_shell(
            command,
            **_subprocess_options(cwd, os.environ.copy()),
        )
        interrupted = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            interrupted = True
            try:
                _kill_process_tree(process)
            except (ProcessLookupError, PermissionError):
                pass
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            try:
                _kill_process_tree(process)
            except (ProcessLookupError, PermissionError):
                pass
            raise

        output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
        exit_code = -1 if interrupted else (process.returncode or 0)
        if not output:
            output = "（无输出）"
        elif exit_code != 0 and not interrupted:
            output += f"\nExit code {exit_code}"
        output_meta = _truncate(output)
        full_output_path = (
            _write_full_output(output) if output_meta["truncated"] else None
        )
        truncation = None
        if output_meta["truncated"]:
            truncation = {
                "strategy": output_meta["strategy"],
                "full_length": output_meta["full_length"],
                "returned_length": output_meta["returned_length"],
                "omitted_lines": output_meta["omitted_lines"],
            }
        return json.dumps(
            {
                "command": command,
                "exit_code": exit_code,
                "interrupted": interrupted,
                "duration_ms": int((time.monotonic() - start) * 1000),
                "output": output_meta["text"],
                "truncation": truncation,
                "full_output_path": full_output_path,
                "description": description,
            },
            ensure_ascii=False,
        )


__all__ = ["ShellTool"]
