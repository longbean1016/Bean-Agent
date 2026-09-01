"""模型输入的无敏感诊断：规范化哈希与相邻请求共同前缀。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from agent.context_budget import estimate_payload_tokens, estimate_tokens


def canonical_json(value: object) -> str:
    """使用稳定 JSON 表示请求内容；对象键排序但不改变消息和 block 顺序。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def canonical_payload_hash(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> str:
    """返回模型输入消息和工具 schema 的 SHA-256 摘要。"""

    payload = {"messages": messages, "tools": list(tools or [])}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def canonical_header_hash(header: object) -> str:
    """返回请求 header/config 的 SHA-256 摘要，不把明文写入诊断。"""

    return hashlib.sha256(canonical_json(header).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PromptCacheRequestDiagnostics:
    """一次实际模型请求的结构诊断，不包含正文或工具参数。"""

    canonical_hash: str
    header_hash: str
    epoch_id: str
    message_count: int
    tool_count: int
    estimated_input_tokens: int
    common_prefix_messages: int
    common_prefix_tokens: int

    def as_log_fields(self) -> dict[str, object]:
        """转换为 Prompt Cache JSONL 可追加的无敏感字段。"""

        return {
            "canonical_hash": self.canonical_hash,
            "header_hash": self.header_hash,
            "epoch_id": self.epoch_id,
            "message_count": self.message_count,
            "tool_count": self.tool_count,
            "estimated_input_tokens": self.estimated_input_tokens,
            "common_prefix_messages": self.common_prefix_messages,
            "common_prefix_tokens": self.common_prefix_tokens,
        }


@dataclass(frozen=True, slots=True)
class _RequestFingerprint:
    message_fingerprints: tuple[str, ...]
    message_count: int
    header_hash: str


class PromptCacheDiagnostics:
    """按会话比较相邻模型请求，避免不同会话污染共同前缀统计。"""

    def __init__(self) -> None:
        self._previous: dict[str, _RequestFingerprint] = {}

    def observe(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        header: object | None = None,
    ) -> PromptCacheRequestDiagnostics:
        """记录一次请求并返回与该会话上一次请求的共同前缀。"""

        key = str(session_key)
        message_fingerprints = tuple(canonical_json(item) for item in messages)
        header_hash = canonical_header_hash(header) if header is not None else ""
        epoch_id = header_hash[:16] if header_hash else "default"
        previous = self._previous.get(key)
        common = 0
        if previous is not None and previous.header_hash == header_hash:
            limit = min(previous.message_count, len(message_fingerprints))
            # 一旦某条消息不同，后面的消息不能再作为连续前缀复用。
            while common < limit and (
                previous.message_fingerprints[common] == message_fingerprints[common]
            ):
                common += 1

        common_prefix_tokens = estimate_tokens(list(messages[:common])) + common * 4
        diagnostics = PromptCacheRequestDiagnostics(
            canonical_hash=canonical_payload_hash(messages, tools),
            header_hash=header_hash,
            epoch_id=epoch_id,
            message_count=len(messages),
            tool_count=len(tools or []),
            estimated_input_tokens=estimate_payload_tokens(messages, tools),
            common_prefix_messages=common,
            common_prefix_tokens=common_prefix_tokens,
        )
        self._previous[key] = _RequestFingerprint(
            message_fingerprints=message_fingerprints,
            message_count=len(message_fingerprints),
            header_hash=header_hash,
        )
        return diagnostics

    def reset(self, session_key: str) -> None:
        """清除一个会话的比较锚点，供压缩 replace 或会话删除使用。"""

        self._previous.pop(str(session_key), None)


__all__ = [
    "PromptCacheDiagnostics",
    "PromptCacheRequestDiagnostics",
    "canonical_json",
    "canonical_header_hash",
    "canonical_payload_hash",
]
