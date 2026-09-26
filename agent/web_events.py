"""Agent 消息和生命周期事件到 Web 协议帧的纯映射。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

from agent.agent_loop import InterruptResult
from agent.event_bus import (
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ContextCompactionStarted,
    ContextUsageUpdated,
    SandboxApprovalRequested,
    SandboxApprovalResolved,
    SessionUsageUpdated,
    SessionUpdated,
    StreamDeltaReady,
    TurnPresentationUpdated,
    ToolCallCompleted,
    ToolCallStarted,
    TurnQueued,
    TurnQueueRejected,
    TurnStarted,
)
from agent.message_bus import OutboundMessage
from agent.tool_projection import (
    project_result_preview,
    project_tool_arguments,
    project_tool_call,
)

JsonPayload: TypeAlias = dict[str, Any]
WebLifecycleEvent: TypeAlias = (
    TurnStarted
    | ContextCompactionStarted
    | ContextCompactionCompleted
    | ContextCompactionFailed
    | ContextUsageUpdated
    | SessionUsageUpdated
    | SessionUpdated
    | TurnQueued
    | TurnQueueRejected
    | StreamDeltaReady
    | TurnPresentationUpdated
    | ToolCallStarted
    | ToolCallCompleted
    | SandboxApprovalRequested
    | SandboxApprovalResolved
)


@dataclass(frozen=True, slots=True)
class MappedWebEvent:
    session_key: str
    payloads: tuple[JsonPayload, ...]


class WebEventMapper:
    """不持有连接状态的 Web 协议映射器。"""

    def map_outbound(self, message: OutboundMessage) -> MappedWebEvent:
        session_key = f"{message.channel}:{message.chat_id}"
        metadata = dict(message.metadata)
        return _mapped(session_key, {
            "type": "message.final",
            "request_id": str(metadata.get("request_id") or ""),
            "session_id": session_key,
            "turn_id": str(metadata.get("turn_id") or ""),
            "content": message.content,
            "thinking": message.thinking,
            "media": list(message.media),
            "message_id": str(metadata.get("message_id") or ""),
            "metadata": metadata,
        })

    def map_event(self, event: WebLifecycleEvent) -> MappedWebEvent:
        if isinstance(event, TurnStarted):
            return _mapped(event.session_key, {
                "type": "turn.started",
                "request_id": event.request_id,
                "session_id": event.session_key,
                "turn_id": event.turn_id,
            })
        if isinstance(event, ContextCompactionStarted):
            return _mapped(event.session_key, {
                "type": "context.compaction.started",
                "session_id": event.session_key,
                "turn_id": event.turn_id,
                "trigger": event.trigger,
                "estimated_tokens": event.estimated_tokens,
            })
        if isinstance(event, ContextCompactionCompleted):
            return _mapped(event.session_key, {
                "type": "context.compaction.completed",
                "session_id": event.session_key,
                "turn_id": event.turn_id,
                "trigger": event.trigger,
                "estimated_tokens": event.estimated_tokens,
                "compacted": event.compacted,
            })
        if isinstance(event, ContextCompactionFailed):
            return _mapped(event.session_key, {
                "type": "context.compaction.failed",
                "session_id": event.session_key,
                "turn_id": event.turn_id,
                "trigger": event.trigger,
                "estimated_tokens": event.estimated_tokens,
                "message": event.error,
            })
        if isinstance(event, ContextUsageUpdated):
            payload = {
                "type": "context.usage.updated",
                "session_id": event.session_key,
                "turn_id": event.turn_id,
                "used_tokens": event.used_tokens,
                "context_window": event.context_window,
                "soft_limit_tokens": event.soft_limit_tokens,
                "hard_input_tokens": event.hard_input_tokens,
                "context_window_source": event.context_window_source,
                "estimate_source": event.estimate_source,
                "breakdown": dict(event.breakdown),
                "sections": [dict(section) for section in event.sections],
            }
            optional = {
                "pressure_tokens": event.pressure_tokens,
                "projected_tokens": event.projected_tokens,
                "surface_tokens": event.surface_tokens,
                "system_tokens": event.system_tokens,
                "tools_tokens": event.tools_tokens,
                "message_tokens": event.message_tokens,
                "as_of_seq": event.as_of_seq,
                "model_runtime_id": event.model_runtime_id,
                "model": event.model,
            }
            payload.update({key: value for key, value in optional.items() if value is not None})
            return _mapped(event.session_key, payload)
        if isinstance(event, SessionUsageUpdated):
            return _mapped(event.session_key, {
                "type": "session.usage.updated",
                "session_id": event.session_key,
                "turn_id": event.turn_id,
                "total_uncached_input_tokens": event.total_uncached_input_tokens,
                "total_cache_read_tokens": event.total_cache_read_tokens,
                "total_cache_write_tokens": event.total_cache_write_tokens,
                "total_input_tokens": event.total_input_tokens,
                "cache_hit_rate": event.cache_hit_rate,
                "total_output_tokens": event.total_output_tokens,
            })
        if isinstance(event, SessionUpdated):
            return _mapped(event.session_key, {
                "type": "session.updated",
                "session": dict(event.session),
            })
        if isinstance(event, TurnQueued):
            return _mapped(event.session_key, {
                "type": "turn.queued",
                "request_id": event.request_id,
                "session_id": event.session_key,
                "position": event.position,
            })
        if isinstance(event, TurnQueueRejected):
            messages = {
                "queue_full": "当前任务较多，请稍后再试",
                "session_busy": "当前会话正在处理消息",
                "closed": "服务正在关闭，请稍后再试",
            }
            return _mapped(event.session_key, {
                "type": "error",
                "request_id": event.request_id,
                "session_id": event.session_key,
                "code": event.reason,
                "message": messages.get(event.reason, "消息暂时无法处理"),
            })
        if isinstance(event, TurnPresentationUpdated):
            return _mapped(event.session_key, {
                "type": "turn.presentation", "session_id": event.session_key,
                "turn_id": event.turn_id, "presentation": event.presentation,
            })
        if isinstance(event, StreamDeltaReady):
            payloads: list[JsonPayload] = []
            # 同一事件含双 delta 时正文必须先到，保持前端 reducer 的既有时序。
            if event.content_delta:
                payloads.append({
                    "type": "answer.delta",
                    "session_id": event.session_key,
                    "turn_id": event.turn_id,
                    "delta": event.content_delta,
                    **({"part_id": event.content_part_id} if event.content_part_id else {}),
                })
            if event.thinking_delta:
                payloads.append({
                    "type": "react.thinking.delta",
                    "session_id": event.session_key,
                    "turn_id": event.turn_id,
                    "delta": event.thinking_delta,
                    **({"part_id": event.thinking_part_id} if event.thinking_part_id else {}),
                })
            return MappedWebEvent(event.session_key, tuple(payloads))
        if isinstance(event, ToolCallStarted):
            payload: JsonPayload = {
                "type": "react.tool.started",
                "session_id": event.session_key,
                "turn_id": event.turn_id,
                "call_id": event.call_id,
                "tool_name": event.tool_name,
                "arguments": project_tool_arguments(event.tool_name, event.arguments),
            }
            _add_tool_timing(
                payload,
                started_at=event.started_at,
                approval_requested_at=event.approval_requested_at,
            )
            return _mapped(event.session_key, payload)
        if isinstance(event, ToolCallCompleted):
            payload = {
                "type": "react.tool.completed",
                "session_id": event.session_key,
                "turn_id": event.turn_id,
                "call_id": event.call_id,
                "tool_name": event.tool_name,
                # 兼容旧 Pipeline 的 ``ok``，并对未知值 fail-safe；不能把
                # 任意新/拼写错误状态伪装成 completed。
                "status": _normalize_tool_status(event.status),
                "result_preview": project_result_preview(event.result_preview),
            }
            _add_tool_timing(
                payload,
                started_at=event.started_at,
                ended_at=event.ended_at,
                duration_ms=event.duration_ms,
                approval_requested_at=event.approval_requested_at,
                approval_resolved_at=event.approval_resolved_at,
                approval_wait_ms=event.approval_wait_ms,
                execution_ms=event.execution_ms,
                group_duration_ms=event.group_duration_ms,
                result_kind=event.result_kind,
                is_truncated=event.is_truncated,
                exit_code=event.exit_code,
                error_code=event.error_code,
            )
            return _mapped(event.session_key, payload)
        if isinstance(event, SandboxApprovalRequested):
            return _mapped(event.session_key, {
                "type": "approval.requested",
                "session_id": event.session_key,
                # approval.requested 只向浏览器投影执行边界；尤其不把
                # fingerprint/内部 schema/未知字段原样广播。身份字段仍保留
                # 供 reducer 将卡片绑定到对应 turn/call。
                "approval": _project_approval_request(event.request),
            })
        if isinstance(event, SandboxApprovalResolved):
            payload: JsonPayload = {
                "type": "approval.resolved",
                # request_id 是客户端决定命令的关联 ID；超时/断线没有命令
                # ID 时保持空字符串。approval_id 始终使用审批实体 ID，避免
                # 把两个不同的幂等键混为一谈。
                "request_id": event.client_request_id or "",
                "session_id": event.session_key,
                "approval_id": event.request_id,
                "decision": event.decision,
                "turn_id": event.turn_id,
                "call_id": event.call_id,
                "state": event.state,
                "decided_at": event.decided_at,
            }
            if event.error_code:
                payload["error_code"] = event.error_code
            return _mapped(event.session_key, payload)
        raise TypeError(f"不支持的 Web 事件: {type(event).__name__}")

    def map_context_usage_snapshot(
        self,
        session_key: str,
        snapshot: Mapping[str, Any],
    ) -> MappedWebEvent:
        pressure = snapshot.get("pressure_tokens")
        projected = snapshot.get("projected_tokens")
        used = projected if projected is not None else pressure
        payload = {
            "type": "context.usage.updated",
            "session_id": session_key,
            "turn_id": "",
            "used_tokens": int(used or 0),
            "context_window": int(snapshot.get("context_window") or 0),
            "soft_limit_tokens": int(snapshot.get("soft_limit_tokens") or 0),
            "hard_input_tokens": int(snapshot.get("hard_input_tokens") or 0),
            "context_window_source": str(snapshot.get("context_window_source") or "unknown"),
            "estimate_source": "provider_projected" if projected is not None else "unknown",
            "breakdown": {
                "system_prompt_tokens": int(snapshot.get("system_tokens") or 0),
                "tools_tokens": int(snapshot.get("tools_tokens") or 0),
                "conversation_tokens": int(snapshot.get("message_tokens") or 0),
                "overhead_tokens": 0,
            },
            "sections": [],
        }
        for key in (
            "pressure_tokens",
            "projected_tokens",
            "surface_tokens",
            "system_tokens",
            "tools_tokens",
            "message_tokens",
            "as_of_seq",
            "model_runtime_id",
            "model",
        ):
            if snapshot.get(key) is not None:
                payload[key] = snapshot[key]
        return _mapped(session_key, payload)

    def map_context_usage_reset(self, session_key: str) -> MappedWebEvent:
        return _mapped(session_key, {
            "type": "context.usage.reset",
            "session_id": session_key,
        })

    def map_session_usage_snapshot(
        self,
        session_key: str,
        snapshot: Mapping[str, Any],
    ) -> MappedWebEvent:
        payload = {
            "type": "session.usage.updated",
            "session_id": session_key,
            "turn_id": "",
            "total_uncached_input_tokens": _non_negative_int(
                snapshot.get("total_uncached_input_tokens")
            ),
            "total_cache_read_tokens": _non_negative_int(
                snapshot.get("total_cache_read_tokens")
            ),
            "total_cache_write_tokens": _non_negative_int(
                snapshot.get("total_cache_write_tokens")
            ),
            "total_input_tokens": _non_negative_int(snapshot.get("total_input_tokens")),
            "cache_hit_rate": (
                float(snapshot["cache_hit_rate"])
                if snapshot.get("cache_hit_rate") is not None
                else None
            ),
            "total_output_tokens": _non_negative_int(snapshot.get("total_output_tokens")),
        }
        return _mapped(session_key, payload)

    def map_active_turn_snapshot(
        self,
        session_key: str,
        snapshot: Mapping[str, Any],
    ) -> MappedWebEvent:
        # 运行快照同时供模型恢复使用，可能含有 llm_surface_messages、原始
        # content blocks 等内部字段；不能用 ``**dict(snapshot)`` 透传到 Web。
        # 这里只建立协议明确的展示白名单，工具参数/结果再由统一投影处理。
        payload: JsonPayload = {
            "type": "turn.snapshot",
            "session_id": session_key,
        }
        for key in ("turn_id", "request_id", "user_message", "content", "thinking", "started_at", "status"):
            value = snapshot.get(key)
            if value is None:
                continue
            if key in {"user_message", "content", "thinking"}:
                payload[key] = str(value)
            else:
                text = str(value).strip()
                if text:
                    payload[key] = text
        raw_media = snapshot.get("user_media")
        if isinstance(snapshot.get("presentation"), dict):
            payload["presentation"] = snapshot["presentation"]
        if isinstance(raw_media, list):
            payload["user_media"] = [str(item) for item in raw_media if isinstance(item, str)]
        raw_tools = snapshot.get("tools")
        if isinstance(raw_tools, list):
            safe_tools: list[dict[str, Any]] = []
            for raw_tool in raw_tools:
                if not isinstance(raw_tool, Mapping):
                    continue
                safe_tools.append(project_tool_call(raw_tool))
            payload["tools"] = safe_tools
        raw_chain = snapshot.get("tool_chain_partial")
        if isinstance(raw_chain, list):
            # snapshot 的 tool_chain_partial 只用于中断恢复，不向 Web 暴露
            # provider_fields 或原始结果；按工具调用逐项投影。
            safe_groups: list[dict[str, Any]] = []
            for group in raw_chain:
                if not isinstance(group, Mapping):
                    continue
                calls = group.get("calls")
                if not isinstance(calls, list):
                    continue
                safe_calls = [
                    project_tool_call(call)
                    for call in calls
                    if isinstance(call, Mapping)
                ]
                if safe_calls:
                    safe_group: dict[str, Any] = {
                        "iteration": group.get("iteration", 0),
                        "text": project_result_preview(group.get("text", "")),
                        "calls": safe_calls,
                    }
                    raw_duration = group.get("group_duration_ms")
                    try:
                        duration = int(raw_duration) if raw_duration is not None else -1
                    except (TypeError, ValueError):
                        duration = -1
                    if duration >= 0:
                        safe_group["group_duration_ms"] = duration
                    safe_groups.append(safe_group)
            payload["tool_chain_partial"] = safe_groups
        return _mapped(session_key, payload)

    def map_interrupted(
        self,
        session_key: str,
        request_id: str,
        result: InterruptResult,
    ) -> MappedWebEvent:
        payload = {
            "type": "turn.interrupted",
            "request_id": request_id,
            "session_id": session_key,
            "turn_id": result.turn_id,
            "status": result.status,
        }
        if result.duration_ms is not None:
            payload["duration_ms"] = result.duration_ms
        if result.ended_at:
            payload["ended_at"] = result.ended_at
        return _mapped(session_key, payload)

    def map_pending_notification(
        self,
        session_key: str,
        *,
        content: str,
        message_id: str,
        metadata: Mapping[str, Any],
    ) -> MappedWebEvent:
        return _mapped(session_key, {
            "type": "message.final",
            "request_id": "",
            "session_id": session_key,
            "turn_id": "",
            "content": content,
            "thinking": "",
            "media": [],
            "message_id": message_id,
            "metadata": dict(metadata),
        })


def _mapped(session_key: str, payload: JsonPayload) -> MappedWebEvent:
    return MappedWebEvent(session_key, (payload,))


def _non_negative_int(value: object) -> int:
    return max(0, int(value or 0))


def _normalize_tool_status(value: object) -> str:
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


def _add_tool_timing(
    payload: JsonPayload,
    *,
    started_at: object = None,
    ended_at: object = None,
    duration_ms: object = None,
    approval_requested_at: object = None,
    approval_resolved_at: object = None,
    approval_wait_ms: object = None,
    execution_ms: object = None,
    group_duration_ms: object = None,
    result_kind: object = None,
    is_truncated: object = None,
    exit_code: object = None,
    error_code: object = None,
) -> None:
    """把可选工具计时字段加入 Web 帧，旧事件保持原有形状。"""

    for key, value in (
        ("started_at", started_at),
        ("ended_at", ended_at),
        ("approval_requested_at", approval_requested_at),
        ("approval_resolved_at", approval_resolved_at),
        ("result_kind", result_kind),
        ("error_code", error_code),
    ):
        text = str(value or "").strip()
        if text:
            payload[key] = text
    for key, value in (
        ("duration_ms", duration_ms),
        ("approval_wait_ms", approval_wait_ms),
        ("execution_ms", execution_ms),
        ("group_duration_ms", group_duration_ms),
        ("exit_code", exit_code),
    ):
        if value is None:
            continue
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            number = -1
        if key == "exit_code" or number >= 0:
            payload[key] = number
    if is_truncated is not None:
        payload["is_truncated"] = bool(is_truncated)


_APPROVAL_PUBLIC_FIELDS = (
    "id",
    "session_id",
    "turn_id",
    "call_id",
    "tool_name",
    "operation",
    "arguments",
    "reason",
    "requested_mode",
    "state",
    "created_at",
    "requested_at",
    "expires_at",
    "summary",
    "scope",
    "reason_code",
)


def _project_approval_request(value: object) -> dict[str, Any]:
    """生成审批卡所需的最小安全投影。

    ``fingerprint`` 只用于后端审计和幂等绑定，不是 UI 展示字段；参数再次
    经过统一工具投影，兼容旧生产者直接塞入原始 arguments 的情况。
    """

    if not isinstance(value, Mapping):
        return {}
    tool_name = str(value.get("tool_name") or "")
    projected: dict[str, Any] = {}
    for key in _APPROVAL_PUBLIC_FIELDS:
        if key not in value:
            continue
        raw = value.get(key)
        if key == "arguments":
            if isinstance(raw, Mapping):
                projected[key] = project_tool_arguments(
                    tool_name,
                    dict(raw),
                )
            continue
        if raw is None:
            continue
        if key in {"operation", "reason", "summary", "scope", "reason_code"}:
            text = str(raw).strip()
            if text:
                projected[key] = project_result_preview(text)
            continue
        projected[key] = raw
    return projected


__all__ = [
    "JsonPayload",
    "MappedWebEvent",
    "WebEventMapper",
    "WebLifecycleEvent",
]
