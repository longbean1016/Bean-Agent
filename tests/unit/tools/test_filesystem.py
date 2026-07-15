"""文件系统工具的路径安全、读取、写入和精确编辑测试。"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from tools.base import ToolResult
from tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool


def test_filesystem_tool_descriptions_and_schemas_match_reference_contract(
    tmp_path: Path,
) -> None:
    read = ReadFileTool(tmp_path)
    write = WriteFileTool(tmp_path)
    edit = EditFileTool(tmp_path)
    list_dir = ListDirTool(tmp_path)

    assert "默认受 400 行和 10KB 双重上限保护" in read.description
    assert "offset=跳过的行数（0-based）" in read.description
    assert read.parameters["properties"]["offset"]["description"] == (
        "起始行号（0-based），默认 0"
    )
    assert read.parameters["properties"]["limit"]["description"] == (
        "最多读取行数，默认不限（受 80K 字符上限约束）"
    )

    assert "优先使用 edit_file 修改已有文件" in write.description
    assert write.parameters["properties"]["content"]["description"] == (
        "要写入的文本内容"
    )

    assert "不包含 read_file 输出的行号前缀" in edit.description
    assert set(edit.parameters["properties"]) == {
        "path",
        "old_text",
        "new_text",
        "replace_all",
    }
    assert "收到'出现N次'警告后再决定" in edit.parameters["properties"][
        "replace_all"
    ]["description"]

    assert list_dir.description == "列举指定目录下的文件和子目录。"
    assert list_dir.parameters == {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要列举的目录路径"}
        },
        "required": ["path"],
    }


@pytest.mark.asyncio
async def test_read_file_uses_zero_based_offset_and_numbered_output(
    tmp_path: Path,
) -> None:
    (tmp_path / "sample.txt").write_bytes("第一行\n第二行\n第三行\n".encode("utf-8"))
    tool = ReadFileTool(tmp_path)

    result = await tool.execute("sample.txt", offset=1, limit=1)

    assert result == (
        "     2→第二行\n"
        "\n\n[第 2–2 行 / 共 3 行 / 30 字节]"
    )


@pytest.mark.asyncio
async def test_read_file_rejects_escape_directory_and_binary(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02")
    tool = ReadFileTool(tmp_path)

    escaped = await tool.execute("../outside.txt")
    binary = await tool.execute("binary.bin")

    assert "超出允许目录" in escaped
    assert "看起来是二进制文件" in binary


@pytest.mark.asyncio
async def test_read_file_returns_image_content_block(tmp_path: Path) -> None:
    raw = b"\x89PNG\r\n\x1a\nsmall-image"
    (tmp_path / "image.png").write_bytes(raw)
    tool = ReadFileTool(tmp_path, multimodal=True)

    result = await tool.execute("image.png")

    assert isinstance(result, ToolResult)
    assert "已读取图片文件 image.png" in result.text
    assert result.content_blocks == [
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(raw).decode(),
                "detail": "high",
            },
        }
    ]


@pytest.mark.asyncio
async def test_read_file_does_not_return_partial_oversized_first_line(
    tmp_path: Path,
) -> None:
    (tmp_path / "large.txt").write_text("x" * 10_001 + "\n", encoding="utf-8")

    result = await ReadFileTool(tmp_path).execute("large.txt")

    assert isinstance(result, str)
    assert result.startswith("\n\n[已截断：首行超过 10KB")


@pytest.mark.asyncio
async def test_write_file_creates_parent_and_rejects_directory(tmp_path: Path) -> None:
    tool = WriteFileTool(tmp_path)

    written = await tool.execute("nested/file.txt", "你好")
    rejected = await tool.execute("nested", "不能覆盖目录")

    assert written == "已写入 2 字节到 nested/file.txt"
    assert (tmp_path / "nested/file.txt").read_text(encoding="utf-8") == "你好"
    assert rejected == "写入文件失败：目标路径是目录：nested"


@pytest.mark.asyncio
async def test_edit_file_warns_on_multiple_matches_and_supports_replace_all(
    tmp_path: Path,
) -> None:
    path = tmp_path / "repeat.txt"
    path.write_text("old\nold\n", encoding="utf-8")
    tool = EditFileTool(tmp_path)

    warning = await tool.execute("repeat.txt", "old", "new")
    edited = await tool.execute("repeat.txt", "old", "new", replace_all=True)

    assert warning.startswith("警告：old_text 在文件中出现了 2 次")
    assert "已成功编辑 repeat.txt（替换 2 处）" in edited
    assert "```diff" in edited
    assert path.read_text(encoding="utf-8") == "new\nnew\n"


@pytest.mark.asyncio
async def test_edit_file_preserves_utf8_bom_and_crlf(tmp_path: Path) -> None:
    path = tmp_path / "windows.txt"
    path.write_bytes("\ufefffirst\r\nsecond\r\n".encode("utf-8"))

    result = await EditFileTool(tmp_path).execute(
        "windows.txt",
        "first\nsecond",
        "first\nchanged",
    )

    assert "已成功编辑" in result
    assert path.read_bytes() == "\ufefffirst\r\nchanged\r\n".encode("utf-8")


@pytest.mark.asyncio
async def test_list_dir_sorts_and_marks_entries(tmp_path: Path) -> None:
    (tmp_path / "z.txt").write_text("z", encoding="utf-8")
    (tmp_path / "a-dir").mkdir()

    result = await ListDirTool(tmp_path).execute(".")
    empty = await ListDirTool(tmp_path).execute("a-dir")

    assert result == "📁 a-dir\n📄 z.txt"
    assert empty == "目录 a-dir 为空"
