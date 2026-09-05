"""文件读取、覆盖写入、精确编辑与目录列举工具。"""

from __future__ import annotations

import asyncio
import base64
import builtins
import difflib
import io
import mimetypes
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from sandbox.errors import SandboxError
from sandbox.filesystem import FilesystemMutationBroker
from sandbox.guard import SandboxGuard, resolve_tool_target
from tools.base import Tool, ToolResult

_READ_MAX_LINES = 400
_READ_MAX_BYTES = 10_000
_READ_PROBE_BYTES = 4096
_IMAGE_MAX_EDGE = 1568
_IMAGE_TARGET_B64_LEN = 8_000_000
_IMAGE_MIN_QUALITY = 45

_T = TypeVar("_T")
_FILE_MUTATION_LOCKS: dict[str, asyncio.Lock] = {}


def _is_inside(path: Path, allowed_dir: Path) -> bool:
    """使用路径组件判断包含关系，避免字符串前缀绕过目录边界。"""

    try:
        path.relative_to(allowed_dir)
    except ValueError:
        return False
    return True


def _resolve_path(path: str, allowed_dir: Path | None = None) -> Path:
    """解析用户路径；配置工作目录时，相对路径只能落在该目录内部。"""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and allowed_dir is not None:
        resolved = (allowed_dir / candidate).resolve()
    else:
        resolved = candidate.resolve()
    if allowed_dir is not None and not _is_inside(resolved, allowed_dir.resolve()):
        raise PermissionError(f"路径 {path} 超出允许目录 {allowed_dir}")
    return resolved


def _get_file_mutation_key(file_path: Path) -> str:
    """为现有和待创建文件生成稳定锁键。"""

    try:
        return str(file_path.resolve(strict=True))
    except FileNotFoundError:
        return os.path.realpath(str(file_path))


async def _run_with_file_mutation_lock(
    file_path: Path,
    operation: Callable[[], Awaitable[_T]],
) -> _T:
    """串行化同一文件的写和编辑，避免并发读改写覆盖彼此结果。"""

    key = _get_file_mutation_key(file_path)
    lock = _FILE_MUTATION_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _FILE_MUTATION_LOCKS[key] = lock
    async with lock:
        result = await operation()
    current = _FILE_MUTATION_LOCKS.get(key)
    if current is lock and not lock.locked():
        _FILE_MUTATION_LOCKS.pop(key, None)
    return result


def _strip_utf8_bom(text: str) -> tuple[str, bool]:
    return (text[1:], True) if text.startswith("\ufeff") else (text, False)


def _restore_utf8_bom(text: str, has_bom: bool) -> str:
    return "\ufeff" + text if has_bom else text


def _supports_crlf_compat(text: str) -> bool:
    """只对纯 CRLF 文件启用换行兼容，混合换行仍要求精确匹配。"""

    if "\r\n" not in text:
        return False
    without_crlf = text.replace("\r\n", "")
    return "\n" not in without_crlf and "\r" not in without_crlf


def _build_edit_diff(old_text: str, new_text: str, path: str) -> str:
    lines = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=f"{path} (before)",
        tofile=f"{path} (after)",
        lineterm="",
        n=2,
    )
    return "\n".join(lines)


def _detect_image_mime_from_header(head: bytes) -> str | None:
    """按文件头识别常见图片，不能只信任可伪造的扩展名。"""

    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _looks_binary(head: bytes) -> bool:
    if not head:
        return False
    if b"\x00" in head:
        return True
    allowed_controls = set(b"\t\n\r\f\b")
    suspicious = 0
    for byte in head:
        if byte in allowed_controls or 32 <= byte <= 126 or byte >= 128:
            continue
        suspicious += 1
    return suspicious / len(head) > 0.3


def _encode_image_for_model(
    file_path: Path,
    detected_mime: str | None = None,
) -> tuple[str, str, bool]:
    """小图直接编码；超大图片按参考实现缩放并压缩成 JPEG。"""

    raw = file_path.read_bytes()
    raw_b64 = base64.b64encode(raw).decode()
    mime = detected_mime or mimetypes.guess_type(file_path.name)[0]
    if mime and mime.startswith("image/") and len(raw_b64) <= _IMAGE_TARGET_B64_LEN:
        return mime, raw_b64, False

    try:
        from PIL import Image, ImageOps
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "当前环境未安装 Pillow，无法压缩大图片；请安装 Pillow 后重试"
        ) from error

    with Image.open(file_path) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode not in ("RGB", "L"):
            canvas = Image.new("RGB", image.size, (255, 255, 255))
            alpha = image.getchannel("A") if "A" in image.getbands() else None
            canvas.paste(image.convert("RGB"), mask=alpha)
            image = canvas
        elif image.mode == "L":
            image = image.convert("RGB")
        if max(image.size) > _IMAGE_MAX_EDGE:
            image.thumbnail((_IMAGE_MAX_EDGE, _IMAGE_MAX_EDGE))

        chosen: bytes | None = None
        for quality in (85, 75, 65, 55, _IMAGE_MIN_QUALITY):
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            chosen = buffer.getvalue()
            encoded = base64.b64encode(chosen).decode()
            if len(encoded) <= _IMAGE_TARGET_B64_LEN:
                return "image/jpeg", encoded, True
    if chosen is None:
        raise RuntimeError("图片压缩失败")
    return "image/jpeg", base64.b64encode(chosen).decode(), True


def _read_image(file_path: Path, mime: str) -> ToolResult:
    detected_mime, encoded, compressed = _encode_image_for_model(file_path, mime)
    note = "，已自动压缩" if compressed else ""
    return ToolResult(
        text=f"[已读取图片文件 {file_path.name}{note}，图片内容已提供给多模态模型]",
        content_blocks=[
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{detected_mime};base64,{encoded}",
                    "detail": "high",
                },
            }
        ],
    )


def _decode_line(raw: bytes) -> tuple[str, bool]:
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), True


def _scan_text_file(
    file_path: Path,
    offset: int,
    limit: int | None,
) -> tuple[list[str], int, int, bool]:
    """逐行扫描以统计完整文件，同时只保留调用方需要的窗口。"""

    sliced_lines: list[str] = []
    total_lines = 0
    total_bytes = 0
    had_decode_errors = False
    with builtins.open(file_path, "rb") as file:
        while True:
            raw_line = file.readline()
            if raw_line == b"":
                break
            total_lines += 1
            total_bytes += len(raw_line)
            line, decode_error = _decode_line(raw_line)
            had_decode_errors = had_decode_errors or decode_error
            if total_lines - 1 < offset:
                continue
            if limit is not None and len(sliced_lines) >= limit:
                continue
            sliced_lines.append(line)
    return sliced_lines, total_lines, total_bytes, had_decode_errors


def _truncate_numbered_lines(
    raw_lines: list[str],
    numbered_lines: list[str],
) -> tuple[str, bool, str | None, bool, int, int]:
    if not numbered_lines:
        return "", False, None, False, 0, 0
    # 首行已经超过预算时不返回残缺半行，提示调用方改用更合适的工具。
    if len(raw_lines[0].encode("utf-8")) > _READ_MAX_BYTES:
        return "", True, "first_line_bytes", True, 0, 0

    parts: list[str] = []
    used_bytes = 0
    truncated_by: str | None = None
    for index, line in enumerate(numbered_lines):
        line_bytes = len(line.encode("utf-8"))
        if index >= _READ_MAX_LINES:
            truncated_by = "lines"
            break
        if used_bytes + line_bytes > _READ_MAX_BYTES:
            truncated_by = "bytes"
            break
        parts.append(line)
        used_bytes += line_bytes
    return (
        "".join(parts),
        truncated_by is not None,
        truncated_by,
        False,
        len(parts),
        used_bytes,
    )


class ReadFileTool(Tool):
    """读取文本或图片文件，并对大结果实施固定预算。"""

    def __init__(
        self,
        allowed_dir: Path | None = None,
        multimodal: bool = True,
        vl_available: bool = False,
        sandbox_guard: SandboxGuard | None = None,
    ) -> None:
        self._allowed_dir = allowed_dir
        self._multimodal = multimodal
        self._vl_available = vl_available
        self._sandbox_guard = sandbox_guard

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "读取文件内容。文本文件输出带行号格式（如 '     1→内容'），便于 edit_file 精确定位。"
            "图片文件由多模态模型直接查看；若非多模态，会提示使用 read_image_vision 工具。\n"
            "文本读取默认受 400 行和 10KB 双重上限保护；大文件须用 limit 分页，不要依赖自动截断后的续读。\n\n"
            "推荐策略：先 limit=50 预览文件结构，再按需读取目标行段（offset=N limit=M）。\n"
            "明显二进制文件不会按文本硬解码，会提示改用 shell 查看。\n"
            "并行读取：可在同一次响应中同时读取多个文件，无需逐一等待。\n"
            "参数说明：offset=跳过的行数（0-based），limit=读取行数；二者仅对文本文件生效。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要读取的文件路径",
                },
                "offset": {
                    "type": "integer",
                    "description": "起始行号（0-based），默认 0",
                    "minimum": 0,
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "最多读取行数，默认不限（受 80K 字符上限约束）",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, **kwargs: Any) -> str | ToolResult:
        offset = int(kwargs.get("offset", 0))
        raw_limit = kwargs.get("limit")
        limit = int(raw_limit) if raw_limit is not None else None
        try:
            session_key = str(kwargs.get("session_key") or "")
            if self._sandbox_guard is not None and not session_key:
                raise SandboxError("缺少会话身份，已拒绝文件读取")
            file_path = (
                resolve_tool_target(path, self._sandbox_guard.policy(session_key))
                if self._sandbox_guard is not None and session_key
                else _resolve_path(path, self._allowed_dir)
            )
            if not file_path.exists():
                return f"错误：文件不存在：{path}"
            if not file_path.is_file():
                return f"错误：路径不是文件：{path}"
            with builtins.open(file_path, "rb") as file:
                head = file.read(_READ_PROBE_BYTES)
            image_mime = _detect_image_mime_from_header(head)
            if image_mime:
                if self._multimodal:
                    return _read_image(file_path, image_mime)
                if self._vl_available:
                    return (
                        f"[检测到图片文件 {file_path.name}（{image_mime}）]\n"
                        f"当前主模型不支持多模态，无法直接查看图片内容。\n"
                        f"请使用 read_image_vision 工具来分析此图片：\n"
                        f"read_image_vision(path='{path}', prompt='描述你想从图片中了解什么')"
                    )
                return (
                    f"[检测到图片文件 {file_path.name}（{image_mime}）]\n"
                    f"当前主模型不支持多模态，且未配置 VL 视觉模型（llm.vl），无法处理图片。\n"
                    f"请在 config.toml 中配置 llm.vl 以启用图片识别能力。"
                )
            if _looks_binary(head):
                return (
                    f"错误：{path} 看起来是二进制文件，read_file 仅适合文本和图片。"
                    "建议改用 shell 搭配 file/xxd/strings 查看。"
                )

            sliced, total_lines, total_bytes, had_errors = _scan_text_file(
                file_path, offset, limit
            )
            numbered = [
                f"{number:6}→{line}"
                for number, line in enumerate(sliced, start=offset + 1)
            ]
            text, truncated, reason, first_too_long, output_lines, output_bytes = (
                _truncate_numbered_lines(sliced, numbered)
            )
            end_line = offset + len(sliced)
            suffix = ""
            if first_too_long:
                suffix = (
                    "\n\n[已截断：首行超过 10KB，直接返回半行价值很低。"
                    "建议缩小读取范围，或改用 shell 查看局部字节内容。]"
                )
            elif truncated:
                reason_text = "行数超限" if reason == "lines" else "字节数超限"
                suffix = (
                    f"\n\n[已截断：文件共 {total_lines} 行 / {total_bytes} 字节，"
                    f"本次返回 {output_lines} 行 / {output_bytes} 字节，因{reason_text}只返回前一部分。"
                    f"建议用 limit=N 分段读取，例如 offset={offset} limit=100。]"
                )
            elif offset > 0 or limit is not None:
                suffix = (
                    f"\n\n[第 {offset + 1}–{end_line} 行 / "
                    f"共 {total_lines} 行 / {total_bytes} 字节]"
                )
            if had_errors:
                suffix += "\n\n[提示：文件不是标准 UTF-8，已用替代字符显示无法解码的字节。]"
            return text + suffix
        except PermissionError as error:
            return f"错误：{error}"
        except Exception as error:
            return f"读取文件失败：{error}"


class WriteFileTool(Tool):
    """完整覆盖写入文本文件，并自动创建父目录。"""

    def __init__(
        self,
        allowed_dir: Path | None = None,
        *,
        sandbox_guard: SandboxGuard | None = None,
        mutation_broker: FilesystemMutationBroker | None = None,
    ) -> None:
        self._allowed_dir = allowed_dir
        self._sandbox_guard = sandbox_guard
        self._mutation_broker = mutation_broker

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "将内容写入文件（完整覆盖写）。不存在的父目录自动创建。\n\n"
            "使用规则：\n"
            "- 优先使用 edit_file 修改已有文件；仅在创建新文件或完整重写时使用 write_file\n"
            "- 写入已存在的文件前，必须先用 read_file 读取当前内容，禁止盲写\n"
            "- 不得主动创建文档文件（*.md、README）除非用户明确要求\n"
            "- 写入路径须为绝对路径或相对工作目录的合法路径"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要写入的文件路径"},
                "content": {"type": "string", "description": "要写入的文本内容"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        try:
            session_key = str(kwargs.get("session_key") or "")
            if self._sandbox_guard is not None and not session_key:
                raise SandboxError("缺少会话身份，已拒绝文件写入")
            if self._sandbox_guard is not None and self._mutation_broker is not None and session_key:
                policy = self._sandbox_guard.policy(session_key)
                target = resolve_tool_target(path, policy)
                authorized = await self._sandbox_guard.authorize_file_mutation(
                    session_key=session_key,
                    turn_id=str(kwargs.get("turn_id") or ""),
                    call_id=str(kwargs.get("call_id") or ""),
                    tool_name=self.name,
                    arguments={"path": path, "content": content},
                    target=target,
                    operation="完整写入文件",
                )
                return await self._mutation_broker.execute(
                    session_key=session_key,
                    operation=self.name,
                    arguments={"path": path, "content": content},
                    execution_mode=authorized.mode,
                )
            file_path = _resolve_path(path, self._allowed_dir)

            async def write() -> str:
                if file_path.exists() and file_path.is_dir():
                    return f"写入文件失败：目标路径是目录：{path}"
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content, encoding="utf-8")
                return f"已写入 {len(content)} 字节到 {path}"

            return await _run_with_file_mutation_lock(file_path, write)
        except (PermissionError, SandboxError) as error:
            return f"错误：{error}"
        except Exception as error:
            return f"写入文件失败：{error}"


class EditFileTool(Tool):
    """精确替换文件文本，并返回可审查的 unified diff。"""

    def __init__(
        self,
        allowed_dir: Path | None = None,
        *,
        sandbox_guard: SandboxGuard | None = None,
        mutation_broker: FilesystemMutationBroker | None = None,
    ) -> None:
        self._allowed_dir = allowed_dir
        self._sandbox_guard = sandbox_guard
        self._mutation_broker = mutation_broker

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "将文件中的 old_text 精确替换为 new_text。\n\n"
            "重要：old_text 和 new_text 是文件的原始内容，不包含 read_file 输出的行号前缀。\n"
            "从 read_file 输出复制 old_text 时，必须去掉行首的 '     N→' 前缀，只保留实际文本内容。\n"
            "old_text 必须与文件内容完全一致（含缩进和换行）。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要编辑的文件路径"},
                "old_text": {
                    "type": "string",
                    "description": "要查找并替换的原始文本（必须与文件内容完全一致，不含行号前缀）",
                },
                "new_text": {"type": "string", "description": "替换后的新文本"},
                "replace_all": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "是否替换文件中所有匹配项，默认 False（只替换第一处）。"
                        "重命名变量、批量修改相同字符串时设为 true。"
                        "不确定匹配数量时先省略，收到'出现N次'警告后再决定。"
                    ),
                },
            },
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(
        self,
        path: str,
        old_text: str,
        new_text: str,
        **kwargs: Any,
    ) -> str:
        replace_all = bool(kwargs.get("replace_all", False))
        try:
            session_key = str(kwargs.get("session_key") or "")
            if self._sandbox_guard is not None and not session_key:
                raise SandboxError("缺少会话身份，已拒绝文件编辑")
            if self._sandbox_guard is not None and self._mutation_broker is not None and session_key:
                policy = self._sandbox_guard.policy(session_key)
                target = resolve_tool_target(path, policy)
                arguments = {
                    "path": path,
                    "old_text": old_text,
                    "new_text": new_text,
                    "replace_all": replace_all,
                }
                authorized = await self._sandbox_guard.authorize_file_mutation(
                    session_key=session_key,
                    turn_id=str(kwargs.get("turn_id") or ""),
                    call_id=str(kwargs.get("call_id") or ""),
                    tool_name=self.name,
                    arguments=arguments,
                    target=target,
                    operation="精确编辑文件",
                )
                return await self._mutation_broker.execute(
                    session_key=session_key,
                    operation=self.name,
                    arguments=arguments,
                    execution_mode=authorized.mode,
                )
            file_path = _resolve_path(path, self._allowed_dir)

            async def edit() -> str:
                if not file_path.exists():
                    return f"错误：文件不存在：{path}"
                raw_content = file_path.read_bytes().decode("utf-8")
                content, has_bom = _strip_utf8_bom(raw_content)
                matched = old_text
                replacement = new_text
                # 模型通常生成 LF；对纯 Windows 文本自动转换为 CRLF 后再匹配。
                if matched not in content and _supports_crlf_compat(content):
                    compatible = old_text.replace("\n", "\r\n")
                    if compatible in content:
                        matched = compatible
                        replacement = new_text.replace("\n", "\r\n")
                if matched not in content:
                    return "错误：未找到 old_text，请确保与文件内容完全一致。"
                count = content.count(matched)
                if count > 1 and not replace_all:
                    return (
                        f"警告：old_text 在文件中出现了 {count} 次。"
                        "如需全部替换，设 replace_all=true；如需精确定位，请包含更多上下文。"
                    )
                new_content = (
                    content.replace(matched, replacement)
                    if replace_all
                    else content.replace(matched, replacement, 1)
                )
                replaced_count = count if replace_all else 1
                diff = _build_edit_diff(content, new_content, path)
                restored = _restore_utf8_bom(new_content, has_bom)
                file_path.write_text(restored, encoding="utf-8", newline="")
                if diff:
                    return (
                        f"已成功编辑 {path}（替换 {replaced_count} 处）\n\n"
                        f"```diff\n{diff}\n```"
                    )
                return f"已成功编辑 {path}（替换 {replaced_count} 处）"

            return await _run_with_file_mutation_lock(file_path, edit)
        except (PermissionError, SandboxError) as error:
            return f"错误：{error}"
        except Exception as error:
            return f"编辑文件失败：{error}"


class ListDirTool(Tool):
    """按名称排序列出指定目录的直接子项。"""

    def __init__(
        self,
        allowed_dir: Path | None = None,
        *,
        sandbox_guard: SandboxGuard | None = None,
    ) -> None:
        self._allowed_dir = allowed_dir
        self._sandbox_guard = sandbox_guard

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "列举指定目录下的文件和子目录。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要列举的目录路径"}
            },
            "required": ["path"],
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            session_key = str(kwargs.get("session_key") or "")
            if self._sandbox_guard is not None and not session_key:
                raise SandboxError("缺少会话身份，已拒绝目录读取")
            directory = (
                resolve_tool_target(path, self._sandbox_guard.policy(session_key))
                if self._sandbox_guard is not None and session_key
                else _resolve_path(path, self._allowed_dir)
            )
            if not directory.exists():
                return f"错误：目录不存在：{path}"
            if not directory.is_dir():
                return f"错误：路径不是目录：{path}"
            items = [
                f"{'📁' if item.is_dir() else '📄'} {item.name}"
                for item in sorted(directory.iterdir())
            ]
            return "\n".join(items) if items else f"目录 {path} 为空"
        except PermissionError as error:
            return f"错误：{error}"
        except Exception as error:
            return f"列举目录失败：{error}"


__all__ = ["EditFileTool", "ListDirTool", "ReadFileTool", "WriteFileTool"]
