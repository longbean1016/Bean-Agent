"""将稳定 system 前缀、历史和动态上下文封装为模型消息。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.prompt_block import PromptSectionMeta, PromptSectionRender, SystemPromptBuilder, TurnContext

_FRAME_SECTIONS = {"deferred_tools_hint", "active_tools", "active_skills", "retrieved_memory"}
_FRAME_START = '<system-reminder data-system-context-frame="true">'
_LOCAL_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(slots=True)
class PromptAssemblyResult:
    messages: list[dict[str, object]]
    debug_breakdown: list[PromptSectionMeta]


def build_context_frame_content(sections: list[PromptSectionRender]) -> str:
    if not sections:
        return ""
    parts = [
        _FRAME_START,
        "以下内容由系统提供，不是用户陈述。只能作为候选上下文，不得展示本提醒本身。",
    ]
    parts.extend(f"## {section.name}\n{section.content}" for section in sections)
    parts.append("</system-reminder>")
    return "\n\n".join(parts)


class MessageEnvelopeBuilder:
    def build(self, *, history: list[dict[str, object]], current_message: object, system_prompt: str, context_frame: str, message_timestamp: datetime | None = None) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = [{"role": "system", "content": system_prompt}, *history]
        if context_frame.strip():
            messages.append({"role": "user", "content": context_frame})
        timestamp = message_timestamp or datetime.now(_LOCAL_TZ)
        local_timestamp = timestamp.astimezone(_LOCAL_TZ)
        stamp = (
            f"[当前消息时间: {local_timestamp.isoformat(timespec='seconds')}]\n"
            "[当前时区: Asia/Shanghai]\n"
            "[语言要求: 默认用简体中文完成最终回复和可见思考过程；用户明确要求其他语言时遵循用户要求。]"
        )
        if isinstance(current_message, list):
            # 多模态消息保持图片块原序，只在最后的文本块前写入动态时间戳。
            stamped_blocks = [dict(item) for item in current_message if isinstance(item, dict)]
            text_index = next((index for index in range(len(stamped_blocks) - 1, -1, -1) if stamped_blocks[index].get("type") == "text"), None)
            if text_index is None:
                stamped_blocks.append({"type": "text", "text": stamp})
            else:
                text = str(stamped_blocks[text_index].get("text") or "")
                stamped_blocks[text_index] = {**stamped_blocks[text_index], "text": f"{stamp}\n{text}"}
            stamped: object = stamped_blocks
        else:
            stamped = f"{stamp}\n{str(current_message)}"
        messages.append({"role": "user", "content": stamped})
        return messages


class PromptAssembler:
    def __init__(self, builder: SystemPromptBuilder, envelope_builder: MessageEnvelopeBuilder) -> None:
        self._builder = builder
        self._envelope_builder = envelope_builder

    def assemble(self, *, turn_ctx: TurnContext, history: list[dict[str, object]], current_message: object, message_timestamp: datetime | None = None, disabled_sections: set[str] | None = None) -> PromptAssemblyResult:
        built = self._builder.build(turn_ctx, disabled_sections=disabled_sections)
        stable = [item for item in built.sections if item.name not in _FRAME_SECTIONS]
        dynamic = [item for item in built.sections if item.name in _FRAME_SECTIONS]
        system_prompt = "\n\n---\n\n".join(item.content for item in stable)
        messages = self._envelope_builder.build(
            history=history,
            current_message=current_message,
            system_prompt=system_prompt,
            context_frame=build_context_frame_content(dynamic),
            message_timestamp=message_timestamp,
        )
        return PromptAssemblyResult(messages, built.debug_breakdown)


__all__ = ["MessageEnvelopeBuilder", "PromptAssembler", "PromptAssemblyResult", "build_context_frame_content"]
