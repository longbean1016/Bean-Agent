"""BeanAgent 应用依赖组装与生命周期容器。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket

from agent.agent_loop import AgentLoop
from agent.channel import WebChannel
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

logger = logging.getLogger(__name__)


class MemoryMaintenanceLoop:
    """启动恢复与周期 Optimizer；不包含主动任务或插件调度。"""

    def __init__(self, memory: Any, *, enabled: bool, interval_seconds: float) -> None:
        self._memory = memory
        self._enabled = bool(enabled)
        self._interval = max(0.001, float(interval_seconds))
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise RuntimeError("MemoryMaintenanceLoop 已关闭")
        # 先重放 outbox，再允许服务接受新 Turn，避免旧窗口长期滞留并与新写入交错。
        await self._memory.replay_pending_consolidations()
        self._started = True
        if self._enabled:
            self._task = asyncio.create_task(self._run(), name="memory-optimizer")

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
                return
            except TimeoutError:
                pass
            try:
                await self._memory.optimize()
            except Exception:
                # 周期整理失败不能终止对话服务；PENDING snapshot 自身负责回滚，
                # 下一周期会重新尝试。
                logger.exception("记忆 Optimizer 周期执行失败")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)


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


def create_fastapi_app(runtime: CoreRuntime) -> FastAPI:
    """为已组装 Runtime 暴露 WebSocket 路由，不复制或隐式替换依赖。"""

    app = FastAPI()
    channel = WebChannel(runtime.message_bus, runtime.event_bus, runtime.agent_loop)
    # 测试、lifespan 和后续前端静态服务都从 app.state 读取同一实例，不能在路由
    # 函数中按连接重新创建 Channel，否则连接池无法按 session_key 广播。
    app.state.core_runtime = runtime
    app.state.web_channel = channel

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await channel.handle_websocket(websocket)

    return app


__all__ = ["CoreRuntime", "MemoryMaintenanceLoop", "build_core_runtime", "create_fastapi_app"]
