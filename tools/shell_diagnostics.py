"""命令身份、保守的失败诊断及重试边界。"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PureWindowsPath


def command_name(token: str) -> str:
    name = PureWindowsPath(token.strip('"\'')).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def simple_command(command: str, *, windows: bool) -> list[str]:
    # 引号内的 URL 查询符不是管道；转义与变量展开仍保守拒绝推断。
    quote = ""
    for char in command:
        if char in "`\n\r^%":
            return []
        if char in ('"' if windows else "\"'"):
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
        elif not quote and char in "|&;<>":
            return []
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        return []


def allows_permission_retry(command: str) -> bool:
    # 保留单条命令的既有审批；不重放可能已部分完成的串联或删除/移动操作。
    if any(char in command for char in "|&;\n\r^%"):
        return False
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        return False
    if not tokens:
        return False
    # 仅保留已知单步内置操作；解释器、脚本和未知命令无法证明没有部分副作用。
    return tokens[0].lower() in {"type", "dir", "where", "echo", "pwd", "ls", "cat"}


@dataclass(frozen=True, slots=True)
class ShellDiagnostic:
    code: str
    message: str


def diagnose(command: str, exit_code: int, interrupted: bool, stdout: str, stderr: str, *, windows: bool) -> ShellDiagnostic | None:
    if interrupted:
        return ShellDiagnostic("shell_timeout", "命令超时，已终止；可能已产生部分效果，请检查后再决定是否重试。")
    tokens = simple_command(command, windows=windows)
    name = command_name(tokens[0]) if tokens else ""
    output = stdout + "\n" + stderr
    if exit_code == 0:
        if windows and tokens and tokens[0].lower() in {"del", "erase"} and re.search(r"(?im)^\s*(?:Access is denied\.|拒绝访问[。.]?)\s*$", output):
            return ShellDiagnostic("shell_partial_failure", "删除命令报告拒绝访问，不能视为成功；保留原始退出码，未自动重试，请先检查各目标状态。")
        return None
    if name == "curl" and exit_code in {5, 6, 7, 28, 35, 60}:
        reason = {
            5: "代理地址解析失败", 6: "目标地址解析失败", 7: "连接失败", 28: "请求超时",
            35: "TLS 握手失败，尚不能确定代理、证书或服务端根因", 60: "证书校验失败",
        }[exit_code]
        return ShellDiagnostic("shell_network_failure", f"curl {reason}。使用 -sS 保留错误信息；不要关闭证书验证或自动提权重试。")
    if re.search(r"(?i)access (?:is )?denied|permission denied|拒绝访问", output):
        return ShellDiagnostic("shell_access_denied", "命令报告权限不足；请检查目标及当前权限，不能据此认定必须提升权限。")
    if re.search(r"(?i)(?:WindowsApps[\\/]+python(?:3)?\.exe|not recognized as an internal|不是内部或外部命令|command not found)", output):
        return ShellDiagnostic("shell_executable_unavailable", "命令或解释器不可用；请使用本轮环境中已验证的绝对路径，不要重复调用不可用别名。")
    return ShellDiagnostic("shell_exit_nonzero", "命令以非零退出码结束，请结合原始输出判断；组合命令的退出码不代表每一步都失败。")
