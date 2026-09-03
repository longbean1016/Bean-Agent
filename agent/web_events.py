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
    SessionUsageUpdated,
    SessionUpdated,
    StreamDeltaReady,
    ToolCallCompleted,
    ToolCallStarted,
    TurnQueued,
    TurnQueueRejected,
    TurnStarted,
)
from agent.message_bus import OutboundMessage

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
    | ToolCallStarted
    | ToolCallCompleted
    | SandboxApprovalRequested
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
        if isinstance(event, StreamDeltaReady):
            payloads: list[JsonPayload] = []
            # 同一事件含双 delta 时正文必须先到，保持前端 reducer 的既有时序。
            if event.content_delta:
                payloads.append({
                    "type": "answer.delta",
                    "session_id": event.session_key,
                    "turn_id": event.turn_id,
                    "delta": event.content_delta,
                })
            if event.thinking_delta:
                payloads.append({
                    "type": "react.thinking.delta",
                    "session_id": event.session_key,
                    "turn_id": event.turn_id,
                    "delta": event.thinking_delta,
                })
            return MappedWebEvent(event.session_key, tuple(payloads))
        if isinstance(event, ToolCallStarted):
            return _mapped(event.session_key, {
                "type": "react.tool.started",
                "session_id": event.session_key,
                "turn_id": event.turn_id,
                "call_id": event.call_id,
                "tool_name": event.tool_name,
                "arguments": dict(event.arguments),
            })
        if isinstance(event, ToolCallCompleted):
            return _mapped(event.session_key, {
                "type": "react.tool.completed",
                "session_id": event.session_key,
                "turn_id": event.turn_id,
                "call_id": event.call_id,
                "tool_name": event.tool_name,
                "status": event.status,
                "result_preview": event.result_preview,
            })
        if isinstance(event, SandboxApprovalRequested):
            return _mapped(event.session_key, {
                "type": "approval.requested",
                "session_id": event.session_key,
                "approval": dict(event.request),
            })
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
        return _mapped(session_key, {"type": "turn.snapshot", **dict(snapshot)})

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


__all__ = [
    "JsonPayload",
    "MappedWebEvent",
    "WebEventMapper",
    "WebLifecycleEvent",
]
