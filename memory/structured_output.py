"""通过强制 Function Call 获取可校验的记忆提取结果。"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class ProviderApi(Protocol):
    async def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tool_choice: str | dict[str, Any] = "auto",
        disable_thinking: bool = False,
    ) -> Any: ...


class StructuredOutputError(ValueError):
    """模型没有按指定 Function 协议提交结构化结果。"""


async def complete_forced_function(
    provider: ProviderApi,
    prompt: str,
    tool: dict[str, Any],
    *,
    required_arrays: tuple[str, ...],
) -> dict[str, Any]:
    """关闭后台 thinking 并强制调用唯一 Function；格式失败时补救一次。"""

    function = tool.get("function")
    if not isinstance(function, dict) or not str(function.get("name") or "").strip():
        raise ValueError("结构化提取 Function 缺少名称")
    function_name = str(function["name"])
    tool_choice = {
        "type": "function",
        "function": {"name": function_name},
    }

    last_error: ValueError | None = None
    for attempt in range(2):
        attempt_prompt = prompt
        if attempt:
            # 只纠正输出协议，不改变原 Prompt 的记忆判断规则和正反例。
            attempt_prompt += (
                f"\n\n上一次没有按协议提交结果。完成判断后必须且只能调用 "
                f"{function_name}；没有可提取内容时也必须传入所有必需的空数组。"
            )
        try:
            response = await provider.complete(
                [{"role": "user", "content": attempt_prompt}],
                tools=[tool],
                tool_choice=tool_choice,
                # DeepSeek thinking 不支持命名 tool_choice；记忆提取属于后台
                # 分类与总结任务，单次关闭推理以获得硬格式约束并减少等待成本。
                disable_thinking=True,
            )
            arguments = _function_arguments(response, function_name)
            _require_array_fields(arguments, required_arrays)
            return arguments
        except ValueError as error:
            last_error = error
            if attempt == 0:
                logger.warning(
                    "记忆结构化提取格式无效，将补救一次: function=%s error=%s",
                    function_name,
                    type(error).__name__,
                )

    assert last_error is not None
    raise StructuredOutputError(
        f"{function_name} 结构化结果无效: {last_error}"
    ) from last_error


def _function_arguments(response: Any, function_name: str) -> dict[str, Any]:
    calls = getattr(response, "tool_calls", None)
    if not isinstance(calls, list) or len(calls) != 1:
        raise StructuredOutputError(
            f"{function_name} 必须返回且只返回一个 tool_call"
        )
    call = calls[0]
    name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
    if str(name or "") != function_name:
        raise StructuredOutputError(
            f"期望调用 {function_name}，实际调用 {str(name or 'unknown')}"
        )
    arguments = (
        call.get("arguments")
        if isinstance(call, dict)
        else getattr(call, "arguments", None)
    )
    if not isinstance(arguments, dict):
        raise StructuredOutputError(f"{function_name} arguments 必须是 JSON object")
    return dict(arguments)


def _require_array_fields(arguments: dict[str, Any], fields: tuple[str, ...]) -> None:
    for field in fields:
        if field not in arguments:
            raise StructuredOutputError(f"{field} 是必需参数")
        if not isinstance(arguments[field], list):
            raise StructuredOutputError(f"{field} 必须是数组")


CONSOLIDATION_EVENTS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_consolidation_events",
        "description": (
            "提交当前归档窗口提取出的事件摘要和 PENDING.md 长期候选。"
            "完成分析后必须且只能调用一次；没有结果时传空数组。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "history_entries": {
                    "type": "array",
                    "description": "按独立主题拆分的用户事件；不得把助手建议或推断写成用户事件。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "summary": {
                                "type": "string",
                                "description": "以 [YYYY-MM-DD HH:MM] 开头的第三人称事件摘要。",
                            },
                            "emotional_weight": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 10,
                                "description": "用户明确情绪的重要程度；普通事件填 0。",
                            },
                        },
                        "required": ["summary", "emotional_weight"],
                        "additionalProperties": False,
                    },
                },
                "pending_items": {
                    "type": "array",
                    "description": "准备进入 PENDING.md 的长期候选，不包含短期状态和 Agent 执行规则。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tag": {
                                "type": "string",
                                "enum": [
                                    "identity",
                                    "preference",
                                    "key_info",
                                    "health_long_term",
                                    "requested_memory",
                                    "correction",
                                    "agent_context",
                                ],
                                "description": "候选所属的长期记忆类别。",
                            },
                            "content": {
                                "type": "string",
                                "description": "脱离当前对话仍能成立的长期候选内容。",
                            },
                        },
                        "required": ["tag", "content"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["history_entries", "pending_items"],
            "additionalProperties": False,
        },
    },
}


RECENT_CONTEXT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_recent_context",
        "description": (
            "提交当前会话的近期语境压缩结果。完成分析后必须且只能调用一次；"
            "每个类别没有内容时传空数组。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "active_topics": {"type": "array", "items": {"type": "string"}, "description": "用户最近持续关注的话题，最多 3 条。"},
                "user_preferences": {"type": "array", "items": {"type": "string"}, "description": "用户近期明确表达的偏好或要求，最多 3 条。"},
                "follow_ups": {"type": "array", "items": {"type": "string"}, "description": "后续适合自然续接的话题，最多 3 条。"},
                "avoidances": {"type": "array", "items": {"type": "string"}, "description": "用户明确要求避免或不想讨论的方向，最多 3 条。"},
                "dormant_threads": {"type": "array", "items": {"type": "string"}, "description": "已离开主线但仍可能被回头追问的话题，最多 5 条。"},
                "ongoing_threads": {"type": "array", "items": {"type": "string"}, "description": "对用户现实生活仍有持续影响的重要线索，最多 3 条。"},
            },
            "required": [
                "active_topics",
                "user_preferences",
                "follow_ups",
                "avoidances",
                "dormant_threads",
                "ongoing_threads",
            ],
            "additionalProperties": False,
        },
    },
}


IMPLICIT_MEMORY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_implicit_memory",
        "description": (
            "提交当前对话窗口中符合证据要求的 profile、preference 和 procedure 长期记忆。"
            "完成分析后必须且只能调用一次；没有结果时传空数组。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "profile": {
                    "type": "array",
                    "description": "用户直接陈述且跨会话稳定成立的个人事实。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string", "description": "只包含 USER 原话可支持的一项完整事实。"},
                            "category": {"type": "string", "enum": ["personal_fact", "purchase", "decision", "status"]},
                            "happened_at": {"type": ["string", "null"], "description": "事实明确发生时间；不适用时为 null。"},
                            "emotional_weight": {"type": "integer", "minimum": 0, "maximum": 10},
                        },
                        "required": ["summary", "category", "happened_at", "emotional_weight"],
                        "additionalProperties": False,
                    },
                },
                "preference": {
                    "type": "array",
                    "description": "用户明确表达且跨会话稳定的服务、讲解或推荐偏好。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string", "description": "只包含 USER 明确表达的稳定偏好。"},
                            "emotional_weight": {"type": "integer", "minimum": 0, "maximum": 10},
                        },
                        "required": ["summary", "emotional_weight"],
                        "additionalProperties": False,
                    },
                },
                "procedure": {
                    "type": "array",
                    "description": "用户明确要求 Agent 在未来同类场景长期遵守的执行规则。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string", "description": "可独立理解的长期执行规则。"},
                            "scenario": {"type": "string", "description": "该流程适用的稳定场景。"},
                            "emotional_weight": {"type": "integer", "minimum": 0, "maximum": 10},
                            "tool_requirement": {"type": ["string", "null"], "description": "明确要求使用的工具；没有时为 null。"},
                            "steps": {"type": "array", "items": {"type": "string"}, "description": "用户明确要求的执行步骤。"},
                            "constraints": {"type": "array", "items": {"type": "string"}, "description": "明确的必须、禁止或顺序约束。"},
                            "rule_schema": {
                                "type": "object",
                                "properties": {
                                    "required_tools": {"type": "array", "items": {"type": "string"}},
                                    "forbidden_tools": {"type": "array", "items": {"type": "string"}},
                                    "mentioned_tools": {"type": "array", "items": {"type": "string"}},
                                },
                                "required": ["required_tools", "forbidden_tools", "mentioned_tools"],
                                "additionalProperties": False,
                            },
                        },
                        "required": ["summary", "scenario", "emotional_weight", "tool_requirement", "steps", "constraints", "rule_schema"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["profile", "preference", "procedure"],
            "additionalProperties": False,
        },
    },
}


__all__ = [
    "CONSOLIDATION_EVENTS_TOOL",
    "IMPLICIT_MEMORY_TOOL",
    "RECENT_CONTEXT_TOOL",
    "StructuredOutputError",
    "complete_forced_function",
]
