"""独立视觉模型工具、条件注册和多模态消息转换测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools import ToolRegistry
from tools.base import ToolResult
from tools.registration import register_filesystem_tools
from tools.runtime import append_tool_result
import tools.vision as vision_module
from tools.vision import ReadImageVisionTool


class _VisionProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(content="图片中有一只猫", thinking=None)


@pytest.mark.asyncio
async def test_read_image_vision_calls_independent_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "cat.png"
    image.write_bytes(b"image")
    monkeypatch.setattr(
        vision_module,
        "_encode_image_data_uri",
        lambda path: "data:image/png;base64,aW1hZ2U=",
    )
    provider = _VisionProvider()
    tool = ReadImageVisionTool(provider, "qwen-vl-max", tmp_path)  # type: ignore[arg-type]

    result = await tool.execute("cat.png", "图中有什么？")

    assert result == "图片中有一只猫"
    assert provider.calls == [
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "图中有什么？"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,aW1hZ2U=",
                                "detail": "high",
                            },
                        },
                    ],
                }
            ],
            "tools": [],
            "model": "qwen-vl-max",
            "max_tokens": 2048,
            "disable_thinking": True,
        }
    ]


def test_register_filesystem_tools_selects_visual_path(tmp_path: Path) -> None:
    provider = _VisionProvider()

    multimodal_registry = register_filesystem_tools(
        ToolRegistry(), allowed_dir=tmp_path, multimodal=True
    )
    vl_registry = register_filesystem_tools(
        ToolRegistry(),
        allowed_dir=tmp_path,
        multimodal=False,
        vl_provider=provider,  # type: ignore[arg-type]
        vl_model="qwen-vl-max",
    )
    text_registry = register_filesystem_tools(
        ToolRegistry(), allowed_dir=tmp_path, multimodal=False
    )

    assert "read_image_vision" not in multimodal_registry.get_registered_names()
    assert "read_image_vision" in vl_registry.get_registered_names()
    assert "read_image_vision" not in text_registry.get_registered_names()
    assert multimodal_registry.get_tool("read_file")._multimodal is True  # type: ignore[union-attr]
    assert vl_registry.get_tool("read_file")._vl_available is True  # type: ignore[union-attr]
    assert vl_registry.get_tool("read_file")._allowed_dir == tmp_path  # type: ignore[union-attr]
    # 对齐 Akashic：视觉工具需要读取 channel 管理的 workspace/uploads 或临时
    # 上传绝对路径，不复用普通文件工具的工作目录沙箱。
    assert vl_registry.get_tool("read_image_vision")._allowed_dir is None  # type: ignore[union-attr]
    assert text_registry.get_tool("read_file")._vl_available is False  # type: ignore[union-attr]


def test_append_tool_result_preserves_text_and_content_blocks() -> None:
    messages: list[dict[str, Any]] = []
    image_block = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
    }

    append_tool_result(
        messages,
        tool_call_id="call-1",
        tool_name="read_file",
        content=ToolResult(text="图片已读取", content_blocks=[image_block]),
    )

    assert messages == [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "图片已读取",
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "以下是工具 read_file 读取到的文件内容，请直接查看。",
                },
                image_block,
            ],
        },
    ]
