"""把 Shell 命令归类为可审计、可复用的执行前授权范围。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


_SHELL_META = ("|", "&", ";", ">", "<", "\n", "\r", "`", "$(", "${", "%")
_UNSAFE_PREFIX_ROOTS = frozenset({
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "python", "python.exe", "python3", "python3.exe", "py", "node", "node.exe",
    "bash", "bash.exe", "sh", "sh.exe", "wsl", "wsl.exe", "perl", "ruby", "php",
    "del", "erase", "rd", "rmdir", "move", "ren", "rename", "rm", "mv",
    "mkdir", "md", "remove-item", "move-item", "rename-item", "new-item",
})
_DELETE_COMMANDS = frozenset({"del", "erase", "rd", "rmdir", "rm"})
_MOVE_COMMANDS = frozenset({"move", "mv"})
_RENAME_COMMANDS = frozenset({"ren", "rename"})
_CREATE_COMMANDS = frozenset({"mkdir", "md"})
_READ_ONLY_COMMANDS = frozenset({"echo", "dir"})
_SAFE_HANDLE_REDIRECTION = re.compile(r"(?<!\S)[012]?>&[012](?!\S)")


@dataclass(frozen=True, slots=True)
class ShellPermissionScope:
    """一次 Shell 调用的授权匹配键与用户可见范围。"""

    operation_code: str
    operation_label: str
    grant_key: str
    display_scope: str
    category: str
    action: str
    target_paths: tuple[Path, ...] = ()
    grant_roots: tuple[Path, ...] = ()
    mutating: bool = False
    scope_kind: str = "exact"


def classify_shell_permission(
    command: str,
    cwd: Path,
    *,
    prefix_rule: Sequence[str] | None = None,
    windows: bool | None = None,
) -> ShellPermissionScope:
    """解析文件变更及安全复合命令；无法证明范围时退回完整命令。"""

    use_windows = os.name == "nt" if windows is None else windows
    operation_code, operation_label, mutating, paths, reusable_paths = _analyze_command(
        command,
        cwd,
        windows=use_windows,
    )

    if mutating and prefix_rule:
        raise ValueError("删除、移动或重命名命令不能申请宽泛前缀授权")

    # 只有每个变更目标都已解析，且复合命令中的其它片段均已证明只读，
    # 才能把一次批准复用为目录范围；否则继续使用完整命令匹配。
    if mutating and paths and reusable_paths:
        roots = _scope_roots(operation_code, paths)
        payload = [operation_code, *(_path_key(path) for path in roots)]
        category, action = _permission_labels(operation_code, "paths")
        return ShellPermissionScope(
            operation_code=operation_code,
            operation_label=operation_label,
            grant_key=_grant_key("paths", payload),
            display_scope=_path_scope_label(operation_label, roots),
            category=category,
            action=action,
            target_paths=paths,
            grant_roots=roots,
            mutating=True,
            scope_kind="paths",
        )

    if prefix_rule:
        tokens = _split(command, windows=use_windows)
        prefix = _validated_prefix(tokens, prefix_rule, command, windows=use_windows)
        return ShellPermissionScope(
            operation_code="execute",
            operation_label="执行 Shell 命令",
            grant_key=_grant_key("prefix", prefix),
            display_scope=f"命令前缀：{' '.join(prefix)}",
            category="命令执行",
            action="命令前缀",
            mutating=False,
            scope_kind="prefix",
        )

    exact = command.strip()
    category, action = _permission_labels(operation_code, "exact")
    return ShellPermissionScope(
        operation_code=operation_code,
        operation_label=operation_label,
        grant_key=_grant_key("exact", [operation_code, exact]),
        display_scope="仅此完整命令",
        category=category,
        action=action,
        target_paths=paths,
        mutating=mutating,
        scope_kind="exact",
    )


def _analyze_command(
    command: str,
    cwd: Path,
    *,
    windows: bool,
) -> tuple[str, str, bool, tuple[Path, ...], bool]:
    """按片段汇总变更类型；复合命令只接受单一变更类型和已知只读检查。"""

    segments = _split_compound(command)
    if segments is None:
        operation = _detect_operation(command)
        if operation is None:
            return "execute", "执行 Shell 命令", False, (), False
        return operation[0], operation[1], True, (), False
    if not segments:
        raise ValueError("命令不能为空")

    operations: list[tuple[str, str]] = []
    targets: list[Path] = []
    reusable = True
    for segment in segments:
        cleaned = _strip_safe_handle_redirections(segment)
        if cleaned is None:
            reusable = False
            operation = _detect_operation(segment)
            if operation is not None:
                operations.append(operation)
            continue
        tokens = _split(cleaned, windows=windows)
        if not tokens:
            continue
        operation_tokens = tokens
        if _command_name(tokens[0]) in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
            inner_tokens = _powershell_command_tokens(tokens, windows=windows)
            if inner_tokens:
                operation_tokens = inner_tokens
        operation = _segment_operation(operation_tokens, segment)
        if operation is None:
            if len(segments) > 1 and _command_name(tokens[0]) not in _READ_ONLY_COMMANDS:
                reusable = False
            continue
        operation_code, operation_label = operation
        operations.append(operation)
        segment_paths = _operation_paths(
            operation_code,
            operation_tokens,
            cwd,
            windows=windows,
        )
        if not segment_paths:
            reusable = False
        targets.extend(segment_paths)

    if not operations:
        # 未识别的输出重定向仍可能写文件，不能在只读或工作区外模式下
        # 因“看起来只是执行命令”而绕过审批。
        if reusable is False and any(marker in command for marker in (">", "<")):
            return "execute", "执行完整 Shell 命令", True, (), False
        return "execute", "执行 Shell 命令", False, (), False

    operation_codes = {operation[0] for operation in operations}
    operation_code, operation_label = operations[0]
    if len(operation_codes) != 1:
        return "execute", "执行完整 Shell 命令", True, tuple(targets), False
    return operation_code, operation_label, True, tuple(targets), reusable


def _segment_operation(tokens: Sequence[str], raw_segment: str) -> tuple[str, str] | None:
    base = _command_name(tokens[0])
    if base in _DELETE_COMMANDS or base == "remove-item":
        return "delete", "删除文件"
    if base in _MOVE_COMMANDS or base == "move-item":
        return "move", "移动文件"
    if base in _RENAME_COMMANDS or base == "rename-item":
        return "rename", "重命名文件"
    if base in _CREATE_COMMANDS or base == "new-item":
        return "write", "创建文件或目录"
    return _detect_operation(raw_segment) if base in {
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
    } else None


def _detect_operation(command: str) -> tuple[str, str] | None:
    lowered = command.casefold()
    if "remove-item" in lowered:
        return "delete", "删除文件"
    if "move-item" in lowered:
        return "move", "移动文件"
    if "rename-item" in lowered:
        return "rename", "重命名文件"
    if "new-item" in lowered:
        return "write", "创建文件或目录"
    direct_commands = (
        (r"(?:^|[|;&]\s*)(?:del|erase|rd|rmdir|rm)(?:\.exe)?(?:\s|$)", ("delete", "删除文件")),
        (r"(?:^|[|;&]\s*)(?:move|mv)(?:\.exe)?(?:\s|$)", ("move", "移动文件")),
        (r"(?:^|[|;&]\s*)(?:ren|rename)(?:\.exe)?(?:\s|$)", ("rename", "重命名文件")),
        (r"(?:^|[|;&]\s*)(?:mkdir|md)(?:\.exe)?(?:\s|$)", ("write", "创建文件或目录")),
    )
    for pattern, operation in direct_commands:
        if re.search(pattern, lowered):
            return operation
    try:
        tokens = _split(command, windows=True)
    except ValueError:
        return None
    if not tokens:
        return None
    base = _command_name(tokens[0])
    if base in _DELETE_COMMANDS:
        return "delete", "删除文件"
    if base in _MOVE_COMMANDS:
        return "move", "移动文件"
    if base in _RENAME_COMMANDS:
        return "rename", "重命名文件"
    if base in _CREATE_COMMANDS or base == "new-item":
        return "write", "创建文件或目录"
    return None


def _operation_paths(
    operation: str,
    tokens: Sequence[str],
    cwd: Path,
    *,
    windows: bool,
) -> tuple[Path, ...]:
    base = _command_name(tokens[0])
    if base in {"remove-item", "move-item", "rename-item"}:
        # PowerShell 参数绑定和表达式语法比直接命令复杂；未实现完整解析前
        # 不从部分 token 猜测授权边界。
        return ()
    if operation == "delete":
        return _delete_paths(tokens[1:], cwd, windows=windows)
    if operation == "move":
        return _move_paths(tokens[1:], cwd, windows=windows)
    if operation == "rename":
        return _rename_paths(tokens[1:], cwd, windows=windows)
    if operation == "write":
        if base == "new-item":
            return _new_item_paths(tokens[1:], cwd)
        if base in _CREATE_COMMANDS:
            return _create_paths(tokens[1:], cwd, windows=windows)
    return ()


def _powershell_command_tokens(tokens: Sequence[str], *, windows: bool) -> tuple[str, ...]:
    """只展开显式 `-Command` 后的单条 PowerShell 命令，复杂脚本继续精确授权。"""

    lowered = [token.casefold() for token in tokens]
    try:
        index = next(index for index, token in enumerate(lowered) if token in {"-command", "-c"})
    except StopIteration:
        return ()
    payload = " ".join(tokens[index + 1:]).strip()
    payload = _strip_quotes(payload)
    if not payload or any(marker in payload for marker in ("|", ";", "&", "`", "$(")):
        return ()
    try:
        return tuple(_split(payload, windows=windows))
    except ValueError:
        return ()


def _split_compound(command: str) -> list[str] | None:
    """保守拆分 CMD 的 `&`/`&&`；引号和标准句柄重定向保持原样。"""

    segments: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        if char in {'"', "'"}:
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
            current.append(char)
            index += 1
            continue
        if not quote and char in {"|", ";", "\n", "\r", "`"}:
            return None
        if not quote and char == "&" and (not current or current[-1] not in {">", "<"}):
            segment = "".join(current).strip()
            if not segment:
                return None
            segments.append(segment)
            current = []
            index += 2 if index + 1 < len(command) and command[index + 1] == "&" else 1
            continue
        current.append(char)
        index += 1
    if quote:
        raise ValueError("命令解析失败，请检查引号是否匹配")
    tail = "".join(current).strip()
    if tail:
        segments.append(tail)
    return segments


def _strip_safe_handle_redirections(segment: str) -> str | None:
    cleaned = _SAFE_HANDLE_REDIRECTION.sub("", segment).strip()
    return None if any(marker in cleaned for marker in (">", "<")) else cleaned


def requires_preapproval(scope: ShellPermissionScope, *, mode: str, workspace: Path | None) -> bool:
    """基础模式允许时不重复询问；无法证明在工作区内时保守审批。"""

    if mode == "danger-full-access" or not scope.mutating:
        return False
    if mode == "read-only":
        return True
    if mode != "workspace-write" or workspace is None or not scope.target_paths:
        return True
    return not all(_contains(workspace, target) for target in scope.target_paths)


def _split(command: str, *, windows: bool) -> list[str]:
    try:
        values = shlex.split(command, posix=not windows)
    except ValueError as error:
        raise ValueError("命令解析失败，请检查引号是否匹配") from error
    return [_strip_quotes(value) for value in values]


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _command_name(value: str) -> str:
    normalized = value.replace("\\", "/").rsplit("/", 1)[-1]
    return normalized.casefold()


def _delete_paths(arguments: Sequence[str], cwd: Path, *, windows: bool) -> tuple[Path, ...]:
    values = [value for value in arguments if not _is_switch(value, windows=windows)]
    return _literal_paths(values, cwd)


def _create_paths(arguments: Sequence[str], cwd: Path, *, windows: bool) -> tuple[Path, ...]:
    values = [value for value in arguments if not _is_switch(value, windows=windows)]
    return _literal_paths(values, cwd)


def _new_item_paths(arguments: Sequence[str], cwd: Path) -> tuple[Path, ...]:
    """保守识别 `New-Item -ItemType Directory -LiteralPath <path>`。"""

    item_type = ""
    path_value = ""
    index = 0
    while index < len(arguments):
        token = arguments[index].casefold()
        if token in {"-itemtype", "-type"} and index + 1 < len(arguments):
            item_type = arguments[index + 1].casefold()
            index += 2
            continue
        if token in {"-literalpath", "-path"} and index + 1 < len(arguments):
            path_value = arguments[index + 1]
            index += 2
            continue
        index += 1
    if item_type not in {"directory", "container"} or not path_value:
        return ()
    path = _literal_path(path_value, cwd)
    return (path,) if path is not None else ()


def _rename_paths(arguments: Sequence[str], cwd: Path, *, windows: bool) -> tuple[Path, ...]:
    values = [value for value in arguments if not _is_switch(value, windows=windows)]
    if len(values) != 2:
        return ()
    source = _literal_path(values[0], cwd)
    if source is None or not _is_literal(values[1]):
        return ()
    destination_value = Path(values[1]).expanduser()
    destination = (
        destination_value
        if destination_value.is_absolute()
        else source.parent / destination_value
    ).resolve(strict=False)
    return source, destination


def _move_paths(arguments: Sequence[str], cwd: Path, *, windows: bool) -> tuple[Path, ...]:
    values = [value for value in arguments if not _is_switch(value, windows=windows)]
    if len(values) < 2:
        return ()
    resolved = _literal_paths(values, cwd)
    return resolved if len(resolved) == len(values) else ()


def _literal_paths(values: Iterable[str], cwd: Path) -> tuple[Path, ...]:
    result: list[Path] = []
    for value in values:
        path = _literal_path(value, cwd)
        if path is None:
            return ()
        result.append(path)
    return tuple(result)


def _literal_path(value: str, cwd: Path) -> Path | None:
    if not _is_literal(value):
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate.resolve(strict=False)


def _is_literal(value: str) -> bool:
    return bool(value) and not any(marker in value for marker in ("*", "?", "%", "$", "`", "|", "&", ";", "<", ">"))


def _is_switch(value: str, *, windows: bool) -> bool:
    if value.startswith("-"):
        return True
    return windows and value.startswith("/") and not Path(value).is_absolute()


def _validated_prefix(
    tokens: Sequence[str],
    requested: Sequence[str],
    command: str,
    *,
    windows: bool,
) -> tuple[str, ...]:
    prefix = tuple(str(value).strip() for value in requested if str(value).strip())
    if not prefix:
        raise ValueError("前缀授权不能为空")
    if _has_shell_meta(command):
        raise ValueError("包含管道、串联或动态展开的命令不能申请前缀授权")
    if _command_name(prefix[0]) in _UNSAFE_PREFIX_ROOTS:
        raise ValueError("解释器、通用执行器和文件变更命令不能申请宽泛前缀授权")
    if len(prefix) > len(tokens):
        raise ValueError("前缀授权必须是完整命令的参数前缀")
    normalize = str.casefold if windows else (lambda value: value)
    if any(normalize(prefix[index]) != normalize(tokens[index]) for index in range(len(prefix))):
        raise ValueError("前缀授权必须按参数边界匹配完整命令")
    return tuple(normalize(value) for value in prefix)


def _scope_roots(operation: str, paths: Sequence[Path]) -> tuple[Path, ...]:
    # 删除对象无论是文件还是目录，都按直接父目录复用。同级目录之间可以
    # 命中同一授权，而 SessionGrant 的严格后代检查仍禁止删除授权根本身。
    # 移动和重命名继续保留对象/容器边界，并同时约束来源与目标。
    roots = {_scope_root(operation, path) for path in paths}
    return tuple(sorted(roots, key=_path_key))


def _scope_root(operation: str, path: Path) -> Path:
    if operation in {"delete", "write"}:
        return path.parent.resolve(strict=False)
    return (path if path.is_dir() else path.parent).resolve(strict=False)


def _path_scope_label(operation_label: str, roots: Sequence[Path]) -> str:
    joined = "、".join(str(root) for root in roots)
    return f"{operation_label} · 目标目录：{joined}"


def _permission_labels(operation: str, scope_kind: str) -> tuple[str, str]:
    actions = {
        "write": "创建/编辑",
        "delete": "删除",
        "move": "移动/重命名",
        "rename": "移动/重命名",
    }
    if operation in actions:
        return "文件变更", actions[operation]
    return "命令执行", "命令前缀" if scope_kind == "prefix" else "完整 Shell 命令"


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve(strict=False))))


def _grant_key(kind: str, values: Sequence[str]) -> str:
    serialized = json.dumps(["shell", kind, *values], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _has_shell_meta(command: str) -> bool:
    return any(marker in command for marker in _SHELL_META)


def _contains(root: Path, target: Path) -> bool:
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


__all__ = ["ShellPermissionScope", "classify_shell_permission", "requires_preapproval"]
