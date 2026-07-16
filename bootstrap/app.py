"""BeanAgent 应用依赖组装与生命周期容器。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agent.agent_loop import AgentLoop
from agent.config_models import Config
from agent.event_bus import EventBus
from agent.message_bus import MessageBus
from agent.pipeline import Pipeline
from agent.prompt_assembler import MessageEnvelopeBuilder, PromptAssembler
from agent.prompt_block import SectionCache, SystemPromptBuilder, default_prompt_blocks
from agent.provider import LLMProvider, create_vision_provider
from memory.embedder import Embedder
from memory.engine import MemoryEngine
from session.manager import SessionManager
from tools import ToolRegistry, register_all


@dataclass(slots=True)
class CoreRuntime:
    """集中持有最小闭环组件，后续 AppRuntime 只负责编排启动和关闭。"""

    config: Config
    workspace: Path
    provider: Any
    embedder: Any | None
    sessions: SessionManager
    memory: MemoryEngine | None
    tools: ToolRegistry
    message_bus: MessageBus
    event_bus: EventBus
    assembler: PromptAssembler
    pipeline: Pipeline
    agent_loop: AgentLoop
    vision_provider: Any | None = None


def build_core_runtime(
    config: Config,
    workspace: Path,
    *,
    provider: Any | None = None,
    embedder: Any | None = None,
) -> CoreRuntime:
    """按依赖方向构造核心组件，不启动任务或监听端口。"""

    root = Path(workspace).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    workdir = Path(config.agent.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    # 主 Provider 是应用级共享资源，由 Runtime 最终关闭；MemoryEngine 只借用它执行
    # QueryRewriter/Consolidation，不拥有其生命周期。
    main_provider = provider or LLMProvider(config.llm)
    sessions = SessionManager(root, history_window=config.session.history_window)
    events = EventBus()
    messages = MessageBus()

    memory: MemoryEngine | None = None
    actual_embedder = embedder
    if config.memory.enabled:
        if actual_embedder is None:
            embedding_config = replace(
                config.memory.embedding,
                api_key=config.memory.embedding.api_key or config.llm.api_key,
                base_url=config.memory.embedding.base_url or str(config.llm.base_url or ""),
            )
            actual_embedder = Embedder(embedding_config)
        # SessionManager 是 SessionStore 的唯一所有者；MemoryEngine 只使用同一 Store
        # 回源已提交消息，close() 不会关闭 sessions.store。
        memory = MemoryEngine(root, actual_embedder, main_provider, sessions.store, config=config.memory)
        memory.bind_events(events)

    vision_provider = create_vision_provider(config.llm.vl)
    tools = ToolRegistry()
    register_all(
        tools,
        allowed_dir=workdir,
        multimodal=config.llm.multimodal,
        vl_provider=vision_provider,
        vl_model=config.llm.vl.model if config.llm.vl else "",
        session_store=sessions.store,
        memory_engine=memory,
    )
    assembler = PromptAssembler(
        SystemPromptBuilder(default_prompt_blocks(), cache=SectionCache()),
        MessageEnvelopeBuilder(),
    )
    pipeline = Pipeline(
        main_provider,
        tools,
        events,
        assembler,
        workspace=str(root),
        memory=memory,
        history_loader=sessions.load_history,
        history_limit=config.session.history_window,
        max_iterations=config.llm.max_iterations or 10,
    )
    agent_loop = AgentLoop(messages, events, pipeline, sessions)
    return CoreRuntime(
        config=config,
        workspace=root,
        provider=main_provider,
        embedder=actual_embedder,
        sessions=sessions,
        memory=memory,
        tools=tools,
        message_bus=messages,
        event_bus=events,
        assembler=assembler,
        pipeline=pipeline,
        agent_loop=agent_loop,
        vision_provider=vision_provider,
    )


__all__ = ["CoreRuntime", "build_core_runtime"]
