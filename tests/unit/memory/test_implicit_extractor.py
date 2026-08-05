"""Consolidation 窗口期隐式长期记忆提取契约测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from memory.implicit_extractor import ImplicitLongTermExtractor, _build_prompt


class Provider:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append((messages, tools, kwargs))
        return SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(
                name="submit_implicit_memory",
                arguments={
                    "profile": [{"summary": "用户是后端开发者", "category": "personal_fact", "emotional_weight": 0}],
                    "preference": [{"summary": "用户偏好中文回答", "emotional_weight": 2}],
                    "procedure": [{
                        "summary": "修改后运行测试",
                        "tool_requirement": "shell",
                        "steps": ["修改代码", "运行测试"],
                        "rule_schema": {
                            "required_tools": ["shell"],
                            "forbidden_tools": [],
                            "mentioned_tools": ["shell"],
                        },
                        "emotional_weight": 0,
                    }],
                },
            )],
        )


@pytest.mark.asyncio
async def test_extracts_profile_preference_and_procedure_fields() -> None:
    provider = Provider()
    result = await ImplicitLongTermExtractor(provider).extract("[user] 我是后端开发者，以后用中文并在修改后运行测试")

    _, tools, kwargs = provider.calls[0]
    assert tools[0]["function"]["name"] == "submit_implicit_memory"
    assert kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_implicit_memory"},
    }
    assert kwargs["disable_thinking"] is True
    assert result.profile[0]["category"] == "personal_fact"
    assert result.preference[0]["summary"] == "用户偏好中文回答"
    assert result.procedure[0]["tool_requirement"] == "shell"
    assert result.procedure[0]["steps"] == ["修改代码", "运行测试"]
    assert result.procedure[0]["rule_schema"] == {
        "required_tools": ["shell"],
        "forbidden_tools": [],
        "mentioned_tools": ["shell"],
    }


@pytest.mark.asyncio
async def test_retries_invalid_function_arguments_once_then_raises() -> None:
    class InvalidProvider:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools=None, **kwargs):
            self.calls += 1
            return SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(name="submit_implicit_memory", arguments={})],
            )

    provider = InvalidProvider()
    with pytest.raises(ValueError, match="profile"):
        await ImplicitLongTermExtractor(provider).extract("[user] 普通对话")

    assert provider.calls == 2


@pytest.mark.asyncio
async def test_retries_empty_function_response_and_uses_second_result() -> None:
    class RetryProvider(Provider):
        async def complete(self, messages, tools=None, **kwargs):
            if not self.calls:
                self.calls.append((messages, tools, kwargs))
                return SimpleNamespace(content="", tool_calls=[])
            return await super().complete(messages, tools=tools, **kwargs)

    provider = RetryProvider()
    result = await ImplicitLongTermExtractor(provider).extract("[user] 普通对话")

    assert len(provider.calls) == 2
    assert result.profile[0]["summary"] == "用户是后端开发者"


def test_prompt_contains_full_evidence_gates_and_counterexamples() -> None:
    prompt = _build_prompt("[USER] 对话", "用户住在上海")

    assert "【三类记忆共同准入规则】" in prompt
    assert "只有 USER 在当前对话中直接陈述，或明确确认的内容才允许提取" in prompt
    assert "【类型判定顺序】" in prompt
    assert "【类型判断的泛化约束】" in prompt
    assert "不是固定关键词的机械匹配" in prompt
    assert "即使句子中出现“以后”，也不能仅凭该词改判为 procedure" in prompt
    assert "稳定偏好与临时例外同时出现时，分别处理" in prompt
    assert "多个主体或多项事实时，每个主体和事实分别判断并保留关系归属" in prompt
    assert "检查 0 — 元讨论/举例说明" in prompt
    assert "检查 A — USER 原话锚点" in prompt
    assert "检查 B — 时效性" in prompt
    assert "检查 C — 来源方向" in prompt
    assert "你还记得我什么时候开始戴 fitbit 手环的吗" in prompt
    assert "ASSISTANT 建议得再具体再合理" in prompt
    assert "那就直接写个脚本绕过去吧" in prompt
    assert "用户住在上海" in prompt
    assert '"rule_schema"' in prompt


def test_prompt_rejects_unattributed_transcripts_and_quoted_material() -> None:
    prompt = _build_prompt("[USER] 用户展示聊天截图", "")

    assert "transcript、聊天截图、OCR、引用文本" in prompt
    assert "材料中的第一人称和 speaker 不自动等于当前 USER" in prompt
    assert "不得提取具体的 profile、preference 或 procedure" in prompt


def test_prompt_keeps_the_relationship_subject_in_profile() -> None:
    prompt = _build_prompt("[USER] 我姐姐长期住在杭州", "")

    assert "明确关系上下文" in prompt
    assert "用户的姐姐长期住在杭州" in prompt
    assert "不能改写为“用户长期住在杭州”" in prompt


def test_prompt_rejects_hypothetical_first_person_statements() -> None:
    prompt = _build_prompt("[USER] 假设我家里有三只猫", "")

    assert "假设、举例、类比或虚构场景" in prompt
    assert "假设我家里有三只猫" in prompt
    assert "不是真实事实披露" in prompt


def test_prompt_scores_emotion_only_after_memory_eligibility() -> None:
    prompt = _build_prompt("[USER] 我非常讨厌恐怖游戏", "")

    assert "先通过长期记忆准入规则" in prompt
    assert "强烈情绪本身不能把临时信息升级为长期记忆" in prompt
    assert "明确强烈喜欢/厌恶、明显受挫、关系张力或强烈在意" in prompt
    assert "3-9" in prompt
