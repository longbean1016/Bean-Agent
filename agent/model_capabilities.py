"""模型能力解析，统一提供上下文窗口及其来源。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping


_PROVIDER_ALIASES = {
    # 配置使用兼容接口的常用简称，能力快照沿用供应商的标准标识。
    "qwen": "dashscope",
}

# 关键模型的能力在本地固定，避免 LiteLLM 快照把输入上限和输出上限
# 误合并成一个并不存在的总窗口。未知模型仍然允许显式配置覆盖。
_MODEL_CAPABILITIES: dict[tuple[str, str], int] = {
    ("deepseek", "deepseek-v4-flash"): 1_000_000,
    ("", "deepseek-v4-flash"): 1_000_000,
}


@dataclass(frozen=True, slots=True)
class ContextWindowResolution:
    """一次启动期能力解析的稳定结果。"""

    context_window: int
    source: str


def resolve_context_window(
    *,
    provider: str,
    model: str,
    configured: int = 0,
    configured_source: str = "",
) -> ContextWindowResolution:
    """按显式配置、模型快照、未知的顺序解析上下文窗口。

    上下文容量属于模型能力而不是请求参数；启动时解析并冻结结果，避免每轮
    访问外部目录或让同一会话在能力变化时出现不一致的压缩边界。
    """

    explicit = _positive_int(configured)
    if explicit:
        source = str(configured_source or "explicit").strip() or "explicit"
        return ContextWindowResolution(explicit, source)

    catalog_window = _find_catalog_window(provider, model)
    if catalog_window:
        return ContextWindowResolution(catalog_window, "provider_catalog")

    entry = _find_model_entry(provider, model)
    if entry is not None:
        max_input = _positive_int(entry.get("max_input_tokens"))
        max_output = _positive_int(entry.get("max_output_tokens") or entry.get("max_tokens"))
        # 输入和输出都声明时取总窗口；只有输入上限时保持输入上限，避免拿
        # 不完整的元数据扩大本地 gate，导致 provider 请求必然超限。
        context_window = max_input + max_output if max_input and max_output else max_input
        if context_window:
            return ContextWindowResolution(context_window, "litellm")

    return ContextWindowResolution(0, "unknown")


def _find_catalog_window(provider: str, model: str) -> int:
    normalized_provider = str(provider or "").strip().lower()
    normalized_model = str(model or "").strip().lower()
    if "/" in normalized_model:
        normalized_model = normalized_model.rsplit("/", 1)[-1]
    aliases = [normalized_provider]
    canonical = _PROVIDER_ALIASES.get(normalized_provider)
    if canonical and canonical not in aliases:
        aliases.append(canonical)
    for alias in aliases:
        value = _MODEL_CAPABILITIES.get((alias, normalized_model))
        if value:
            return value
    return _MODEL_CAPABILITIES.get(("", normalized_model), 0)


@lru_cache(maxsize=1)
def _model_registry() -> Mapping[str, Mapping[str, Any]]:
    """读取随运行环境安装的本地能力快照；依赖缺失时按未知能力运行。"""

    # 能力解析不能因联网刷新价格表而改变运行中的上下文边界，因此只使用安装包内快照。
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    try:
        import litellm  # type: ignore[import-not-found]
    except Exception:
        return {}
    registry = getattr(litellm, "model_cost", {})
    return registry if isinstance(registry, Mapping) else {}


def _find_model_entry(provider: str, model: str) -> Mapping[str, Any] | None:
    normalized_model = str(model or "").strip()
    if not normalized_model:
        return None
    normalized_provider = _PROVIDER_ALIASES.get(
        str(provider or "").strip().lower(),
        str(provider or "").strip().lower(),
    )
    candidates: list[str] = []
    if normalized_provider and "/" not in normalized_model:
        candidates.append(f"{normalized_provider}/{normalized_model}")
    candidates.append(normalized_model)
    registry = _model_registry()
    for candidate in candidates:
        entry = registry.get(candidate)
        if isinstance(entry, Mapping):
            return entry
    return None


def _positive_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


__all__ = ["ContextWindowResolution", "resolve_context_window"]
