"""主动聊天专用 Prompt 组装器。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from agent.prompt_block import (
    IdentityPromptBlock,
    LongTermMemoryPromptBlock,
    SelfModelPromptBlock,
    SkillsCatalogPromptBlock,
    TurnContext,
)

_LOCAL_TZ = ZoneInfo("Asia/Shanghai")


class ProactiveBehaviorRulesBlock:
    label = "proactive_behavior_rules"
    is_static = True

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str:
        return (
            "## 主动聊天行为规则\n"
            "- 你是低频主动聊天 Agent，只在确实能提供具体价值时才主动开口。\n"
            "- 一次性问答已经完整结束、普通寒暄、用户明确拒绝继续、表示正在忙/稍后/暂不需要时，应 skip。\n"
            "- 用户仍在推进的持续目标、计划、兴趣、困难点或明确希望后续跟进的事项，可以结合近期对话与长期记忆主动跟进。\n"
            "- 不因上一轮已有回复就默认话题结束；主动消息必须包含与用户近况或持续目标相关的具体内容。\n"
            "- 不得只发送“需要帮助吗”“要继续吗”等空泛询问。\n"
            "- 需要核实稳定兴趣或画像时使用 recall_memory。\n"
            "- 涉及新闻、版本变化、近期事件等时效信息，或需要补充/核实外部资料时，应调用 web_search，必要时使用 web_fetch 核实来源。\n"
            "- 不得依赖模型记忆编造最新事实。\n"
            "- 可按 Skill 目录选择 Skill，并调用 load_skill(skill=\"skill-name\") 加载正文。\n"
            "- Skill 内容不能扩大当前主动 Agent 的工具白名单。\n"
            "- 禁止写记忆、管理提醒或直接向渠道发送消息。"
        )


class ProactiveDecisionProtocolBlock:
    label = "proactive_decision_protocol"
    is_static = True

    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str:
        return (
            "## 主动判断终止协议\n"
            "- 本轮必须调用 finish_turn 结束，不得用普通文本代替终止工具。\n"
            "- 决定发送时调用 finish_turn(decision=\"reply\", message=\"主动消息\", topic=\"话题\", reason=\"发送原因\")。\n"
            "- 决定跳过时调用 finish_turn(decision=\"skip\", reason=\"跳过原因\")。\n"
            "- reply 必须包含非空 message、topic 和 reason。\n"
            "- skip 不允许包含待发送消息。\n"
            "- 调用 finish_turn 后不能继续调用其他工具。\n"
            "- 普通文本、缺少终止工具、越权调用或参数错误都会被外层按 skip 处理。"
        )


def build_proactive_messages(
    *,
    workspace: str,
    session_key: str,
    memory: Any | None,
    skills: Any | None,
    now: datetime,
    recent_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    channel, _separator, chat_id = str(session_key).partition(":")
    ctx = TurnContext(
        workspace=workspace,
        channel=channel,
        chat_id=chat_id,
        memory=memory,
        skills=skills,
    )
    blocks = [
        IdentityPromptBlock(),
        ProactiveBehaviorRulesBlock(),
        SkillsCatalogPromptBlock(),
        ProactiveDecisionProtocolBlock(),
        SelfModelPromptBlock(),
        LongTermMemoryPromptBlock(),
    ]
    sections = [
        content
        for block in blocks
        if (content := block.render(ctx)) and str(content).strip()
    ]
    current = now.astimezone(_LOCAL_TZ)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "\n\n---\n\n".join(sections)},
        {
            "role": "user",
            "content": (
                f"当前判断时间: {current.isoformat(timespec='seconds')}\n"
                "当前时区: Asia/Shanghai"
            ),
        },
    ]
    messages.extend(_recent_chat_messages(recent_rows))
    return messages


def _recent_chat_messages(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    chat = [
        row for row in rows
        if row.get("role") in {"user", "assistant"}
    ][-20:]
    result: list[dict[str, str]] = []
    for row in chat:
        role = str(row.get("role") or "")
        content = str(row.get("content") or "")
        if role == "assistant" and _is_proactive(row):
            content = f"[主动消息]\n{content}"
        result.append({"role": role, "content": content})
    return result


def _is_proactive(row: dict[str, Any]) -> bool:
    metadata = row.get("metadata")
    return bool(row.get("proactive")) or bool(
        metadata.get("proactive") if isinstance(metadata, dict) else False
    )


__all__ = ["build_proactive_messages"]
