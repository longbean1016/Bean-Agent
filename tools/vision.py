"""使用独立 VL 模型分析本地图片。"""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tools.base import Tool
from tools.filesystem import _detect_image_mime_from_header, _resolve_path

if TYPE_CHECKING:
    from agent.provider import LLMProvider

_VL_MAX_FILE_BYTES = 20 * 1024 * 1024
_VL_MAX_DATA_URI_BYTES = 8 * 1024 * 1024
_VL_MAX_EDGE = 4096


def _encode_image_data_uri(file_path: Path) -> str:
    """校验图片并编码 data URI，超过预算时逐级缩放压缩。"""

    file_size = os.path.getsize(file_path)
    if file_size > _VL_MAX_FILE_BYTES:
        raise ValueError(
            f"图片文件过大（{file_size / 1024 / 1024:.1f}MB），"
            "上限为 20MB。请压缩或裁剪后重试。"
        )
    raw = file_path.read_bytes()
    mime = _detect_image_mime_from_header(raw[:4096])
    if mime is None:
        raise ValueError("不支持的图片格式。仅支持 PNG、JPEG、GIF、BMP、WebP。")

    try:
        from PIL import Image, ImageOps
    except ModuleNotFoundError as error:
        raise ValueError("当前环境未安装 Pillow，无法校验图片。") from error

    try:
        with Image.open(file_path) as image:
            image.verify()
    except Exception as error:
        raise ValueError("图片文件无法解码或已损坏。") from error

    with Image.open(file_path) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode not in ("RGB", "L"):
            canvas = Image.new("RGB", image.size, (255, 255, 255))
            alpha = image.getchannel("A") if "A" in image.getbands() else None
            canvas.paste(image.convert("RGB"), mask=alpha)
            image = canvas
        elif image.mode == "L":
            image = image.convert("RGB")

        raw_b64_length = len(base64.b64encode(raw).decode())
        if max(image.size) > _VL_MAX_EDGE or raw_b64_length > _VL_MAX_DATA_URI_BYTES:
            image.thumbnail((_VL_MAX_EDGE, _VL_MAX_EDGE))

        buffer = io.BytesIO()
        if mime == "image/jpeg":
            image.save(buffer, format="JPEG", quality=95, optimize=True)
            clean_mime = "image/jpeg"
        else:
            image.save(buffer, format="PNG", optimize=True)
            clean_mime = "image/png"
        clean_b64 = base64.b64encode(buffer.getvalue()).decode()
        if len(clean_b64) <= _VL_MAX_DATA_URI_BYTES:
            return f"data:{clean_mime};base64,{clean_b64}"

        for quality in (85, 75, 65, 55, 45):
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            encoded = base64.b64encode(buffer.getvalue()).decode()
            if len(encoded) <= _VL_MAX_DATA_URI_BYTES:
                return f"data:image/jpeg;base64,{encoded}"
    raise ValueError("图片压缩后仍然过大，请继续压缩或裁剪。")


class ReadImageVisionTool(Tool):
    """主模型不能直接读图时，调用独立视觉模型返回文字描述。"""

    def __init__(
        self,
        vl_provider: LLMProvider,
        vl_model: str,
        allowed_dir: Path | None = None,
    ) -> None:
        self._provider = vl_provider
        self._model = vl_model
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "read_image_vision"

    @property
    def description(self) -> str:
        return (
            "使用独立的视觉模型分析图片内容。主模型无法直接查看图片时使用此工具。"
            "你需要提供一个 prompt 来说明你想从图片中了解什么。\n\n"
            "参数说明：\n"
            "- path：图片文件的路径\n"
            "- prompt：描述你想从这张图片中了解什么内容，越具体越好。"
            "例如 '图中有什么文字？'、'描述这张图片中的物体和场景'、"
            "'这张表格中第3行的数据是什么？'\n\n"
            "限制：原始文件不超过20MB，超限图片会自动缩放至最宽/最高4096像素并压缩。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "图片文件的路径"},
                "prompt": {
                    "type": "string",
                    "description": "描述你想从图片中了解什么内容，越具体越好",
                },
            },
            "required": ["path", "prompt"],
        }

    async def execute(self, path: str, prompt: str, **kwargs: Any) -> str:
        try:
            file_path = _resolve_path(path, self._allowed_dir)
            if not file_path.exists():
                return f"错误：文件不存在：{path}"
            if not file_path.is_file():
                return f"错误：路径不是文件：{path}"
            data_uri = _encode_image_data_uri(file_path)
        except ValueError as error:
            return f"图片处理失败：{error}"
        except Exception as error:
            return f"读取图片文件失败：{error}"

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_uri, "detail": "high"},
                    },
                ],
            }
        ]
        try:
            response = await self._provider.chat(
                messages=messages,
                tools=[],
                model=self._model,
                max_tokens=2048,
                disable_thinking=True,
            )
            if response.content:
                return response.content
            if response.thinking:
                return f"[VL 模型思考过程]\n{response.thinking}"
            return "视觉模型未返回任何内容，请调整 prompt 后重试。"
        except Exception as error:
            return f"调用视觉模型失败：{error}"


__all__ = ["ReadImageVisionTool"]
