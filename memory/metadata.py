"""记忆元数据的 JSON 持久化边界。"""

from __future__ import annotations

import json


def serialize_memory_metadata(metadata: object) -> str:
    """拒绝运行时对象和非法 JSON 数值，不把对象强制转成字符串。"""

    if not isinstance(metadata, dict):
        raise ValueError("记忆元数据必须是 JSON 对象")
    try:
        return json.dumps(metadata, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        # 不回显元数据或底层异常，避免把记忆内容与内部对象带入错误日志。
        raise ValueError("记忆元数据必须可序列化为 JSON，不能包含运行时对象、循环引用或非有限数值") from None
