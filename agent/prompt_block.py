"""可组合 Prompt 区块、静态缓存与稳定 system 前缀构建。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class MemoryPromptApi(Protocol):
    def read_self(self) -> str: ...
    def get_memory_context(self) -> str: ...
    def read_checkpoint_summary(self, session_key: str = "") -> str: ...


class SkillsPromptApi(Protocol):
    """Prompt 组装只依赖 Skill 目录与正文读取，不感知磁盘扫描实现。"""

    def build_skills_summary(self) -> str: ...
    def get_always_skills(self) -> list[str]: ...
    def load_skills_for_context(self, names: list[str]) -> str: ...


@dataclass(slots=True)
class TurnContext:
    workspace: str
    channel: str
    chat_id: str
    memory: MemoryPromptApi | None = None
    retrieved_memory_block: str = ""
    checkpoint_summary: str = ""
    active_tool_names: list[str] = field(default_factory=list)
    skills: SkillsPromptApi | None = None
    active_skill_names: list[str] = field(default_factory=list)
    deferred_tools_hint: str = ""


class PromptBlock(Protocol):
    priority: int
    label: str
    is_static: bool
    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None: ...
    def cache_signature(self, ctx: TurnContext) -> str | None: ...


@dataclass(frozen=True, slots=True)
class PromptSectionRender:
    name: str
    content: str
    is_static: bool
    cache_hit: bool = False


@dataclass(frozen=True, slots=True)
class PromptSectionMeta:
    name: str
    chars: int
    est_tokens: int
    is_static: bool
    cache_hit: bool


@dataclass(slots=True)
class SystemPromptBuildResult:
    sections: list[PromptSectionRender]
    debug_breakdown: list[PromptSectionMeta]


class SectionCache:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str, str], str] = {}

    def get(self, scope: str, label: str, signature: str) -> str | None:
        return self._data.get((scope, label, signature))

    def set(self, scope: str, label: str, signature: str, content: str) -> None:
        self._data[(scope, label, signature)] = content


class _Block:
    priority = 0
    label = ""
    is_static = False
    def cache_signature(self, ctx: TurnContext) -> str | None: return None


class IdentityPromptBlock(_Block):
    priority, label, is_static = 10, "identity", True
    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str:
        return "你是 BeanAgent，一个直接、高效的 AI 协作伙伴。优先给出结论，再补充必要细节。"
    def cache_signature(self, ctx: TurnContext) -> str: return ctx.workspace


class BehaviorRulesPromptBlock(_Block):
    priority, label, is_static = 15, "behavior_rules", True
    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str:
        return (
            "## 行为规则\n"
            "- 除非用户明确要求其他语言，最终回复与思考过程使用简体中文。\n"
            "- 文件和命令工具必须遵守工作目录与安全校验。\n"
            "- 只在用户明确要求或信息确有长期价值时使用记忆工具。\n"
            "- 工具失败时说明原因，不得编造执行结果。"
        )
    def cache_signature(self, ctx: TurnContext) -> str: return ctx.workspace


class SkillsCatalogPromptBlock(_Block):
    priority, label, is_static = 20, "skills_catalog", True
    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        summary = cached_signature if cached_signature is not None else (
            ctx.skills.build_skills_summary() if ctx.skills else ""
        )
        return f"## Skill 目录\n{summary}" if summary.strip() else None
    def cache_signature(self, ctx: TurnContext) -> str | None:
        return ctx.skills.build_skills_summary() or None if ctx.skills else None


class SelfModelPromptBlock(_Block):
    priority, label = 30, "self_model"
    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        content = ctx.memory.read_self().strip() if ctx.memory else ""
        return f"## 自我认知\n{content}" if content else None


class LongTermMemoryPromptBlock(_Block):
    priority, label = 35, "long_term_memory"
    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        content = ctx.memory.get_memory_context().strip() if ctx.memory else ""
        return f"## 长期记忆\n{content}" if content else None


class SessionContextPromptBlock(_Block):
    priority, label = 40, "session_context"
    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str:
        # 当前时间放在消息信封中，避免每轮改变 system 前缀并破坏供应商 Prompt Cache。
        return f"## 会话环境\n- 通道: {ctx.channel}\n- 会话 ID: {ctx.chat_id}"


class ActiveToolsPromptBlock(_Block):
    priority, label = 50, "active_tools"
    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        return f"当前活跃工具: {', '.join(ctx.active_tool_names)}" if ctx.active_tool_names else None


class DeferredToolsHintBlock(_Block):
    """注入未加载工具目录，让模型在调用 tool_search 前知道有哪些工具可用。"""

    priority, label = 48, "deferred_tools_hint"
    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        return ctx.deferred_tools_hint.strip() or None


class ActiveSkillsPromptBlock(_Block):
    priority, label = 50, "active_skills"
    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        if not ctx.skills:
            return None
        # always Skill 先于本轮显式命中，既保证基础约束稳定，也保持用户提及顺序。
        names = list(dict.fromkeys([
            *ctx.skills.get_always_skills(),
            *ctx.active_skill_names,
        ]))
        content = ctx.skills.load_skills_for_context(names)
        return f"# Active Skills\n\n{content}" if content else None


class RetrievedMemoryPromptBlock(_Block):
    priority, label = 55, "retrieved_memory"
    def render(self, ctx: TurnContext, cached_signature: str | None = None) -> str | None:
        content = ctx.retrieved_memory_block.strip()
        return f"## 检索到的相关记忆\n{content}" if content else None


class SystemPromptBuilder:
    def __init__(self, blocks: list[PromptBlock], cache: SectionCache | None = None) -> None:
        self._blocks = sorted(blocks, key=lambda block: block.priority)
        self._cache = cache or SectionCache()

    def build(self, ctx: TurnContext, *, disabled_sections: set[str] | None = None) -> SystemPromptBuildResult:
        disabled = disabled_sections or set()
        sections: list[PromptSectionRender] = []
        metadata: list[PromptSectionMeta] = []
        for block in self._blocks:
            if block.label in disabled:
                continue
            signature = block.cache_signature(ctx) if block.is_static else None
            content = self._cache.get(ctx.workspace, block.label, signature) if signature else None
            cache_hit = content is not None
            if content is None:
                content = block.render(ctx, cached_signature=signature)
                if content and signature:
                    self._cache.set(ctx.workspace, block.label, signature, content)
            if not content:
                continue
            sections.append(PromptSectionRender(block.label, content, block.is_static, cache_hit))
            metadata.append(PromptSectionMeta(block.label, len(content), max(1, len(content) // 3), block.is_static, cache_hit))
        return SystemPromptBuildResult(sections, metadata)


def default_prompt_blocks() -> list[PromptBlock]:
    return [IdentityPromptBlock(), BehaviorRulesPromptBlock(), SkillsCatalogPromptBlock(), SelfModelPromptBlock(), LongTermMemoryPromptBlock(), SessionContextPromptBlock(), DeferredToolsHintBlock(), ActiveToolsPromptBlock(), ActiveSkillsPromptBlock(), RetrievedMemoryPromptBlock()]


__all__ = ["DeferredToolsHintBlock", "PromptSectionMeta", "PromptSectionRender", "SectionCache", "SystemPromptBuilder", "TurnContext", "default_prompt_blocks"]
