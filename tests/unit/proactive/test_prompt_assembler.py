"""主动聊天专用 Prompt 组装测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from proactive.prompt_assembler import build_proactive_messages


class _Memory:
    def read_self(self) -> str:
        return "BeanAgent 保持克制主动。"

    def get_memory_context(self) -> str:
        return "用户长期关注记忆评测。"

    def read_recent_context(self) -> str:
        return "不应读取普通 recent context"


class _Skills:
    def build_skills_summary(self) -> str:
        return '<skills><skill name="review"><description>代码审查</description></skill></skills>'

    def get_always_skills(self) -> list[str]:
        return []

    def load_skills_for_context(self, names: list[str]) -> str:
        return ""


def test_proactive_prompt_orders_stable_blocks_before_memory_and_recent_messages() -> None:
    messages = build_proactive_messages(
        workspace="D:/BeanAgent",
        session_key="web:a",
        memory=_Memory(),
        skills=_Skills(),
        now=datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc),
        recent_rows=[
            {
                "role": "user",
                "content": "最近在整理主动聊天协议",
                "timestamp": "2026-07-29T17:58:00+08:00",
                "tool_chain": [{"calls": [{"name": "shell"}]}],
                "reasoning_content": "不能进入主动 prompt",
                "metadata": {"secret": "hidden"},
            },
            {
                "role": "assistant",
                "content": "我建议先收敛终止协议",
                "timestamp": "2026-07-29T17:59:00+08:00",
                "proactive": True,
            },
        ],
    )

    system_prompt = str(messages[0]["content"])
    assert system_prompt.index("你是 BeanAgent") < system_prompt.index("## 主动聊天行为规则")
    assert system_prompt.index("## 主动聊天行为规则") < system_prompt.index("## Skill 目录")
    assert system_prompt.index("## Skill 目录") < system_prompt.index("## 主动判断终止协议")
    assert system_prompt.index("## 主动判断终止协议") < system_prompt.index("## 自我认知")
    assert system_prompt.index("## 自我认知") < system_prompt.index("## 长期记忆")
    assert "代码审查" in system_prompt
    assert "BeanAgent 保持克制主动" in system_prompt
    assert "用户长期关注记忆评测" in system_prompt
    assert "普通 recent context" not in system_prompt

    assert messages[1]["role"] == "user"
    assert "当前判断时间: 2026-07-29T18:00:00+08:00" in str(messages[1]["content"])
    assert messages[2] == {"role": "user", "content": "最近在整理主动聊天协议"}
    assert messages[3] == {"role": "assistant", "content": "[主动消息]\n我建议先收敛终止协议"}
    assert "tool_chain" not in str(messages)
    assert "reasoning_content" not in str(messages)
    assert "hidden" not in str(messages)


def test_proactive_recent_messages_keep_last_twenty_chat_rows_only() -> None:
    rows = [
        {"role": "tool", "content": "工具结果"},
        *[
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"消息 {index}"}
            for index in range(25)
        ],
    ]

    messages = build_proactive_messages(
        workspace="D:/BeanAgent",
        session_key="web:a",
        memory=None,
        skills=None,
        now=datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc),
        recent_rows=rows,
    )

    recent = messages[2:]
    assert len(recent) == 20
    assert recent[0]["content"] == "消息 5"
    assert recent[-1]["content"] == "消息 24"
    assert all(item["role"] in {"user", "assistant"} for item in recent)
