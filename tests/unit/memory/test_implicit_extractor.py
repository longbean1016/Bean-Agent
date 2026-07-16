"""Consolidation 窗口期隐式长期记忆提取契约测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from memory.implicit_extractor import ImplicitLongTermExtractor, _build_prompt


class Provider:
    async def complete(self, messages, tools=None):
        return SimpleNamespace(content="""```json
{
  "profile": [{"summary": "用户是后端开发者", "category": "personal_fact", "emotional_weight": 0}],
  "preference": [{"summary": "用户偏好中文回答", "emotional_weight": 2}],
  "procedure": [{"summary": "修改后运行测试", "tool_requirement": "shell", "steps": ["修改代码", "运行测试"], "rule_schema": {"when": "修改代码"}}]
}
```""")


@pytest.mark.asyncio
async def test_extracts_profile_preference_and_procedure_fields() -> None:
    result = await ImplicitLongTermExtractor(Provider()).extract("[user] 我是后端开发者，以后用中文并在修改后运行测试")

    assert result.profile[0]["category"] == "personal_fact"
    assert result.preference[0]["summary"] == "用户偏好中文回答"
    assert result.procedure[0]["tool_requirement"] == "shell"
    assert result.procedure[0]["steps"] == ["修改代码", "运行测试"]
    assert result.procedure[0]["rule_schema"] == {"when": "修改代码"}


@pytest.mark.asyncio
async def test_rejects_non_object_json() -> None:
    class ListProvider:
        async def complete(self, messages, tools=None):
            return SimpleNamespace(content="[]")

    with pytest.raises(ValueError, match="JSON object"):
        await ImplicitLongTermExtractor(ListProvider()).extract("[user] 普通对话")


def test_prompt_contains_full_evidence_gates_and_counterexamples() -> None:
    prompt = _build_prompt("[USER] 对话", "用户住在上海")

    assert "检查 0 — 元讨论/举例说明" in prompt
    assert "检查 A — USER 原话锚点" in prompt
    assert "检查 B — 时效性" in prompt
    assert "检查 C — 来源方向" in prompt
    assert "你还记得我什么时候开始戴 fitbit 手环的吗" in prompt
    assert "ASSISTANT 建议得再具体再合理" in prompt
    assert "那就直接写个脚本绕过去吧" in prompt
    assert "用户住在上海" in prompt
    assert '"rule_schema"' in prompt
