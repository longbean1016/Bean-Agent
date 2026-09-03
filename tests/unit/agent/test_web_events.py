"""Web 事件映射器的协议契约测试。"""

from __future__ import annotations

import pytest

from agent.agent_loop import InterruptResult
from agent.event_bus import (
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ContextCompactionStarted,
    ContextUsageUpdated,
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
from agent.web_events import WebEventMapper


def test_outbound_message_maps_complete_final_payload() -> None:
    message = OutboundMessage(
        "web",
        "chat",
        "answer",
        thinking="reason",
        media=["result.png"],
        metadata={
            "request_id": "request-1",
            "turn_id": "turn-1",
            "message_id": "message-1",
            "notification_id": "notification-1",
        },
    )

    mapped = WebEventMapper().map_outbound(message)

    assert mapped.session_key == "web:chat"
    assert mapped.payloads == ({
        "type": "message.final",
        "request_id": "request-1",
        "session_id": "web:chat",
        "turn_id": "turn-1",
        "content": "answer",
        "thinking": "reason",
        "media": ["result.png"],
        "message_id": "message-1",
        "metadata": message.metadata,
    },)
    assert mapped.payloads[0]["metadata"] is not message.metadata


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            TurnStarted("web:chat", "turn-1", "request-1", "ignored content"),
            {
                "type": "turn.started",
                "request_id": "request-1",
                "session_id": "web:chat",
                "turn_id": "turn-1",
            },
        ),
        (
            ContextCompactionStarted("web:chat", "turn-1", "soft_limit", 1200),
            {
                "type": "context.compaction.started",
                "session_id": "web:chat",
                "turn_id": "turn-1",
                "trigger": "soft_limit",
                "estimated_tokens": 1200,
            },
        ),
        (
            ContextCompactionCompleted("web:chat", "turn-1", "soft_limit", 1200, True),
            {
                "type": "context.compaction.completed",
                "session_id": "web:chat",
                "turn_id": "turn-1",
                "trigger": "soft_limit",
                "estimated_tokens": 1200,
                "compacted": True,
            },
        ),
        (
            ContextCompactionFailed("web:chat", "turn-1", "soft_limit", 1200, "failed"),
            {
                "type": "context.compaction.failed",
                "session_id": "web:chat",
                "turn_id": "turn-1",
                "trigger": "soft_limit",
                "estimated_tokens": 1200,
                "message": "failed",
            },
        ),
        (
            SessionUsageUpdated("web:chat", "turn-1", 1, 2, 3, 6, 0.5, 4),
            {
                "type": "session.usage.updated",
                "session_id": "web:chat",
                "turn_id": "turn-1",
                "total_uncached_input_tokens": 1,
                "total_cache_read_tokens": 2,
                "total_cache_write_tokens": 3,
                "total_input_tokens": 6,
                "cache_hit_rate": 0.5,
                "total_output_tokens": 4,
            },
        ),
        (
            SessionUpdated("web:chat", {"key": "web:chat", "title": "title"}),
            {
                "type": "session.updated",
                "session": {"key": "web:chat", "title": "title"},
            },
        ),
        (
            TurnQueued("web:chat", "request-1", 2),
            {
                "type": "turn.queued",
                "request_id": "request-1",
                "session_id": "web:chat",
                "position": 2,
            },
        ),
        (
            ToolCallStarted("web:chat", "turn-1", "call-1", "read_file", {"path": "a"}),
            {
                "type": "react.tool.started",
                "session_id": "web:chat",
                "turn_id": "turn-1",
                "call_id": "call-1",
                "tool_name": "read_file",
                "arguments": {"path": "a"},
            },
        ),
        (
            ToolCallCompleted("web:chat", "turn-1", "call-1", "read_file", "ok", "done"),
            {
                "type": "react.tool.completed",
                "session_id": "web:chat",
                "turn_id": "turn-1",
                "call_id": "call-1",
                "tool_name": "read_file",
                "status": "ok",
                "result_preview": "done",
            },
        ),
    ],
    ids=[
        "turn-started",
        "compaction-started",
        "compaction-completed",
        "compaction-failed",
        "session-usage",
        "session-updated",
        "turn-queued",
        "tool-started",
        "tool-completed",
    ],
)
def test_lifecycle_event_payloads_are_preserved(event: object, expected: dict) -> None:
    mapped = WebEventMapper().map_event(event)  # type: ignore[arg-type]

    assert mapped.session_key == "web:chat"
    assert mapped.payloads == (expected,)


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        ("queue_full", "当前任务较多，请稍后再试"),
        ("session_busy", "当前会话正在处理消息"),
        ("closed", "服务正在关闭，请稍后再试"),
        ("unknown", "消息暂时无法处理"),
    ],
)
def test_queue_rejection_keeps_stable_chinese_message(reason: str, message: str) -> None:
    mapped = WebEventMapper().map_event(
        TurnQueueRejected("web:chat", "request-1", reason)
    )

    assert mapped.payloads == ({
        "type": "error",
        "request_id": "request-1",
        "session_id": "web:chat",
        "code": reason,
        "message": message,
    },)


def test_stream_content_precedes_thinking_delta() -> None:
    mapped = WebEventMapper().map_event(
        StreamDeltaReady("web:chat", "turn-1", "answer", "reason")
    )

    assert [payload["type"] for payload in mapped.payloads] == [
        "answer.delta",
        "react.thinking.delta",
    ]
    assert [payload["delta"] for payload in mapped.payloads] == ["answer", "reason"]


def test_context_usage_event_only_includes_present_optional_fields() -> None:
    mapped = WebEventMapper().map_event(ContextUsageUpdated(
        session_key="web:chat",
        turn_id="turn-1",
        used_tokens=50,
        context_window=100,
        soft_limit_tokens=80,
        hard_input_tokens=95,
        context_window_source="catalog",
        estimate_source="provider",
        breakdown={"conversation_tokens": 50},
        sections=({"name": "conversation"},),
        pressure_tokens=50,
        model="model",
    ))

    payload = mapped.payloads[0]
    assert payload["pressure_tokens"] == 50
    assert payload["model"] == "model"
    assert "projected_tokens" not in payload
    assert "as_of_seq" not in payload
    assert payload["sections"] == [{"name": "conversation"}]


def test_context_usage_snapshot_preserves_projection_rules() -> None:
    mapped = WebEventMapper().map_context_usage_snapshot("web:chat", {
        "pressure_tokens": 100,
        "projected_tokens": 120,
        "context_window": 1000,
        "soft_limit_tokens": 800,
        "hard_input_tokens": 950,
        "context_window_source": "catalog",
        "system_tokens": 10,
        "tools_tokens": 20,
        "message_tokens": 70,
        "model": "model",
    })

    payload = mapped.payloads[0]
    assert payload["used_tokens"] == 120
    assert payload["estimate_source"] == "provider_projected"
    assert payload["breakdown"] == {
        "system_prompt_tokens": 10,
        "tools_tokens": 20,
        "conversation_tokens": 70,
        "overhead_tokens": 0,
    }
    assert payload["pressure_tokens"] == 100
    assert payload["projected_tokens"] == 120
    assert "surface_tokens" not in payload


def test_snapshot_and_direct_response_helpers_keep_protocol_shapes() -> None:
    mapper = WebEventMapper()

    reset = mapper.map_context_usage_reset("web:chat")
    usage = mapper.map_session_usage_snapshot("web:chat", {
        "total_uncached_input_tokens": -1,
        "total_cache_read_tokens": 2,
        "total_cache_write_tokens": 3,
        "total_input_tokens": 4,
        "cache_hit_rate": None,
        "total_output_tokens": 5,
    })
    snapshot = mapper.map_active_turn_snapshot(
        "web:chat", {"session_id": "web:chat", "turn_id": "turn-1"}
    )
    interrupted = mapper.map_interrupted(
        "web:chat",
        "request-1",
        InterruptResult("interrupted", "web:chat", "turn-1", 10, "ended"),
    )

    assert reset.payloads == ({"type": "context.usage.reset", "session_id": "web:chat"},)
    assert usage.payloads[0]["total_uncached_input_tokens"] == 0
    assert usage.payloads[0]["cache_hit_rate"] is None
    assert snapshot.payloads == ({
        "type": "turn.snapshot",
        "session_id": "web:chat",
        "turn_id": "turn-1",
    },)
    assert interrupted.payloads == ({
        "type": "turn.interrupted",
        "request_id": "request-1",
        "session_id": "web:chat",
        "turn_id": "turn-1",
        "status": "interrupted",
        "duration_ms": 10,
        "ended_at": "ended",
    },)


def test_pending_notification_maps_as_final_message() -> None:
    mapped = WebEventMapper().map_pending_notification(
        "web:chat",
        content="reminder",
        message_id="notification-1",
        metadata={"notification_id": "notification-1"},
    )

    assert mapped.payloads == ({
        "type": "message.final",
        "request_id": "",
        "session_id": "web:chat",
        "turn_id": "",
        "content": "reminder",
        "thinking": "",
        "media": [],
        "message_id": "notification-1",
        "metadata": {"notification_id": "notification-1"},
    },)
