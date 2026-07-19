"""按模型能力注册文件与视觉工具。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from memory.contracts import MemoryRetrievalApi, MemoryToolProfile, MemoryWriteApi
from session.store import SessionStore
from tools.forget_memory import ForgetMemoryTool
from tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from tools.memorize import MemorizeTool
from tools.message_lookup import FetchMessagesTool, SearchMessagesTool
from tools.recall_memory import RecallMemoryTool
from tools.registry import ToolRegistry
from tools.tool_search import ToolSearchTool
from tools.shell import ShellTool
from tools.vision import ReadImageVisionTool
from tools.web_fetch import AsyncHttpClient as FetchHttpClient, WebFetchTool
from tools.web_search import AsyncHttpClient as SearchHttpClient, WebSearchTool

if TYPE_CHECKING:
    from agent.provider import LLMProvider
    from agent.skills import SkillsLoader


class MemoryToolsApi(MemoryRetrievalApi, MemoryWriteApi, Protocol):
    """统一注册只依赖记忆引擎的查询、写入和能力声明。"""

    def tool_profile(self) -> MemoryToolProfile: ...


def register_filesystem_tools(
    registry: ToolRegistry,
    *,
    allowed_dir: Path | None,
    multimodal: bool,
    vl_provider: LLMProvider | None = None,
    vl_model: str = "",
) -> ToolRegistry:
    """注册文件工具，并按主模型能力决定是否增加独立视觉工具。"""

    vl_available = not multimodal and vl_provider is not None and bool(vl_model)
    registry.register(
        ReadFileTool(
            # 读取允许使用上传文件的绝对路径；写入、编辑和目录枚举仍受 workdir 约束。
            allowed_dir=None,
            multimodal=multimodal,
            vl_available=vl_available,
        )
    )
    registry.register(WriteFileTool(allowed_dir))
    registry.register(EditFileTool(allowed_dir))
    registry.register(ListDirTool(allowed_dir))
    if vl_available:
        # Channel 上传可能落在 workspace/uploads 或系统临时目录，
        # 它们不一定属于 agent.workdir。视觉工具因此不继承普通文件工具的路径根
        # 限制；Read/Write/Edit/List 仍严格限制在 allowed_dir 内。
        registry.register(ReadImageVisionTool(vl_provider, vl_model, None))
    return registry


def register_all(
    registry: ToolRegistry,
    *,
    allowed_dir: Path | None,
    multimodal: bool,
    allow_shell_network: bool = True,
    vl_provider: LLMProvider | None = None,
    vl_model: str = "",
    search_client: SearchHttpClient | None = None,
    fetch_client: FetchHttpClient | None = None,
    session_store: SessionStore | None = None,
    memory_engine: MemoryToolsApi | None = None,
    skills: SkillsLoader | None = None,
) -> ToolRegistry:
    """注册当前依赖实际支持的全部内置工具。"""

    registry.register(ToolSearchTool(registry), always_on=True)
    registry.register(
        ShellTool(
            allow_network=allow_shell_network,
            working_dir=allowed_dir,
            restricted_dir=allowed_dir,
        )
    )
    register_filesystem_tools(
        registry,
        allowed_dir=allowed_dir,
        multimodal=multimodal,
        vl_provider=vl_provider,
        vl_model=vl_model,
    )
    registry.register(WebSearchTool(client=search_client))
    registry.register(WebFetchTool(client=fetch_client))

    if skills is not None:
        # Skill 目录由 Prompt 和加载工具共享同一读取边界，避免两套路径规则产生差异。
        from tools.load_skill import LoadSkillTool

        registry.register(LoadSkillTool(skills))

    if session_store is not None:
        # 历史工具直接依赖可用 Store；不注册“暂不可用”的占位 Schema。
        registry.register(FetchMessagesTool(session_store))
        registry.register(SearchMessagesTool(session_store))

    if memory_engine is not None:
        # Engine profile 是单一能力来源，允许只启用读、只启用写等组合。
        profile = memory_engine.tool_profile()
        if profile.recall is not None:
            registry.register(RecallMemoryTool(memory_engine, profile.recall))
        if profile.memorize is not None:
            registry.register(MemorizeTool(memory_engine, profile.memorize))
        if profile.forget is not None:
            registry.register(ForgetMemoryTool(memory_engine, profile.forget))
    return registry


__all__ = ["register_all", "register_filesystem_tools"]
