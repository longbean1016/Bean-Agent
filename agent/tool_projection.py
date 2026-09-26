"""工具调用的安全展示投影。

模型侧仍需要完整参数和结果继续推理，但实时事件、运行快照和语义消息只应
携带可以展示给用户的摘要。这个模块集中定义两者之间的边界，避免不同出口
各自实现一套脱敏规则而发生遗漏。
"""

from __future__ import annotations

import re
from copy import deepcopy
from collections.abc import Mapping
from typing import Any

_MAX_DISPLAY_TEXT = 500
_SECRET_KEY_RE = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|auth(?:orization)?|cookie|credential|private[_-]?key)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    # 环境变量既可能叫 TOKEN，也可能是 OPENAI_API_KEY；旧表达式要求
    # 前缀后再次出现 TOKEN/KEY，漏掉了最常见的 ``TOKEN=value`` 形态。
    r"(?P<key>\b(?:[A-Z][A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL)|"
    r"TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|AUTH(?:ORIZATION)?|COOKIE))"
    # 值允许带一个 Bearer 前缀；否则 ``TOKEN=Bearer abc`` 会只替换
    # ``Bearer``，把真正的 token ``abc`` 留在展示文本里。
    r"(?P<separator>\s*(?:=|:)\s*)(?P<value>(?!Bearer\s+[\"'])(?:Bearer\s+)?[^\s\"';&|]+)",
    re.IGNORECASE,
)
_SECRET_JSON_RE = re.compile(
    r"(?P<prefix>[\"']?(?:token|access[_-]?token|refresh[_-]?token|secret|client[_-]?secret|password|passwd|api[_-]?key|auth[_-]?token|authorization|x[-_]?(?:api[-_]?key|auth(?:orization)?|auth[-_]?token|access[-_]?token|token)|cookie|credential|private[_-]?key)[\"']?\s*[:=]\s*[\"']?)(?P<value>(?!(?:Bearer)\b|\[已脱敏\])[^\s,}\"']+)",
    re.IGNORECASE,
)
_SECRET_QUOTED_RE = re.compile(
    r"(?P<prefix>[\"']?(?:token|access[_-]?token|refresh[_-]?token|secret|client[_-]?secret|password|passwd|api[_-]?key|auth[_-]?token|authorization|x[-_]?(?:api[-_]?key|auth(?:orization)?|auth[-_]?token|access[-_]?token|token)|cookie|credential|private[_-]?key)[\"']?\s*[:=]\s*[\"'])"
    r"(?P<value>.*?)(?P<suffix>[\"'])",
    re.IGNORECASE,
)
_BEARER_QUOTED_RE = re.compile(
    r"(?P<prefix>\bBearer\s+)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)
_FLAG_SECRET_QUOTED_RE = re.compile(
    r"(?P<prefix>(?<![\w-])--?(?:token|secret|password|passwd|api[-_]?key|auth[-_]?token|authorization|x[-_]?(?:api[-_]?key|auth(?:orization)?|auth[-_]?token|access[-_]?token|token))\b(?:=|\s+))"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?P<prefix>\bBearer\s+)(?P<value>[^\s,;\"']+)", re.IGNORECASE)
_FLAG_SECRET_RE = re.compile(
    # 需要在完整 flag 边界匹配；否则 ``token-secret`` 中的 ``-secret`` 会
    # 被误认为 ``-secret <下一个词>``，反而把后续文本吞掉并留下密钥。
    r"(?P<prefix>(?<![\w-])--?(?:token|secret|password|passwd|api[-_]?key|auth[-_]?token|authorization|x[-_]?(?:api[-_]?key|auth(?:orization)?|auth[-_]?token|access[-_]?token|token))\b(?:=|\s+))(?P<value>[^\s]+)",
    re.IGNORECASE,
)
# MCP/Shell 参数中也常见 ``token abc``、``apiKey abc`` 这类没有 ``=`` 或
# ``:`` 的键值写法；如果只覆盖赋值形式，密钥会从命令摘要中漏出。
_SECRET_BARE_QUOTED_RE = re.compile(
    r"(?P<prefix>(?<![\w-])(?:access[_-]?token|accesstoken|refresh[_-]?token|refreshtoken|"
    r"client[_-]?secret|clientsecret|private[_-]?key|privatekey|token|secret|"
    r"password|passwd|api[_-]?key|apikey|auth[_-]?token|authorization|auth|"
    r"x[-_]?(?:api[-_]?key|auth(?:orization)?|auth[-_]?token|access[-_]?token|token)|cookie|credential)"
    r"\b\s+[\"'])"
    r"(?P<value>.*?)(?P<suffix>[\"'])",
    re.IGNORECASE,
)
_SECRET_BARE_RE = re.compile(
    r"(?P<prefix>(?<![\w-])(?:access[_-]?token|accesstoken|refresh[_-]?token|refreshtoken|"
    r"client[_-]?secret|clientsecret|private[_-]?key|privatekey|token|secret|"
    r"password|passwd|api[_-]?key|apikey|auth[_-]?token|authorization|auth|"
    r"x[-_]?(?:api[-_]?key|auth(?:orization)?|auth[-_]?token|access[-_]?token|token)|cookie|credential)"
    r"\b\s+)(?P<value>(?!Bearer\s+\[已脱敏\])(?!\[已脱敏\])"
    r"(?:(?:Bearer)\s+)?[^\s,;\"']+)",
    re.IGNORECASE,
)
_HIDDEN_KEYS = {
    "content",
    "old_text",
    "new_text",
    "body",
    "payload",
    "input",
    "data",
    "env",
    "environment",
    "cwd",
    "workdir",
    "working_directory",
    # 请求头、Cookie 和环境容器通常是密钥的另一种载体；即使值是
    # 字符串而不是对象，也不应因为字段名不匹配而原样进入 Web 事件。
    "headers",
    "header",
    "cookies",
}
_APPROVAL_STATES = frozenset({
    "none",
    "pending",
    "submitting",
    "allowed-once",
    "allowed-session",
    "rejected",
    "cancelled",
    "expired",
    "unavailable",
})
_COMMAND_KEYS = {"command", "cmd", "shell_command"}
_BARE_SECRET_STOPWORDS = {
    # 常见自然语言短语（如 ``password reset``）不是密钥载体；仅对裸空格
    # 形式跳过这些词，赋值、Bearer 和显式 flag 仍按安全规则强制脱敏。
    "available",
    "completed",
    "count",
    "done",
    "error",
    "failed",
    "field",
    "fields",
    "here",
    "input",
    "name",
    "no",
    "optional",
    "output",
    "pending",
    "required",
    "reset",
    "text",
    "true",
    "unknown",
    "unavailable",
    "value",
    "values",
    "yes",
}


def project_tool_arguments(
    tool_name: str,
    arguments: Mapping[str, object] | None,
) -> dict[str, object]:
    """仅保留工具边界摘要，不复制正文、密钥或复杂 MCP 参数。"""

    source = arguments if isinstance(arguments, Mapping) else {}
    name = str(tool_name or "")
    if name == "write_file":
        raw_length = source.get("content_length")
        try:
            content_length = max(0, int(raw_length)) if raw_length is not None else len(str(source.get("content") or ""))
        except (TypeError, ValueError):
            content_length = len(str(source.get("content") or ""))
        return {
            "path": _bounded(_redact_string(source.get("path"))),
            "content_length": content_length,
        }
    if name == "edit_file":
        def projected_length(key: str, source_key: str) -> int:
            raw_length = source.get(key)
            try:
                return max(0, int(raw_length)) if raw_length is not None else len(str(source.get(source_key) or ""))
            except (TypeError, ValueError):
                return len(str(source.get(source_key) or ""))

        return {
            "path": _bounded(_redact_string(source.get("path"))),
            "replace_all": bool(source.get("replace_all", False)),
            "old_text_length": projected_length("old_text_length", "old_text"),
            "new_text_length": projected_length("new_text_length", "new_text"),
        }

    displayed: dict[str, object] = {}
    for raw_key, raw_value in source.items():
        key = str(raw_key)
        normalized = re.sub(r"[\s.]+", "_", key.lower().replace("-", "_"))
        if _SECRET_KEY_RE.search(normalized):
            displayed[key] = "[已脱敏]"
        elif normalized in _HIDDEN_KEYS:
            # 正文和环境变量只保留规模；cwd/workdir 不把内部路径送到浏览器。
            if normalized in {"content", "old_text", "new_text", "body", "payload", "input", "data"}:
                displayed[f"{key}_length"] = len(str(raw_value or ""))
            else:
                displayed[key] = "[已隐藏]"
        elif normalized in _COMMAND_KEYS:
            displayed[key] = redact_command(str(raw_value or ""))
        elif isinstance(raw_value, str):
            displayed[key] = _redact_string(raw_value)
        elif isinstance(raw_value, (int, float, bool)) or raw_value is None:
            displayed[key] = raw_value
        else:
            # 复杂对象可能包含嵌套密钥；只返回类型和规模，不递归复制原文。
            displayed[key] = complex_value_summary(raw_value)
    return displayed


def redact_command(command: str) -> str:
    """脱敏 Shell 中常见的环境变量和 secret flag，并限制展示长度。"""

    return _redact_string(command)


def project_result_preview(value: object, *, max_length: int = _MAX_DISPLAY_TEXT) -> str:
    """返回可展示的短结果摘要；不保留完整工具输出。"""

    if value is None:
        return ""
    # 结构化结果不递归复制，避免嵌套对象把密钥或完整输出带进展示层。
    if isinstance(value, Mapping) or isinstance(value, (list, tuple, set)):
        return complex_value_summary(value)
    try:
        limit = max(0, int(max_length))
    except (TypeError, ValueError):
        limit = _MAX_DISPLAY_TEXT
    return _redact_string(value, max_length=limit)


def project_tool_call(
    call: Mapping[str, object],
    *,
    include_model_fields: bool = False,
) -> dict[str, Any]:
    """将单次调用转换为语义消息/快照可安全存储的形状。"""

    name = str(call.get("name") or "tool")
    projected: dict[str, Any] = {
        "call_id": str(call.get("call_id") or ""),
        "name": name,
        "arguments": project_tool_arguments(name, call.get("arguments") if isinstance(call.get("arguments"), Mapping) else {}),
        "status": _safe_status(call.get("status")),
        "result": project_result_preview(call.get("result_preview", call.get("result", ""))),
        "result_preview": project_result_preview(call.get("result_preview", call.get("result", ""))),
    }
    # durable model surface 需要兼容旧历史中的 ``ok`` 标记；Web/API 默认
    # 仍统一投影为 completed，避免把内部兼容语义暴露给新客户端。
    if include_model_fields and str(call.get("status") or "").strip().lower() == "ok":
        projected["status"] = "ok"
    # 语义消息默认只展示摘要，但无 durable surface 的旧部署仍需用完整参数
    # 重建下一轮模型 transcript。私有键不会经过再次 project_tool_call 的 Web 投影。
    if include_model_fields:
        raw_arguments = call.get("_model_arguments", call.get("arguments"))
        if isinstance(raw_arguments, Mapping):
            projected["_model_arguments"] = deepcopy(dict(raw_arguments))
        if "_model_result" in call:
            projected["_model_result"] = str(call.get("_model_result") or "")
        elif "result" in call:
            projected["_model_result"] = str(call.get("result") or "")
        raw_blocks = call.get("_model_content_blocks", call.get("content_blocks"))
        if isinstance(raw_blocks, list):
            projected["_model_content_blocks"] = deepcopy(raw_blocks)
    # 运行快照需要保留审批与工具行的关联；这里只携带短 ID 和有限状态，
    # 不能把审批参数、指纹或其它内部字段一并透传。普通历史投影同样只在
    # 生产者明确提供时携带，旧事件不会凭空增加字段。
    approval_id = str(call.get("approval_id") or "").strip()
    if approval_id:
        projected["approval_id"] = approval_id[:128]
    approval_state = str(call.get("approval_state") or "").strip().lower()
    if approval_state in _APPROVAL_STATES:
        projected["approval_state"] = approval_state
    # 仅复制时间/诊断字段，过滤内部参数和完整 content blocks。
    for key in (
        "started_at",
        "ended_at",
        "approval_requested_at",
        "approval_resolved_at",
    ):
        raw = call.get(key)
        if raw is not None and str(raw).strip():
            projected[key] = str(raw).strip()
    for key in ("duration_ms", "approval_wait_ms", "execution_ms", "group_duration_ms", "exit_code"):
        raw = call.get(key)
        if raw is None:
            continue
        try:
            number = int(raw)
        except (TypeError, ValueError):
            continue
        if key == "exit_code" or number >= 0:
            projected[key] = number
    for key in ("result_kind", "error_code"):
        raw = call.get(key)
        if raw is not None and str(raw).strip():
            projected[key] = str(raw).strip()[:80]
    if call.get("is_truncated") is not None:
        projected["is_truncated"] = bool(call.get("is_truncated"))
    return projected


def project_tool_chain(
    chain: object,
    *,
    include_model_fields: bool = False,
) -> list[dict[str, Any]]:
    """统一投影整个 tool_chain；非法或未知调用被丢弃而不是原样透传。"""

    if not isinstance(chain, list):
        return []
    projected_groups: list[dict[str, Any]] = []
    for raw_group in chain:
        if not isinstance(raw_group, Mapping):
            continue
        calls = raw_group.get("calls")
        if not isinstance(calls, list):
            continue
        projected_calls = [
            project_tool_call(call, include_model_fields=include_model_fields)
            for call in calls
            if isinstance(call, Mapping) and str(call.get("call_id") or "")
        ]
        if not projected_calls and not include_model_fields:
            continue
        group: dict[str, Any] = {
            "iteration": _non_negative_int(raw_group.get("iteration")),
            "text": project_result_preview(raw_group.get("text", "")),
            "calls": projected_calls,
        }
        if include_model_fields:
            if "text" in raw_group:
                group["_model_text"] = str(raw_group.get("text") or "")
            provider_fields = raw_group.get("provider_fields")
            if isinstance(provider_fields, Mapping):
                reasoning = provider_fields.get("reasoning_content")
                if isinstance(reasoning, str):
                    group["provider_fields"] = {"reasoning_content": reasoning}
        for key in ("group_duration_ms",):
            value = raw_group.get(key)
            if value is not None:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                if number >= 0:
                    group[key] = number
        # provider_fields 可能包含原始供应商内容，不进入展示投影。
        projected_groups.append(group)
    return projected_groups


def complex_value_summary(value: object) -> str:
    if isinstance(value, Mapping):
        return f"[对象，{len(value)} 个字段]"
    if isinstance(value, (list, tuple, set)):
        return f"[列表，{len(value)} 项]"
    return "[已隐藏]"


def _redact_string(value: object, *, max_length: int = _MAX_DISPLAY_TEXT) -> str:
    """统一处理所有进入展示层的字符串，先脱敏再截断。"""

    text = str(value if value is not None else "")
    text = _SECRET_QUOTED_RE.sub(
        lambda match: f"{match.group('prefix')}[已脱敏]{match.group('suffix')}",
        text,
    )
    text = _BEARER_QUOTED_RE.sub(
        lambda match: f"{match.group('prefix')}[已脱敏]",
        text,
    )
    text = _FLAG_SECRET_QUOTED_RE.sub(
        lambda match: f"{match.group('prefix')}[已脱敏]",
        text,
    )
    # Bearer 的非引号值最后处理；引号形式必须先于该规则，否则只会替换
    # 开引号前的半段并把带空格的尾部留在展示文本中。
    text = _BEARER_RE.sub(
        lambda match: f"{match.group('prefix')}[已脱敏]",
        text,
    )
    text = _SECRET_ASSIGNMENT_RE.sub(_redact_assignment, text)
    text = _SECRET_JSON_RE.sub(
        lambda match: f"{match.group('prefix')}[已脱敏]",
        text,
    )
    text = _FLAG_SECRET_RE.sub(
        lambda match: f"{match.group('prefix')}[已脱敏]",
        text,
    )
    text = _SECRET_BARE_QUOTED_RE.sub(
        _redact_bare_quoted,
        text,
    )
    text = _SECRET_BARE_RE.sub(_redact_bare, text)
    try:
        limit = max(0, int(max_length))
    except (TypeError, ValueError):
        limit = _MAX_DISPLAY_TEXT
    return str(_bounded(text, limit))


def _redact_assignment(match: re.Match[str]) -> str:
    """替换赋值形式的密钥，并保留 Bearer 方案名以便用户识别字段。"""

    raw_value = str(match.group("value") or "")
    prefix = "Bearer " if raw_value.lower().startswith("bearer ") else ""
    return f"{match.group('key')}{match.group('separator')}{prefix}[已脱敏]"


def _redact_bare(match: re.Match[str]) -> str:
    """替换以空格分隔的密钥值，同时保留 Bearer 方案名。"""

    raw_value = str(match.group("value") or "")
    value = raw_value[7:] if raw_value.lower().startswith("bearer ") else raw_value
    if value.lower() in _BARE_SECRET_STOPWORDS:
        return match.group(0)
    prefix = "Bearer " if raw_value.lower().startswith("bearer ") else ""
    return f"{match.group('prefix')}{prefix}[已脱敏]"


def _redact_bare_quoted(match: re.Match[str]) -> str:
    """处理带引号的空格分隔密钥，并避免自然语言短语误报。"""

    value = str(match.group("value") or "")
    if value.strip().lower() in _BARE_SECRET_STOPWORDS:
        return match.group(0)
    prefix = str(match.group("prefix") or "")
    # 正则前缀包含开引号；展示层不保留不成对的引号，避免摘要出现
    # ``token \"[已脱敏]`` 这种看起来像截断的伪值。
    if prefix.endswith(('"', "'")):
        prefix = prefix[:-1]
    return f"{prefix}[已脱敏]"


def _redact_flag_quoted(match: re.Match[str]) -> str:
    """完整替换带空格的 flag 值，避免引号后的正文残留。"""

    return f"{match.group('prefix')}[已脱敏]"


def _bounded(value: object, max_length: int = _MAX_DISPLAY_TEXT) -> object:
    if not isinstance(value, str):
        return value
    return value if len(value) <= max_length else f"{value[:max_length]}…"


def _safe_status(value: object) -> str:
    status = str(value or "unknown").strip().lower()
    if status == "ok":
        return "completed"
    if status in {
        "running",
        "completed",
        "error",
        "interrupted",
        "cancelled",
        "expired",
        "unavailable",
        "rejected",
        "unknown",
    }:
        return status
    return "unknown"


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "complex_value_summary",
    "project_result_preview",
    "project_tool_arguments",
    "project_tool_call",
    "project_tool_chain",
    "redact_command",
]
