"""按模型能力注册文件与视觉工具。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from tools.registry import ToolRegistry
from tools.vision import ReadImageVisionTool

if TYPE_CHECKING:
    from agent.provider import LLMProvider


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
            allowed_dir=allowed_dir,
            multimodal=multimodal,
            vl_available=vl_available,
        )
    )
    registry.register(WriteFileTool(allowed_dir))
    registry.register(EditFileTool(allowed_dir))
    registry.register(ListDirTool(allowed_dir))
    if vl_available:
        # 只有工具确实已注册时，ReadFileTool 才会提示模型调用它。
        registry.register(ReadImageVisionTool(vl_provider, vl_model, allowed_dir))
    return registry


__all__ = ["register_filesystem_tools"]
