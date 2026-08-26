"""上下文压缩 checkpoint 的稳定身份和摘要工具。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any


def normalize_created_at(value: datetime | str) -> str:
    """把会话创建时间规范化为带微秒的 UTC ISO-8601 字符串。"""

    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def compaction_scope_id(session_key: str, session_created_at: datetime | str) -> str:
    """用 session incarnation 区分同名会话被删除后重新创建的情况。"""

    key = str(session_key).strip()
    if not key:
        raise ValueError("session_key 不能为空")
    normalized = normalize_created_at(session_created_at)
    digest = hashlib.sha256(f"{key}\0{normalized}".encode("utf-8")).hexdigest()[:16]
    return f"{key}@{digest}"


def compaction_source_ref(scope_id: str, generation: int) -> str:
    """返回同一 generation 重试时必须复用的稳定来源标识。"""

    safe_generation = int(generation)
    if safe_generation < 1:
        raise ValueError("generation 必须是正整数")
    digest = hashlib.sha256(f"{scope_id}\0{safe_generation}".encode("utf-8")).hexdigest()[:16]
    return f"context-compaction:{scope_id}:{safe_generation}:{digest}"


def canonical_digest(value: Any) -> str:
    """对 SourcePlan 等不可变契约做稳定 JSON 摘要，避免字典顺序造成漂移。"""

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "canonical_digest",
    "compaction_scope_id",
    "compaction_source_ref",
    "normalize_created_at",
]
