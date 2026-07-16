"""Prompt 稳定前缀、动态 reminder 与消息封装测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from agent.prompt_assembler import MessageEnvelopeBuilder, PromptAssembler
from agent.prompt_block import SectionCache, SystemPromptBuilder, TurnContext, default_prompt_blocks


class Memory:
    def read_self(self) -> str: return "保持直接"
    def get_memory_context(self) -> str: return "用户是开发者"
    def read_recent_context(self) -> str:
        return "## Compression\n- 项目开发\n\n## Recent Turns\n- [user] 重复消息"


def test_static_prefix_cache_and_dynamic_context_frame_are_separated() -> None:
    builder = SystemPromptBuilder(default_prompt_blocks(), cache=SectionCache())
    assembler = PromptAssembler(builder, MessageEnvelopeBuilder())
    context = TurnContext(
        workspace="D:/workspace", channel="web", chat_id="c", memory=Memory(),
        retrieved_memory_block="用户偏好中文", tools_summary="- read_file: 读取文件",
        active_tool_names=["read_file"],
    )

    first = assembler.assemble(turn_ctx=context, history=[], current_message="你好")
    second = assembler.assemble(turn_ctx=context, history=[], current_message="继续")

    assert first.messages[0]["role"] == "system"
    assert first.messages[0]["content"] == second.messages[0]["content"]
    assert {item.name for item in second.debug_breakdown if item.cache_hit} == {
        "identity", "behavior_rules", "tools_catalog"
    }
    reminder = first.messages[-2]["content"]
    assert reminder.startswith('<system-reminder data-system-context-frame="true">')
    assert "用户偏好中文" in reminder
    assert "项目开发" in reminder
    assert "重复消息" not in reminder


def test_envelope_order_is_system_history_reminder_current_message() -> None:
    envelope = MessageEnvelopeBuilder().build(
        history=[{"role": "assistant", "content": "旧回答"}],
        current_message="当前问题",
        system_prompt="稳定系统提示",
        context_frame="<system-reminder>动态上下文</system-reminder>",
        message_timestamp=datetime(2026, 7, 16, 1, 2, 3, tzinfo=timezone.utc),
    )

    assert [item["role"] for item in envelope] == ["system", "assistant", "user", "user"]
    assert envelope[-1]["content"] == "[当前消息时间: 2026-07-16T01:02:03Z]\n当前问题"


def test_stable_behavior_rules_request_chinese_answer_and_reasoning() -> None:
    assembler = PromptAssembler(
        SystemPromptBuilder(default_prompt_blocks(), cache=SectionCache()),
        MessageEnvelopeBuilder(),
    )
    result = assembler.assemble(
        turn_ctx=TurnContext(workspace="D:/workspace", channel="web", chat_id="c"),
        history=[],
        current_message="请分析问题",
    )

    system_prompt = str(result.messages[0]["content"])
    assert "最终回复与思考过程使用简体中文" in system_prompt
