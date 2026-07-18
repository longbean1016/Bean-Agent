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


class Skills:
    def build_skills_summary(self) -> str:
        return '<skills><skill name="review"><description>代码审查</description></skill></skills>'

    def get_always_skills(self) -> list[str]:
        return ["base"]

    def load_skills_for_context(self, names: list[str]) -> str:
        bodies = {"base": "始终遵循基础流程", "review": "检查行为回归"}
        return "\n".join(bodies[name] for name in names if name in bodies)


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
    assert envelope[-1]["content"] == (
        "[当前消息时间: 2026-07-16T09:02:03+08:00]\n"
        "[当前时区: Asia/Shanghai]\n"
        "[语言要求: 默认用简体中文完成最终回复和可见思考过程；用户明确要求其他语言时遵循用户要求。]\n"
        "当前问题"
    )


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


def test_skill_catalog_is_stable_while_active_skill_body_stays_in_frame() -> None:
    assembler = PromptAssembler(
        SystemPromptBuilder(default_prompt_blocks(), cache=SectionCache()),
        MessageEnvelopeBuilder(),
    )
    base = TurnContext(
        workspace="D:/workspace",
        channel="web",
        chat_id="c",
        skills=Skills(),
        active_skill_names=[],
    )
    active = TurnContext(
        workspace="D:/workspace",
        channel="web",
        chat_id="c",
        skills=Skills(),
        active_skill_names=["review"],
    )

    first = assembler.assemble(
        turn_ctx=base,
        history=[],
        current_message="普通问题",
    )
    second = assembler.assemble(
        turn_ctx=active,
        history=[],
        current_message="$review 检查代码",
    )

    assert first.messages[0]["content"] == second.messages[0]["content"]
    assert "代码审查" in str(first.messages[0]["content"])
    assert "始终遵循基础流程" not in str(first.messages[0]["content"])
    assert "始终遵循基础流程" in str(first.messages[-2]["content"])
    assert "检查行为回归" not in str(first.messages[-2]["content"])
    assert "检查行为回归" in str(second.messages[-2]["content"])
