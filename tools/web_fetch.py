"""网页内容抓取、SSRF 防护和文本格式转换。"""

from __future__ import annotations

import ipaddress
import json
from typing import Any, Protocol
from urllib.parse import urlparse

import html2text
import httpx
from lxml import html as lxml_html
from lxml.etree import ParserError

from tools.base import Tool

_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120
_MAX_TEXT_CHARS = 50_000
_USER_AGENT = "BeanAgent/1.0"

_ACCEPT = {
    "markdown": "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1",
    "text": "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1",
    "html": "text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, */*;q=0.1",
}


class AsyncHttpClient(Protocol):
    """抓取工具只依赖最小 GET 接口，便于注入共享客户端和离线替身。"""

    async def get(self, url: str, **kwargs: Any) -> httpx.Response: ...


class WebFetchTool(Tool):
    """抓取 HTTP(S) 内容并返回适合模型消费的结构化 JSON。"""

    name = "web_fetch"
    description = (
        "抓取指定 URL 的内容并返回。"
        "支持 text（纯文本）、markdown（转换后的 Markdown，默认）、html（原始 HTML）三种格式。"
        "仅支持 HTTP/HTTPS，响应上限 5MB。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要抓取的完整 URL，必须以 http:// 或 https:// 开头",
            },
            "format": {
                "type": "string",
                "enum": ["text", "markdown", "html"],
                "description": "返回格式：text 纯文本 / markdown 转换后的 Markdown / html 原始 HTML。默认 markdown",
            },
            "timeout": {
                "type": "integer",
                "description": f"超时秒数，默认 {_DEFAULT_TIMEOUT}，最大 {_MAX_TIMEOUT}",
                "minimum": 1,
                "maximum": _MAX_TIMEOUT,
            },
        },
        "required": ["url"],
    }

    def __init__(self, client: AsyncHttpClient | None = None) -> None:
        self._client = client

    async def execute(self, **kwargs: Any) -> str:
        url = str(kwargs["url"])
        output_format = str(kwargs.get("format", "markdown"))
        timeout = min(int(kwargs.get("timeout", _DEFAULT_TIMEOUT)), _MAX_TIMEOUT)

        if not url.startswith(("http://", "https://")):
            return _error(url, "URL 必须以 http:// 或 https:// 开头")
        target_error = _validate_url_target(url)
        if target_error:
            return _error(url, target_error)

        try:
            response = await self._get(url, output_format, timeout)
        except httpx.TimeoutException:
            return _error(url, f"请求超时（>{timeout}s）")
        except httpx.ConnectError:
            return _error(url, "无法建立连接")
        except httpx.RequestError as error:
            return _error(url, f"请求失败：{error}")
        except Exception as error:
            # 网络适配器可能抛出非 httpx 异常；工具边界统一降级，避免中断 AgentLoop。
            return _error(url, f"请求失败：{error}")

        if response.status_code != 200:
            return _error(url, f"HTTP {response.status_code}")

        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > _MAX_BYTES:
                    return _error(url, "响应过大（超过 5MB 限制）")
            except ValueError:
                pass

        body = response.content
        if len(body) > _MAX_BYTES:
            return _error(url, "响应过大（超过 5MB 限制）")

        content_type = response.headers.get("content-type", "")
        if any(
            value in content_type.lower()
            for value in (
                "application/pdf",
                "application/octet-stream",
                "image/",
                "video/",
                "audio/",
            )
        ):
            return _error(
                url,
                f"不支持二进制内容（{content_type}），请使用能处理该格式的专用工具",
            )

        encoding = response.encoding or "utf-8"
        is_html = "text/html" in content_type.lower()
        if output_format == "html":
            text = body.decode(encoding, errors="replace")
        elif output_format == "markdown" and is_html:
            text = _to_markdown(body.decode(encoding, errors="replace"))
        elif output_format == "text" and is_html:
            text = _to_text(body)
        else:
            text = body.decode(encoding, errors="replace")

        result: dict[str, Any] = {
            "url": url,
            "final_url": str(response.url),
            "status": response.status_code,
            "content_type": content_type,
            "format": output_format,
            "length": min(len(text), _MAX_TEXT_CHARS),
            "text": text[:_MAX_TEXT_CHARS],
        }
        if len(text) > _MAX_TEXT_CHARS:
            # 明示截断边界，避免模型把不完整正文误认为完整网页。
            result["truncated"] = True
            result["note"] = (
                f"内容已截断至 {_MAX_TEXT_CHARS} 字符，如需更多内容请缩小范围或使用其他工具"
            )
        return json.dumps(result, ensure_ascii=False)

    async def _get(
        self, url: str, output_format: str, timeout: int
    ) -> httpx.Response:
        request_kwargs = {
            "follow_redirects": True,
            "timeout": timeout,
            "headers": {
                "User-Agent": _USER_AGENT,
                "Accept": _ACCEPT.get(output_format, "*/*"),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        }
        if self._client is not None:
            return await self._client.get(url, **request_kwargs)

        # 临时客户端只由本次调用持有；注入的共享客户端则由组装层负责关闭。
        async with httpx.AsyncClient() as client:
            return await client.get(url, **request_kwargs)


def _error(url: str, message: str) -> str:
    return json.dumps({"error": message, "url": url}, ensure_ascii=False)


def _validate_url_target(url: str) -> str | None:
    """拒绝显式内网目标，降低工具被用于 SSRF 的风险。"""

    host = (urlparse(url).hostname or "").strip().lower()
    if not host:
        return "URL 缺少主机名"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host.endswith(".local") or host.endswith(".localhost"):
            return f"禁止访问本地域名：{host}"
        return None
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
    ):
        return f"禁止访问内网/本地地址：{host}"
    return None


def _to_markdown(raw_html: str) -> str:
    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = False
    converter.body_width = 0
    converter.unicode_snob = True
    converter.protect_links = True
    return converter.handle(raw_html).strip()


def _to_text(content: bytes) -> str:
    try:
        document = lxml_html.fromstring(content)
    except ParserError:
        return content.decode("utf-8", errors="replace")

    for tag in ("script", "style", "noscript", "iframe", "object", "embed"):
        for element in document.xpath(f"//{tag}"):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
    # 按文本节点插入边界，避免相邻块级标签被 lxml 拼成 ``TitleHello``。
    return " ".join(" ".join(document.itertext()).split())


__all__ = ["WebFetchTool"]
