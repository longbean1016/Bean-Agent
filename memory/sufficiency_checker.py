"""判断当前记忆召回是否需要启动额外增强。"""

from __future__ import annotations


def should_enhance_retrieval(items: list[dict[str, object]]) -> bool:
    """仅在完全没有召回结果时触发 HyDE。

    akashic 的当前策略不对已有结果再做一次 LLM 质量判断；类型阈值和适用性由
    后续注入规划负责，这里保持确定性并减少回复链路额外延迟。
    """

    return not items


__all__ = ["should_enhance_retrieval"]
