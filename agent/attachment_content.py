"""把已通过 WebChannel 校验的附件转换为模型用户内容。"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from pathlib import Path
from typing import Any

_MAX_TEXT_CHARS = 100_000


async def build_current_user_content(
    text: str,
    media_paths: list[str],
    *,
    multimodal: bool = True,
    vl_available: bool = False,
) -> str | list[dict[str, Any]]:
    """按模型能力构造附件内容，并避免在事件循环中执行同步文件 IO。"""

    if not media_paths:
        return text
    return await asyncio.to_thread(
        _build_current_user_content_sync,
        text,
        media_paths,
        multimodal=multimodal,
        vl_available=vl_available,
    )


def _build_current_user_content_sync(
    text: str,
    media_paths: list[str],
    *,
    multimodal: bool,
    vl_available: bool,
) -> str | list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    deferred_images: list[str] = []
    text_parts: list[str] = []
    for raw_path in media_paths:
        path = Path(raw_path)
        mime, _ = mimetypes.guess_type(path.name)
        if mime and mime.startswith("image/"):
            if not multimodal:
                deferred_images.append(raw_path)
                continue
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            })
            continue
        content = path.read_text(encoding="utf-8")
        if len(content) > _MAX_TEXT_CHARS:
            omitted = len(content) - _MAX_TEXT_CHARS
            content = f"{content[:_MAX_TEXT_CHARS]}\n...[省略 {omitted} 个字符]"
        text_parts.append(f"[文本附件: {path.name}]\n```text\n{content}\n```")

    combined = "\n\n".join([part for part in [text.strip(), *text_parts] if part]).strip()
    # 多模态开关只决定图片如何消费；UTF-8 文本附件始终直接进入当前消息，
    # 避免模型根据工作目录自行猜测上传路径是否可访问。
    if not multimodal:
        return _build_text_with_media_refs(
            combined,
            deferred_images,
            vl_available=vl_available,
        )
    if not images:
        return combined
    return [*images, {"type": "text", "text": combined}]


def _build_text_with_media_refs(
    text: str,
    media_paths: list[str],
    *,
    vl_available: bool,
) -> str:
    """为纯文本主模型保留附件引用，图片只能经独立 VL 工具读取。"""

    refs: list[str] = []
    local_images: list[str] = []
    for raw_path in media_paths:
        value = str(raw_path)
        if value.startswith(("http://", "https://")):
            refs.append(f"- 图片URL: {value}")
            continue

        path = Path(value)
        mime, _ = mimetypes.guess_type(path.name)
        if not path.is_file():
            continue
        if mime and mime.startswith("image/"):
            refs.append(f"- 图片路径: {value}")
            local_images.append(value)
        else:
            refs.append(f"- 文件路径: {value}")

    if not refs:
        return text

    lines = [text, "", "[附加媒体]", *refs]
    if vl_available and local_images:
        # 主模型只看到路径和明确的 ReAct 指令，不能收到它不支持的 image_url；
        # 真正的图片字节由 read_image_vision 交给独立 VL Provider。
        lines.append(
            "当前主模型不能直接接收图片内容；需要识别图片时，调用 read_image_vision 工具。"
        )
        for path in local_images:
            quoted_path = json.dumps(path, ensure_ascii=False)
            lines.append(
                f'- read_image_vision(path={quoted_path}, prompt="描述这张图片的内容")'
            )
    elif vl_available:
        lines.append("当前主模型不能直接接收图片内容；远程图片需先取得本地路径后再读图。")
    else:
        lines.append("当前主模型不能直接接收图片内容，且未配置 VL 视觉模型。")
    return "\n".join(lines)


__all__ = ["build_current_user_content"]
