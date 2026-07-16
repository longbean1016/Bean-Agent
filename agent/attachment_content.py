"""把已通过 WebChannel 校验的附件转换为模型用户内容。"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from pathlib import Path
from typing import Any

_MAX_TEXT_CHARS = 100_000


async def build_current_user_content(
    text: str,
    media_paths: list[str],
) -> str | list[dict[str, Any]]:
    """异步读取附件，避免在 AgentLoop 事件循环中执行同步文件 IO。"""

    if not media_paths:
        return text
    return await asyncio.to_thread(_build_current_user_content_sync, text, media_paths)


def _build_current_user_content_sync(
    text: str,
    media_paths: list[str],
) -> str | list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for raw_path in media_paths:
        path = Path(raw_path)
        mime, _ = mimetypes.guess_type(path.name)
        if mime and mime.startswith("image/"):
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
    if not images:
        return combined
    return [*images, {"type": "text", "text": combined}]


__all__ = ["build_current_user_content"]
