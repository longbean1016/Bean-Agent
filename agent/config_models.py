"""BeanAgent 配置数据模型。

集中定义各模块接收的配置结构，下游模块不直接读取 TOML。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class VisionConfig:
    """独立视觉模型配置；model 为空表示不启用 VL 工具。"""

    provider: str = ""
    model: str = ""
    api_key: str = ""
    base_url: str | None = None
    max_tokens: int = 2048
    request_timeout_s: float = 90.0


@dataclass
class LLMConfig:
    """主语言模型及 Agent 调用参数。"""

    provider: str = ""  # OpenAI 兼容服务商，用于选择默认接口地址
    model: str = "deepseek-v4-flash"  # 主对话模型
    api_key: str = ""  # 服务商密钥，推荐通过环境变量注入
    base_url: str | None = None  # 自定义兼容接口地址，空值时按 provider 推断
    max_tokens: int = 8192  # 单次模型回复允许生成的最大 token 数
    context_window: int = 0  # Provider 输入上下文上限；0 表示模型容量未知
    context_window_source: str = "unknown"  # 上下文能力来源，便于界面区分估算可靠性
    max_iterations: int = 10  # 单轮 ReAct 最大迭代次数，0 表示不限制
    system_prompt: str = ""  # 额外系统提示词，空值时由 PromptBlock 组装
    request_timeout_s: float = 90.0  # 单次模型请求超时秒数
    multimodal: bool = True  # 主模型是否能直接接收 image_url 内容块
    vl: VisionConfig | None = None  # 主模型不支持图片时使用的独立视觉模型
    # DeepSeek 的 thinking 等扩展参数不属于标准 OpenAI 字段，需要放入
    # extra_body。default_factory 保证不同 Config 实例不会共享可变字典。
    extra_body: dict[str, object] = field(default_factory=dict)


@dataclass
class EmbeddingConfig:
    """记忆向量化模型参数。"""

    model: str = "text-embedding-v3"  # 向量模型
    api_key: str = ""  # 向量服务密钥，空值时由组装层复用主模型密钥
    base_url: str = ""  # 向量服务的 OpenAI 兼容接口地址
    dimensions: int = 1024  # BeanAgent 记忆表使用的固定向量维度


@dataclass
class OptimizerConfig:
    """长期记忆后台整理参数。"""

    enabled: bool = True  # 是否启动长期记忆整理任务
    interval_seconds: int = 64800  # 两次完整整理之间的间隔，默认 18 小时
    merge_max_tokens: int = 16384  # 合并记忆时允许使用的最大 token 数
    self_update_max_tokens: int = 2048  # 更新自我认知记忆时的最大 token 数
    step_delay_seconds: int = 15  # 连续整理步骤之间的延迟，避免请求过密


@dataclass
class RetrievalConfig:
    """记忆检索与融合排序参数。"""

    hotness_alpha: float = 0.20  # 最终排序中记忆热度的权重
    half_life_days: float = 14.0  # 记忆热度随时间衰减的半衰期天数
    rrf_k: int = 60  # 双路检索进行 RRF 融合时的平滑常数
    keyword_rrf_weight: float = 0.5  # 关键词检索在 RRF 融合中的权重
    procedure_threshold: float = 0.66  # 流程记忆自动注入的最低原始相关分
    preference_threshold: float = 0.5  # 偏好记忆自动注入的最低原始相关分
    event_threshold: float = 0.5  # 事件记忆自动注入的最低原始相关分
    profile_threshold: float = 0.5  # 用户画像记忆自动注入的最低原始相关分
    max_forced_procedures: int = 3  # 带工具要求的强制流程单次注入上限
    max_procedure_preference: int = 4  # 单次注入的流程和偏好总数上限
    max_event_profile: int = 4  # 单次注入的事件和画像总数上限


@dataclass
class DedupConfig:
    """记忆去重与替代阈值。"""

    event_candidate_threshold: float = 0.75  # Event 进入 LLM 语义判断的最低向量相似度
    profile_candidate_threshold: float = 0.72  # Profile 进入 LLM 语义判断的最低向量相似度
    preference_candidate_threshold: float = 0.65  # Preference 进入 LLM 语义判断的最低向量相似度
    procedure_candidate_threshold: float = 0.82  # Procedure 进入 LLM 语义判断的最低向量相似度
    candidate_top_k: int = 5  # 每条新记忆最多提供给 LLM 的同类型旧记忆候选数


@dataclass
class MemoryConfig:
    """记忆模块总配置。"""

    enabled: bool = False  # 是否启用长期记忆闭环
    engine_name: str = "default"  # 记忆引擎名称，当前最小版本仅支持 default
    # 旧配置字段仅为反序列化兼容保留；历史范围和压缩触发均由 Session
    # checkpoint 边界及 LLM token gate 决定，不能再从这里派生消息数窗口。
    context_window: int = 0
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)  # 向量配置
    optimizer: OptimizerConfig = field(default_factory=OptimizerConfig)  # 整理配置
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)  # 检索配置
    dedup: DedupConfig = field(default_factory=DedupConfig)  # 去重配置


@dataclass
class WebChatConfig:
    """网页聊天服务参数。"""

    enabled: bool = True  # 是否随主进程启动网页聊天通道
    host: str = "127.0.0.1"  # 默认仅监听本机，避免无意暴露服务
    port: int = 6322  # Web 服务监听端口
    channel_name: str = "web"  # 生成 session_key 时使用的通道前缀


@dataclass
class AgentConfig:
    """Agent 运行目录参数。"""

    workdir: str = "."  # 工具执行的相对工作目录，由 workspace 约束实际根路径
    max_concurrent_turns: int = 5  # 不同会话同时进入 Pipeline 的上限
    max_queued_turns: int = 20  # 并发占满后允许保存在内存中的等待会话数

    def __post_init__(self) -> None:
        if self.max_concurrent_turns < 1:
            raise ValueError("max_concurrent_turns 必须大于等于 1")
        if self.max_queued_turns < 0:
            raise ValueError("max_queued_turns 必须大于等于 0")


@dataclass
class ChannelsConfig:
    """消息通道配置集合。"""

    chat: WebChatConfig = field(default_factory=WebChatConfig)  # 网页聊天通道


@dataclass
class Config:
    """应用根配置。"""

    llm: LLMConfig = field(default_factory=LLMConfig)  # 主模型与循环参数
    memory: MemoryConfig = field(default_factory=MemoryConfig)  # 长期记忆参数
    agent: AgentConfig = field(default_factory=AgentConfig)  # Agent 运行参数
    channels: ChannelsConfig = field(default_factory=ChannelsConfig)  # 通道参数

    @classmethod
    def load(cls, path: str | Path = "config.toml") -> Config:
        """从 TOML 文件创建根配置。"""

        from agent.config import load_config

        return load_config(path)


__all__ = [
    "AgentConfig",
    "ChannelsConfig",
    "Config",
    "DedupConfig",
    "EmbeddingConfig",
    "LLMConfig",
    "MemoryConfig",
    "OptimizerConfig",
    "RetrievalConfig",
    "WebChatConfig",
    "VisionConfig",
]
