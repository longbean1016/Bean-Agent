"""工具调用展示投影的脱敏和模型兼容边界测试。"""

from __future__ import annotations

from agent.tool_projection import (
    project_result_preview,
    project_tool_arguments,
    project_tool_call,
    project_tool_chain,
)


def test_write_and_edit_arguments_never_copy_file_body() -> None:
    write = project_tool_arguments(
        "write_file",
        {"path": "D:/workspace/a.txt", "content": "TOP_SECRET body"},
    )
    edit = project_tool_arguments(
        "edit_file",
        {
            "path": "D:/workspace/a.txt",
            "old_text": "old private text",
            "new_text": "new private text",
        },
    )

    assert write == {"path": "D:/workspace/a.txt", "content_length": 15}
    assert edit == {
        "path": "D:/workspace/a.txt",
        "replace_all": False,
        "old_text_length": 16,
        "new_text_length": 16,
    }
    assert "TOP_SECRET" not in str(write)
    assert "private text" not in str(edit)


def test_generic_arguments_redact_string_secret_carriers() -> None:
    projected = project_tool_arguments(
        "mcp_demo__request",
        {
            "headers": "Authorization: Bearer bearer-secret",
            "cookies": "session=session-secret",
            "url": "https://example.test/?access_token=query-secret",
            "note": "password=inline-secret",
            "nested": {"api_key": "nested-secret"},
            "count": 0,
        },
    )

    assert projected["headers"] == "[已隐藏]"
    assert projected["cookies"] in {"[已隐藏]", "[已脱敏]"}
    assert "query-secret" not in str(projected["url"])
    assert "inline-secret" not in str(projected["note"])
    assert "nested-secret" not in str(projected["nested"])
    assert projected["count"] == 0


def test_result_preview_is_bounded_and_does_not_copy_structured_result() -> None:
    assert "result-secret" not in project_result_preview(
        {"token": "result-secret", "items": ["result-secret"]}
    )
    preview = project_result_preview("x" * 80, max_length=10)
    assert preview == ("x" * 10) + "…"


def test_result_preview_redacts_bearer_token_after_assignment_separator() -> None:
    preview = project_result_preview("Authorization: Bearer abc123 TOKEN=Bearer def456")

    assert "abc123" not in preview
    assert "def456" not in preview
    assert "Bearer [已脱敏]" in preview


def test_result_preview_redacts_quoted_secret_with_spaces() -> None:
    preview = project_result_preview('{"password": "secret with spaces"}')

    assert "secret with spaces" not in preview
    assert '"password": "[已脱敏]"' in preview


def test_result_preview_redacts_space_delimited_secret_carriers() -> None:
    preview = project_result_preview(
        "token token-secret apiKey api-key-secret auth auth-secret "
        "password password-secret Authorization Bearer bearer-secret"
    )

    for secret in (
        "token-secret",
        "api-key-secret",
        "auth-secret",
        "password-secret",
        "bearer-secret",
    ):
        assert secret not in preview
    assert "token [已脱敏]" in preview
    assert "apiKey [已脱敏]" in preview
    assert "auth [已脱敏]" in preview
    assert "password [已脱敏]" in preview
    assert "Authorization Bearer [已脱敏]" in preview


def test_result_preview_redacts_quoted_space_delimited_secret() -> None:
    preview = project_result_preview('password "secret with spaces" apiKey \'quoted key\'')

    assert "secret with spaces" not in preview
    assert "quoted key" not in preview


def test_result_preview_redacts_common_http_header_aliases() -> None:
    preview = project_result_preview(
        "x-api-key api-secret x-auth-token auth-secret authToken compact-secret"
    )

    for secret in ("api-secret", "auth-secret", "compact-secret"):
        assert secret not in preview


def test_result_preview_does_not_treat_natural_language_or_hyphenated_word_as_flag() -> None:
    preview = project_result_preview("token count password reset token-secret foo auth required")

    assert preview == "token count password reset token-secret foo auth required"


def test_result_preview_redacts_quoted_flag_value_as_a_whole() -> None:
    preview = project_result_preview('--token "secret with spaces" --password \'another secret\'')

    assert "secret with spaces" not in preview
    assert "another secret" not in preview
    assert preview == "--token [已脱敏] --password [已脱敏]"


def test_model_fields_are_private_compatibility_surface() -> None:
    raw_arguments = {"content": "model-only body", "token": "model-secret"}
    raw_blocks = [{"type": "image", "data": "model-only-block"}]
    call = {
        "call_id": "call-1",
        "name": "write_file",
        "arguments": raw_arguments,
        "result": "model-only-result",
        "content_blocks": raw_blocks,
        "status": "ok",
    }

    public = project_tool_call(call)
    private = project_tool_call(call, include_model_fields=True)

    assert "_model_arguments" not in public
    assert "_model_result" not in public
    assert "_model_content_blocks" not in public
    assert "content" not in public["arguments"]
    assert private["_model_arguments"] == raw_arguments
    assert private["_model_result"] == "model-only-result"
    assert private["_model_content_blocks"] == raw_blocks
    assert private["status"] == "ok"

    # 私有 surface 必须是副本，不能因调用方后续修改原始参数而改变已保存消息。
    raw_arguments["token"] = "changed"
    raw_blocks[0]["data"] = "changed"
    assert private["_model_arguments"]["token"] == "model-secret"
    assert private["_model_content_blocks"][0]["data"] == "model-only-block"


def test_tool_chain_public_projection_drops_private_model_fields_and_unknown_status() -> None:
    chain = [{
        "iteration": 2,
        "text": "model text",
        "calls": [{
            "call_id": "call-1",
            "name": "shell",
            "arguments": {"command": "echo TOKEN=secret"},
            "result": "TOKEN=secret",
            "_model_arguments": {"command": "echo TOKEN=secret"},
            "_model_result": "TOKEN=secret",
            "status": "future-status",
        }],
    }]

    projected = project_tool_chain(chain)

    assert projected[0]["calls"][0]["status"] == "unknown"
    assert "_model_arguments" not in projected[0]["calls"][0]
    assert "_model_result" not in projected[0]["calls"][0]
    assert "secret" not in str(projected)


def test_tool_call_projection_keeps_safe_approval_linkage() -> None:
    projected = project_tool_call(
        {
            "call_id": "call-approval-1",
            "name": "shell",
            "arguments": {"command": "echo hello"},
            "approval_id": "approval-1",
            "approval_state": "PENDING",
        }
    )

    assert projected["approval_id"] == "approval-1"
    assert projected["approval_state"] == "pending"


def test_tool_call_projection_drops_unknown_or_empty_approval_fields() -> None:
    projected = project_tool_call(
        {
            "call_id": "call-approval-2",
            "name": "shell",
            "arguments": {"command": "echo hello"},
            "approval_id": "   ",
            "approval_state": "future-state",
        }
    )

    assert "approval_id" not in projected
    assert "approval_state" not in projected


def test_tool_call_projection_bounds_approval_id() -> None:
    projected = project_tool_call(
        {
            "call_id": "call-approval-3",
            "name": "shell",
            "arguments": {"command": "echo hello"},
            "approval_id": "a" * 256,
            "approval_state": "allowed-once",
        }
    )

    assert projected["approval_id"] == "a" * 128
    assert projected["approval_state"] == "allowed-once"


def test_tool_call_projection_keeps_session_approval_state() -> None:
    projected = project_tool_call(
        {
            "call_id": "call-approval-session",
            "name": "shell",
            "arguments": {"command": 'del "D:\\test\\a.txt"'},
            "approval_id": "approval-session",
            "approval_state": "allowed-session",
        }
    )

    assert projected["approval_id"] == "approval-session"
    assert projected["approval_state"] == "allowed-session"
