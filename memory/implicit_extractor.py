"""从 Consolidation 对话窗口提取稳定的隐式长期记忆。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class ImplicitMemoryDraft:
    profile: list[dict[str, object]] = field(default_factory=list)
    preference: list[dict[str, object]] = field(default_factory=list)
    procedure: list[dict[str, object]] = field(default_factory=list)


class ProviderApi(Protocol):
    async def complete(self, messages: list[dict[str, str]], tools: list[dict[str, Any]] | None = None) -> Any: ...


class ImplicitLongTermExtractor:
    """对齐参考实现，只在较长 Consolidation 窗口中提取新长期事实。"""

    def __init__(self, provider: ProviderApi) -> None:
        self._provider = provider

    async def extract(self, conversation: str, existing_profile: str = "") -> ImplicitMemoryDraft:
        response = await self._provider.complete(
            [{"role": "user", "content": _build_prompt(conversation, existing_profile)}],
            tools=[],
        )
        text = str(getattr(response, "content", response) or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("隐式长期记忆提取必须返回 JSON object")
        return ImplicitMemoryDraft(
            profile=_items(data.get("profile")),
            preference=_items(data.get("preference")),
            procedure=_items(data.get("procedure")),
        )


def _items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict) and str(item.get("summary") or "").strip()]


def _build_prompt(conversation: str, existing_profile: str) -> str:
    # 这里刻意强调 USER 证据锚点：assistant 的总结、猜测和示例只能提供上下文，
    # 不能反向生成用户画像或长期规则，避免将模型自己的话写成用户事实。
    return f"""你是长期记忆提取专家。从对话窗口提取 profile、preference、procedure，返回 JSON object。
默认三个数组都为空；宁可遗漏，也不要把临时信息写入长期记忆。

判断标准：这条信息放到六个月后的新对话中仍有用，才允许输出。
- profile：USER 直接陈述的身份、持有物、爱好、长期状态或重要决定；category 只能是 personal_fact、purchase、decision、status。
- preference：USER 明确表达、跨会话稳定成立的服务或表达偏好。
- procedure：USER 对 agent 明确提出的、未来类似任务可复用的执行规则，可包含 tool_requirement、steps、rule_schema。

禁止提取：assistant 提供的例子或推测、用户提问和反问、当前任务的临时要求、具体时间事件。
每项可包含 emotional_weight（0-10）；不确定时为 0。只输出 JSON，不要解释。

已有画像：
{existing_profile}

对话窗口：
{conversation}

输出格式：
{{"profile": [], "preference": [], "procedure": []}}"""


__all__ = ["ImplicitLongTermExtractor", "ImplicitMemoryDraft"]
