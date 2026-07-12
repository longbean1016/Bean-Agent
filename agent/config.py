"""配置加载模块。

从 TOML 文件读取配置，并支持 ``${ENV_VAR}`` 环境变量插值。
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from agent.config_models import (
    AgentConfig,
    ChannelsConfig,
    Config,
    DedupConfig,
    EmbeddingConfig,
    LLMConfig,
    MemoryConfig,
    OptimizerConfig,
    RetrievalConfig,
    SessionConfig,
    WebChatConfig,
)

_PRESETS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}
# 当 TOML 只填写 provider、没有填写 base_url 时，使用这里的 OpenAI 兼容地址。
# 显式填写的 base_url 始终优先，方便切换代理服务或私有部署模型。


def load_config(path: str | Path = "config.toml") -> Config:
    """读取 TOML，并将已知字段映射为应用配置对象。

    加载顺序为：解析 TOML -> 提取各配置段 -> 转换字段类型 -> 构造 Config。
    应用入口只调用本函数一次，后续模块通过构造函数接收对应的子配置。
    """

    # tomllib 返回普通嵌套字典。这里先读取完整文件，再分别处理 [llm]、
    # [memory]、[session]、[agent] 和 [channels]，避免下游模块重复读取文件。
    data = _load_config_data(path)

    # TOML 中缺少 [llm] 或该节点不是字典时，_as_dict 会返回空字典，
    # 下面的字段便会使用 BeanAgent 定义的默认值。
    llm_raw = _as_dict(data.get("llm"))
    provider = str(llm_raw.get("provider", "") or "")

    # 地址选择优先级：用户显式配置 > provider 预设 > None。
    # None 会留给下游 OpenAI 客户端使用其自身默认地址。
    base_url_value = llm_raw.get("base_url") or _PRESETS.get(provider)

    # TOML 不会依据 dataclass 注解自动转换类型，因此在边界处显式转换。
    # 这样下游拿到 LLMConfig 后，无需再判断字符串数字等原始输入形式。
    llm = LLMConfig(
        provider=provider,
        model=str(llm_raw.get("model", "deepseek-v4-flash")),
        api_key=_resolve(str(llm_raw.get("api_key", ""))),
        base_url=str(base_url_value) if base_url_value else None,
        max_tokens=int(llm_raw.get("max_tokens", 8192)),
        max_iterations=int(llm_raw.get("max_iterations", 10)),
        system_prompt=str(llm_raw.get("system_prompt", "") or ""),
        request_timeout_s=float(llm_raw.get("request_timeout_s", 90.0)),
    )

    session_raw = _as_dict(data.get("session"))
    agent_raw = _as_dict(data.get("agent"))

    # 根 Config 是所有配置的统一出口。记忆和通道存在多层嵌套，交给专用
    # 函数装配；结构简单的 Session 和 Agent 在这里直接创建。
    return Config(
        llm=llm,
        memory=_load_memory_config(data),
        session=SessionConfig(
            history_window=int(session_raw.get("history_window", 40))
        ),
        agent=AgentConfig(workdir=str(agent_raw.get("workdir", "."))),
        channels=_load_channels_config(data),
    )


def _resolve(value: str) -> str:
    """用环境变量替换字符串中的 ``${ENV_VAR}`` 占位符。

    例如 ``${DEEPSEEK_API_KEY}`` 会读取同名环境变量。一个字符串中可以
    出现多个占位符，普通文本保持不变。
    """

    # 未设置的变量保留原占位符，避免静默变成空密钥而难以定位配置错误。
    return re.sub(
        r"\$\{(\w+)\}",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        value,
    )


def _as_dict(value: object) -> dict[str, Any]:
    """将 TOML 子节点规范为字典，缺失或类型错误时使用空字典。

    这是配置边界的容错处理，使可选配置段缺失时可以统一走默认值。
    """

    return value if isinstance(value, dict) else {}


def _load_config_data(path: str | Path) -> dict[str, Any]:
    """读取且解析主 TOML 配置文件。

    当前主配置只接受 TOML。文件不存在、编码错误或 TOML 语法错误时，
    保留标准异常并让启动过程直接失败，避免携带错误配置继续运行。
    """

    config_path = Path(path)
    # 先检查扩展名可以为误传 JSON/YAML 文件提供更明确的错误信息。
    if config_path.suffix.lower() != ".toml":
        raise ValueError(f"主配置仅支持 TOML: {config_path.suffix}")
    return tomllib.loads(config_path.read_text(encoding="utf-8"))


def _load_memory_config(data: dict[str, Any]) -> MemoryConfig:
    """加载记忆模块及其四组嵌套配置。

    ``embedding`` 负责生成向量，``optimizer`` 负责后台整理，
    ``retrieval`` 负责召回融合，``dedup`` 负责事实和事件去重。
    """

    # 先把四个 TOML 子表拆开，随后逐一映射到对应 dataclass。
    # 每个子配置由 default_factory 独立创建，不会在 Config 实例间共享状态。
    raw = _as_dict(data.get("memory"))
    embedding_raw = _as_dict(raw.get("embedding"))
    optimizer_raw = _as_dict(raw.get("optimizer"))
    retrieval_raw = _as_dict(raw.get("retrieval"))
    dedup_raw = _as_dict(raw.get("dedup"))

    return MemoryConfig(
        # enabled=False 时，后续 bootstrap 会跳过整个记忆引擎的创建。
        enabled=bool(raw.get("enabled", False)),
        engine_name=str(raw.get("engine_name", "default")),
        # Embedding 密钥支持环境变量；若配置为空，后续组装层可复用 LLM 密钥。
        embedding=EmbeddingConfig(
            model=str(embedding_raw.get("model", "text-embedding-v3")),
            api_key=_resolve(str(embedding_raw.get("api_key", ""))),
            base_url=str(embedding_raw.get("base_url", "")),
            dimensions=int(embedding_raw.get("dimensions", 1024)),
        ),
        # 优化器只保存调度和 token 限制，本阶段不会启动后台任务。
        optimizer=OptimizerConfig(
            enabled=bool(optimizer_raw.get("enabled", True)),
            interval_seconds=int(optimizer_raw.get("interval_seconds", 64800)),
            merge_max_tokens=int(optimizer_raw.get("merge_max_tokens", 16384)),
            self_update_max_tokens=int(
                optimizer_raw.get("self_update_max_tokens", 2048)
            ),
            step_delay_seconds=int(optimizer_raw.get("step_delay_seconds", 15)),
        ),
        # 检索参数会在记忆 Batch 中注入 RRF 排序器和热度计算器。
        retrieval=RetrievalConfig(
            hotness_alpha=float(retrieval_raw.get("hotness_alpha", 0.20)),
            half_life_days=float(retrieval_raw.get("half_life_days", 14.0)),
            rrf_k=int(retrieval_raw.get("rrf_k", 60)),
            keyword_rrf_weight=float(
                retrieval_raw.get("keyword_rrf_weight", 0.5)
            ),
        ),
        # 去重阈值集中配置，避免记忆写入流程中出现散落的魔法数字。
        dedup=DedupConfig(
            supersede_threshold=float(
                dedup_raw.get("supersede_threshold", 0.90)
            ),
            event_dedup_threshold=float(
                dedup_raw.get("event_dedup_threshold", 0.92)
            ),
            event_dedup_window_days=int(
                dedup_raw.get("event_dedup_window_days", 7)
            ),
        ),
    )


def _load_channels_config(data: dict[str, Any]) -> ChannelsConfig:
    """加载当前最小闭环唯一支持的网页聊天通道。

    BeanAgent 暂不加载 akashic 的 Telegram、QQ 和 CLI 配置，避免把非闭环
    能力提前带入项目。channel_name 用于生成 ``web:<chat_id>`` session_key。
    """

    raw = _as_dict(data.get("channels"))
    chat_raw = _as_dict(raw.get("chat"))
    # 即使整个 [channels.chat] 缺失，也会生成一份只监听本机的默认配置。
    return ChannelsConfig(
        chat=WebChatConfig(
            enabled=bool(chat_raw.get("enabled", True)),
            host=str(chat_raw.get("host", "127.0.0.1")),
            port=int(chat_raw.get("port", 6322)),
            channel_name=str(chat_raw.get("channel_name", "web")),
        )
    )


__all__ = ["load_config"]
