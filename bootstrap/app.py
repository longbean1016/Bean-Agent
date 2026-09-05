"""BeanAgent 应用依赖组装与生命周期容器。"""

from __future__ import annotations

import asyncio
import mimetypes
import logging
from datetime import datetime
from io import BytesIO
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from uuid import uuid4

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError

from agent.agent_loop import AgentLoop
from agent.channel import WebChannel
from agent.config_models import Config
from agent.event_bus import EventBus, SandboxApprovalRequested
from agent.message_bus import MessageBus
from agent.mcp.manage_tools import McpAddTool, McpListTool, McpRemoveTool
from agent.mcp.registry import McpServerRegistry
from agent.pipeline import Pipeline
from agent.prompt_assembler import MessageEnvelopeBuilder, PromptAssembler
from agent.prompt_block import SectionCache, SystemPromptBuilder, default_prompt_blocks
from agent.prompt_cache_log import PromptCacheLogWriter
from agent.provider import LLMProvider, create_vision_provider
from agent.skills import SkillsLoader
from bootstrap.native_folder_picker import (
    DirectoryPicker,
    NativeHostError,
    NativeHostUnavailable,
    NativePickerBusy,
    WindowsDirectoryPicker,
    is_loopback_client,
)
from memory.embedder import Embedder
from memory.engine import MemoryEngine
from model_settings.adapters import AdapterRegistry
from model_settings.catalog import ModelCatalogService
from model_settings.catalog import CatalogUpdateError
from model_settings.discovery import (
    ModelAuthenticationError,
    ModelDiscoveryError,
    ModelDiscoveryTimeout,
    ModelDiscoveryUnsupported,
    OpenAIModelDiscovery,
)
from model_settings.models import ModelRoute
from model_settings.provider_manager import ModelInvocationTestError, ProviderManager
from model_settings.secrets import SecretStore, SecretStoreError, SqliteSecretStore
from model_settings.service import ModelSettingsNotFound, ModelSettingsService, ModelSettingsValidationError
from model_settings.store import ModelSettingsConflict, ModelSettingsStore
from proactive.agent_tools import ProactiveToolFactory
from proactive.chat_loop import ProactiveChatLoop
from proactive.models import SessionProactiveSettings
from proactive.notification_service import NotificationService
from proactive.scheduler import SchedulerService
from proactive.soft_executor import SoftTaskExecutor
from proactive.store import ProactiveStore
from proactive.turn_service import ProactiveTurnService
from session.manager import SessionManager
from sandbox.approval import ApprovalCoordinator
from sandbox.filesystem import FilesystemMutationBroker
from sandbox.guard import SandboxGuard
from sandbox.policy import SandboxPolicyResolver
from sandbox.runtime import SandboxProcessRuntime, create_runtime_temp_root
from tools import ToolRegistry, register_all
from tools.schedule import (
    CancelScheduleTool,
    ListSchedulesTool,
    ScheduleReminderTool,
    ScheduleTaskTool,
)

logger = logging.getLogger(__name__)

_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".py", ".json", ".toml", ".yaml", ".yml",
    ".csv", ".log", ".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".xml",
    # 这些格式均按 UTF-8 纯文本读取；脚本和构建文件只作为内容提供给模型，绝不执行。
    ".rst", ".adoc", ".tex", ".java", ".c", ".h", ".cpp", ".hpp", ".cs",
    ".go", ".rs", ".php", ".rb", ".swift", ".kt", ".kts", ".scala", ".lua",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".sql", ".r", ".vue",
    ".svelte", ".ini", ".conf", ".cfg", ".properties", ".ndjson", ".jsonl",
    ".tsv", ".graphql", ".gql", ".dockerfile",
}
_IMAGE_FORMATS = {"PNG", "JPEG", "GIF", "WEBP", "BMP"}
_MAX_TEXT_UPLOAD = 2 * 1024 * 1024
_MAX_IMAGE_UPLOAD = 10 * 1024 * 1024
_MODEL_ONLY_MESSAGE_FIELDS = frozenset({
    "llm_user_content",
    "llm_context_frame",
    "llm_message_timestamp",
    "llm_epoch_id",
    "llm_surface_messages",
})


def _public_chat_message(message: dict[str, Any]) -> dict[str, Any]:
    """聊天接口只返回语义消息，隐藏模型侧 Prompt 投影。"""

    return {
        key: value
        for key, value in message.items()
        if key not in _MODEL_ONLY_MESSAGE_FIELDS
    }


class MemoryMaintenanceLoop:
    """启动恢复与周期 Optimizer；不包含主动任务或插件调度。"""

    def __init__(self, memory: Any, *, enabled: bool, interval_seconds: float, now_fn: Callable[[], datetime] | None = None) -> None:
        self._memory = memory
        self._enabled = bool(enabled)
        self._interval = max(60.0, float(interval_seconds))
        self._now_fn = now_fn or datetime.now
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
        logger.info(
            "记忆 Optimizer 周期已启动: interval_seconds=%.0f",
            self._interval,
        )
        while not self._stop_event.is_set():
            seconds = self._seconds_until_next_tick()
            logger.info("距离下次记忆优化 %.0f 秒", seconds)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
                return
            except TimeoutError:
                pass
            try:
                await self._memory.optimize()
            except Exception:
                # 周期整理失败不能终止对话服务；PENDING snapshot 自身负责回滚，
                # 下一周期会重新尝试。
                logger.exception("记忆 Optimizer 周期执行失败")

    def _seconds_until_next_tick(self) -> float:
        """按绝对时间轴寻找下一周期边界，避免服务重启后重新等待完整周期。"""

        now = self._now_fn()
        current = now.replace(second=0, microsecond=0).timestamp()
        next_tick = (current // self._interval + 1) * self._interval
        return max(1.0, next_tick - now.timestamp())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)


class AppRuntime:
    """对齐参考实现的启动/关闭容器，只保留 BeanAgent 最小闭环资源。"""

    def __init__(self, core: CoreRuntime) -> None:
        self.core = core
        self.channel = WebChannel(
            core.message_bus,
            core.event_bus,
            core.agent_loop,
            media_root=core.workspace / "uploads",
            proactive_store=core.proactive_store,
            ensure_session=core.sessions.get_or_create,
            context_usage_loader=lambda session_key: asyncio.to_thread(
                core.sessions.store.get_context_usage, session_key
            ),
            session_usage_loader=lambda session_key: asyncio.to_thread(
                core.sessions.store.get_session_usage, session_key
            ),
            sandbox_loader=lambda session_key: asyncio.to_thread(
                core.sessions.store.get_session_sandbox, session_key
            ),
            sandbox_mode_writer=core.sessions.set_sandbox_mode,
            workspace_writer=core.sessions.set_workspace,
            approvals=core.sandbox_approvals,
            # 页面模型可按会话切换，WebChannel 不能再用启动 Provider 身份过滤快照。
            context_runtime_id="",
            route_resolver=lambda session_key, requested: _freeze_web_model_route(
                core, session_key, requested
            ),
        )
        self.maintenance = (
            MemoryMaintenanceLoop(
                core.memory,
                enabled=core.config.memory.optimizer.enabled,
                interval_seconds=core.config.memory.optimizer.interval_seconds,
            )
            if core.memory is not None
            else None
        )
        self.agent_task: asyncio.Task[None] | None = None
        self._started = False
        self._shutdown = False
        self._lifecycle_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            if self._shutdown:
                raise RuntimeError("AppRuntime 已关闭")
            # MCP 工具目录必须在第一条消息进入 AgentLoop 前恢复完成，避免启动
            # 窗口内同一配置在不同 Turn 中呈现不同能力。
            await self.core.mcp_registry.load_and_connect_all()
            # 恢复旧 outbox 必须先于 AgentLoop 接受新消息，保持事件顺序可审计。
            if self.maintenance is not None:
                await self.maintenance.start()
            self.agent_task = asyncio.create_task(self.core.agent_loop.run(), name="beanagent-loop")
            await self.core.scheduler.start()
            await self.core.proactive_chat.start()
            self._started = True

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            if self._shutdown:
                return
            self._shutdown = True

            # 顺序与参考 AppRuntime 一致：先阻止新工作进入，再等待/取消执行任务，
            # 最后按所有权从上层服务向底层 HTTP/SQLite 资源释放。
            await _cleanup_step("web_channel.close", self.channel.close)
            await _cleanup_step("sandbox_approvals.close", self.core.sandbox_approvals.close)
            await _cleanup_step("proactive_chat.close", self.core.proactive_chat.close)
            await _cleanup_step("proactive_scheduler.close", self.core.scheduler.close)
            await _cleanup_step("agent_loop.close", self.core.agent_loop.close)
            if self.agent_task is not None and not self.agent_task.done():
                self.agent_task.cancel()
                await asyncio.gather(self.agent_task, return_exceptions=True)
            await _cleanup_step("mcp_registry.shutdown", self.core.mcp_registry.shutdown)
            await _cleanup_step("sandbox_runtime.close", self.core.sandbox_runtime.close)
            if self.maintenance is not None:
                await _cleanup_step("memory_maintenance.close", self.maintenance.close)
            if self.core.memory is not None:
                await _cleanup_step("memory.close", self.core.memory.close)
            await _cleanup_step("proactive_store.close", asyncio.to_thread, self.core.proactive_store.close)
            await _cleanup_step("sessions.close", self.core.sessions.close)
            if self.core.vision_provider is not None:
                await _cleanup_step("vision_provider.close", self.core.vision_provider.close)
            await _cleanup_step("provider_manager.close", self.core.provider_manager.close)
            await _cleanup_step("provider.close", self.core.provider.close)
            await _cleanup_step("model_settings_store.close", asyncio.to_thread, self.core.model_store.close)


async def _cleanup_step(name: str, callback: Any, *args: Any) -> None:
    try:
        await callback(*args)
    except Exception:
        logger.exception("应用关闭步骤失败: %s", name)


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
    mcp_registry: McpServerRegistry
    message_bus: MessageBus
    event_bus: EventBus
    assembler: PromptAssembler
    pipeline: Pipeline
    agent_loop: AgentLoop
    proactive_store: ProactiveStore
    proactive_turns: ProactiveTurnService
    proactive_notifications: NotificationService
    scheduler: SchedulerService
    proactive_chat: ProactiveChatLoop
    proactive_tools: ProactiveToolFactory
    sandbox_policy: SandboxPolicyResolver
    sandbox_approvals: ApprovalCoordinator
    sandbox_guard: SandboxGuard
    sandbox_runtime: SandboxProcessRuntime
    model_store: ModelSettingsStore
    model_settings: ModelSettingsService
    provider_manager: ProviderManager
    legacy_model_available: bool
    vision_provider: Any | None = None


def build_core_runtime(
    config: Config,
    workspace: Path,
    *,
    provider: Any | None = None,
    embedder: Any | None = None,
    model_secret_store: SecretStore | None = None,
) -> CoreRuntime:
    """按依赖方向构造核心组件，不启动任务或监听端口。"""

    root = Path(workspace).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    # 主 Provider 是应用级共享资源，由 Runtime 最终关闭；MemoryEngine 只借用它执行
    # QueryRewriter/Consolidation，不拥有其生命周期。
    legacy_model_available = provider is not None or bool(config.llm.api_key and config.llm.model)
    main_provider = provider or LLMProvider(config.llm)
    sessions = SessionManager(root)
    events = EventBus()
    messages = MessageBus()
    proactive_store = ProactiveStore(root / "proactive.db")
    sandbox_policy = SandboxPolicyResolver(
        sessions.store,
        data_root=root,
        runtime_temp_root=create_runtime_temp_root(),
    )
    sandbox_approvals = ApprovalCoordinator(sessions.store)

    async def publish_approval(request: Any) -> None:
        await events.emit(
            SandboxApprovalRequested(request.session_id, request.to_wire())
        )

    sandbox_approvals.set_publisher(publish_approval)
    sandbox_guard = SandboxGuard(sandbox_policy, sandbox_approvals)
    sandbox_runtime = SandboxProcessRuntime(sandbox_policy)
    mutation_broker = FilesystemMutationBroker(sandbox_policy, sandbox_runtime)
    model_database = root / "model-settings.db"
    secrets = model_secret_store or SqliteSecretStore(model_database)
    model_store = ModelSettingsStore(model_database)
    model_settings = ModelSettingsService(
        model_store,
        secrets,
        OpenAIModelDiscovery(),
        ModelCatalogService(root / "catalog" / "models-dev-catalog.json"),
    )
    _import_legacy_model_settings(model_settings, config)
    provider_manager = ProviderManager(model_store, secrets, AdapterRegistry(), config.llm)

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
    skills = SkillsLoader(root)
    prompt_cache_log = PromptCacheLogWriter(root)
    tools = ToolRegistry()
    register_all(
        tools,
        allowed_dir=None,
        multimodal=config.llm.multimodal,
        vl_provider=vision_provider,
        vl_model=config.llm.vl.model if config.llm.vl else "",
        session_store=sessions.store,
        memory_engine=memory,
        skills=skills,
        sandbox_guard=sandbox_guard,
        sandbox_runtime=sandbox_runtime,
        mutation_broker=mutation_broker,
    )
    # 提醒工具从当前 Turn 的系统执行上下文取得 session_key，模型不需要也不能
    # 选择其他 Web 会话作为目标。
    tools.register(ScheduleReminderTool(proactive_store), risk="write", always_on=True)
    tools.register(ScheduleTaskTool(proactive_store), risk="write", always_on=True)
    tools.register(ListSchedulesTool(proactive_store), risk="read-only", always_on=True)
    tools.register(CancelScheduleTool(proactive_store), risk="write", always_on=True)
    mcp_registry = McpServerRegistry(root / "mcp_servers.json", tools)
    # 管理工具必须与运行时持有的 Registry 共享同一实例，动态添加和关闭才能
    # 作用于同一批 Client；三者常驻可见，远端工具本身仍需搜索解锁。
    tools.register(
        McpAddTool(mcp_registry),
        risk="external-side-effect",
        always_on=True,
    )
    tools.register(McpRemoveTool(mcp_registry), risk="write", always_on=True)
    tools.register(McpListTool(mcp_registry), risk="read-only", always_on=True)
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
        skills=skills,
        prompt_cache_log=prompt_cache_log,
        history_loader=sessions.load_history,
        surface_loader=sessions.load_surface,
        surface_appender=sessions.append_surface,
        event_appender=sessions.append_session_event,
        context_compactor=(memory.compact_for_context if memory is not None else None),
        context_usage_loader=lambda session_key: asyncio.to_thread(
            sessions.store.get_context_usage, session_key
        ),
        context_usage_writer=lambda session_key, snapshot: asyncio.to_thread(
            sessions.store.save_context_usage, session_key, snapshot
        ),
        session_usage_writer=lambda session_key, turn_id, iteration, usage: asyncio.to_thread(
            sessions.store.save_session_usage, session_key, turn_id, iteration, usage
        ),
        max_iterations=config.llm.max_iterations or 10,
        # 主模型和独立视觉模型是两条互斥的图片消费路径：前者直接接收图片块，
        # 后者只通过 read_image_vision 工具读取本地上传路径。
        multimodal=config.llm.multimodal,
        vl_available=vision_provider is not None,
        sandbox_guard=sandbox_guard,
        provider_manager=provider_manager,
    )
    agent_loop = AgentLoop(
        messages,
        events,
        pipeline,
        sessions,
        max_concurrent_turns=config.agent.max_concurrent_turns,
        max_queued_turns=config.agent.max_queued_turns,
    )
    proactive_turns = ProactiveTurnService(proactive_store, sessions, messages)
    proactive_notifications = NotificationService(proactive_store, messages)
    proactive_tools = ProactiveToolFactory(sessions.store, memory, tools, skills, workspace=str(root))
    soft_executor = SoftTaskExecutor(pipeline)
    scheduler = SchedulerService(
        proactive_store,
        proactive_notifications,
        soft_executor=soft_executor.execute,
    )
    proactive_chat = ProactiveChatLoop(
        proactive_store,
        sessions.store,
        main_provider,
        proactive_turns,
        proactive_tools,
        is_session_busy=agent_loop.is_session_busy,
    )
    return CoreRuntime(
        config=config,
        workspace=root,
        provider=main_provider,
        embedder=actual_embedder,
        sessions=sessions,
        memory=memory,
        tools=tools,
        mcp_registry=mcp_registry,
        message_bus=messages,
        event_bus=events,
        assembler=assembler,
        pipeline=pipeline,
        agent_loop=agent_loop,
        proactive_store=proactive_store,
        proactive_turns=proactive_turns,
        proactive_notifications=proactive_notifications,
        scheduler=scheduler,
        proactive_chat=proactive_chat,
        proactive_tools=proactive_tools,
        sandbox_policy=sandbox_policy,
        sandbox_approvals=sandbox_approvals,
        sandbox_guard=sandbox_guard,
        sandbox_runtime=sandbox_runtime,
        model_store=model_store,
        model_settings=model_settings,
        provider_manager=provider_manager,
        legacy_model_available=legacy_model_available,
        vision_provider=vision_provider,
    )


def _import_legacy_model_settings(settings: ModelSettingsService, config: Config) -> None:
    """首次启动可导入 TOML 主模型；失败时保留 legacy Provider 可用。"""

    if settings.store.list_connections() or not config.llm.api_key or not config.llm.model:
        return
    base_url = str(config.llm.base_url or "").strip()
    if not base_url and config.llm.provider.lower() == "openai":
        base_url = "https://api.openai.com/v1"
    if not base_url:
        return
    text = f"{config.llm.provider} {base_url} {config.llm.model}".lower()
    adapter = (
        "deepseek" if "deepseek" in text
        else "qwen_dashscope" if "qwen" in text or "dashscope" in text
        else "generic_openai"
    )
    try:
        connection = settings.create_connection({
            "name": "原配置模型",
            "provider": config.llm.provider,
            "base_url": base_url,
            "api_key": config.llm.api_key,
            "default_adapter": adapter,
        })
        model = settings.save_manual_model(connection["id"], {
            "model_id": config.llm.model,
            "display_name": config.llm.model,
            "context_window": config.llm.context_window or None,
            "max_output_tokens": config.llm.max_tokens,
            "supports_vision": config.llm.multimodal,
            "adapter": adapter,
        })
        settings.set_route(ModelRoute(connection["id"], model["model_id"]))
    except Exception as error:
        logger.warning("导入原模型配置失败，将继续使用启动配置: %s", type(error).__name__)


def _freeze_web_model_route(
    core: CoreRuntime,
    session_key: str,
    requested: dict[str, Any] | None,
) -> dict[str, Any] | None:
    route = ModelRoute(**requested) if requested is not None else None
    # 首轮发送可能刚创建会话，前端此前没有 session_key 可调用 REST 保存；
    # 因此在入队边界持久化显式选择，再冻结同一条路由。
    if route is not None:
        core.model_settings.set_route(route, session_key=session_key)
    elif core.model_settings.get_route(session_key) is None and core.legacy_model_available:
        return None
    return core.provider_manager.freeze(
        core.model_settings,
        session_key=session_key,
        requested=route,
    ).metadata()


def create_fastapi_app(
    runtime: CoreRuntime | AppRuntime,
    *,
    directory_picker: DirectoryPicker | None = None,
) -> FastAPI:
    """为已组装 Runtime 暴露 WebSocket 路由，不复制或隐式替换依赖。"""

    application = runtime if isinstance(runtime, AppRuntime) else AppRuntime(runtime)
    host_directories = directory_picker or WindowsDirectoryPicker()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await application.start()
        try:
            yield
        finally:
            await application.shutdown()

    app = FastAPI(lifespan=lifespan)
    channel = application.channel
    # 测试、lifespan 和后续前端静态服务都从 app.state 读取同一实例，不能在路由
    # 函数中按连接重新创建 Channel，否则连接池无法按 session_key 广播。
    app.state.core_runtime = application.core
    app.state.app_runtime = application
    app.state.web_channel = channel
    settings = application.core.model_settings
    project_root = Path(__file__).resolve().parent.parent
    static_dir = project_root / "static" / "chat"
    index_file = static_dir / "index.html"
    upload_dir = application.core.workspace / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/assets",
        StaticFiles(directory=static_dir, check_dir=False),
        name="chat_assets",
    )

    def settings_error(status: int, code: str, error: Exception) -> JSONResponse:
        return JSONResponse(status_code=status, content={"code": code, "detail": str(error)})

    app.add_exception_handler(
        ModelSettingsValidationError,
        lambda _request, error: settings_error(400, "invalid_settings", error),
    )
    app.add_exception_handler(
        ModelSettingsNotFound,
        lambda _request, error: settings_error(404, "settings_not_found", error),
    )
    app.add_exception_handler(
        ModelSettingsConflict,
        lambda _request, error: settings_error(409, "settings_conflict", error),
    )
    app.add_exception_handler(
        SecretStoreError,
        lambda _request, error: settings_error(503, "secret_store_unavailable", error),
    )
    app.add_exception_handler(
        ModelAuthenticationError,
        lambda _request, error: settings_error(401, error.code, error),
    )
    app.add_exception_handler(
        ModelDiscoveryTimeout,
        lambda _request, error: settings_error(504, error.code, error),
    )
    app.add_exception_handler(
        ModelDiscoveryUnsupported,
        lambda _request, error: settings_error(422, error.code, error),
    )
    app.add_exception_handler(
        ModelDiscoveryError,
        lambda _request, error: settings_error(502, error.code, error),
    )
    app.add_exception_handler(
        ModelInvocationTestError,
        lambda _request, error: settings_error(error.status_code, error.code, error),
    )
    app.add_exception_handler(
        CatalogUpdateError,
        lambda _request, error: settings_error(502, "catalog_update_failed", error),
    )

    @app.get("/api/settings")
    async def get_model_settings() -> dict[str, Any]:
        route = settings.get_route()
        return {
            "connections": settings.list_connections(),
            "default_route": route.public_dict() if route else None,
            "catalog": settings.store.get_catalog_state(),
            "routing_required": not (
                application.core.legacy_model_available
                and not settings.store.list_connections()
            ),
        }

    @app.post("/api/settings/connections", status_code=201)
    async def create_model_connection(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return settings.create_connection(payload)

    @app.put("/api/settings/connections/{connection_id}")
    async def update_model_connection(
        connection_id: str, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        return settings.update_connection(connection_id, payload)

    @app.get("/api/settings/connections/{connection_id}/api-key")
    async def get_model_connection_api_key(connection_id: str) -> dict[str, str]:
        return {"api_key": settings.get_connection_api_key(connection_id)}

    @app.delete("/api/settings/connections/{connection_id}", status_code=204)
    async def delete_model_connection(connection_id: str) -> Response:
        settings.delete_connection(connection_id)
        return Response(status_code=204)

    @app.post("/api/settings/connections/{connection_id}/test")
    async def test_model_connection(connection_id: str) -> dict[str, Any]:
        return await settings.test_model_list(connection_id)

    @app.post("/api/settings/connections/{connection_id}/models/refresh")
    async def refresh_connection_models(connection_id: str) -> dict[str, Any]:
        return await settings.refresh_models(connection_id)

    @app.post("/api/settings/connections/{connection_id}/models/{model_id:path}/test")
    async def test_connection_model(connection_id: str, model_id: str) -> dict[str, Any]:
        return await application.core.provider_manager.test_model(
            settings, ModelRoute(connection_id, model_id)
        )

    @app.post("/api/settings/connections/{connection_id}/models", status_code=201)
    async def create_manual_model(
        connection_id: str, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        return settings.save_manual_model(connection_id, payload)

    @app.patch("/api/settings/connections/{connection_id}/models/{model_id:path}")
    async def update_connection_model(
        connection_id: str, model_id: str, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        return settings.update_model(connection_id, model_id, payload)

    @app.get("/api/settings/routes/default")
    async def get_default_model_route() -> dict[str, Any]:
        route = settings.get_route()
        return {"route": route.public_dict() if route else None}

    @app.put("/api/settings/routes/default")
    async def set_default_model_route(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        route = settings.set_route(ModelRoute(
            str(payload.get("connection_id") or ""),
            str(payload.get("model_id") or ""),
            str(payload.get("reasoning_effort") or "") or None,
        ))
        return {"route": route.public_dict()}

    @app.get("/api/settings/routes/session/{session_key:path}")
    async def get_session_model_route(session_key: str) -> dict[str, Any]:
        _validate_settings_session_key(session_key)
        route = settings.get_route(session_key)
        return {"route": route.public_dict() if route else None}

    @app.put("/api/settings/routes/session/{session_key:path}")
    async def set_session_model_route(
        session_key: str, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        _validate_settings_session_key(session_key)
        route = settings.set_route(ModelRoute(
            str(payload.get("connection_id") or ""),
            str(payload.get("model_id") or ""),
            str(payload.get("reasoning_effort") or "") or None,
        ), session_key=session_key)
        return {"route": route.public_dict()}

    @app.post("/api/settings/catalog/update")
    async def update_model_catalog() -> dict[str, Any]:
        return await settings.update_catalog()

    @app.get("/", response_model=None)
    def chat_index() -> FileResponse | dict[str, str]:
        if index_file.is_file():
            return FileResponse(index_file)
        return {"status": "ok", "message": "聊天前端尚未构建，请运行 npm run build"}

    @app.get("/chat/{session_id}", response_model=None)
    def chat_session_index(session_id: str) -> FileResponse | dict[str, str]:
        """会话详情使用前端路由，直接访问或刷新时仍返回同一 SPA 入口。"""

        if index_file.is_file():
            return FileResponse(index_file)
        return {"status": "ok", "message": "聊天前端尚未构建，请运行 npm run build"}

    @app.get("/settings/models", response_model=None)
    def model_settings_index() -> FileResponse | dict[str, str]:
        """模型设置是独立前端路由，直接访问或刷新时返回 SPA 入口。"""

        if index_file.is_file():
            return FileResponse(index_file)
        return {"status": "ok", "message": "聊天前端尚未构建，请运行 npm run build"}

    @app.get("/api/chat/sessions")
    def list_sessions(
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        items, total = application.core.sessions.store.list_chat_sessions(
            channel=application.core.config.channels.chat.channel_name,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        return {"items": items, "total": total}

    @app.get("/api/chat/workspaces")
    def list_workspaces() -> dict[str, Any]:
        return {"items": application.core.sessions.store.list_workspaces()}

    def require_local_host(request: Request) -> None:
        client_host = request.client.host if request.client is not None else None
        if not is_loopback_client(client_host):
            raise HTTPException(
                status_code=403,
                detail="本机目录能力只允许从回环地址调用",
            )

    @app.post("/api/chat/workspaces/pick")
    def pick_workspace_directory(request: Request) -> dict[str, str | None]:
        """打开系统选择器；选择阶段不创建工作目录或会话记录。"""

        require_local_host(request)
        try:
            selected = host_directories.pick_directory()
            if selected is None:
                return {"path": None}
            resolved = application.core.sandbox_policy.validate_workspace(selected)
            return {"path": str(resolved)}
        except NativePickerBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except NativeHostUnavailable as error:
            raise HTTPException(status_code=501, detail=str(error)) from error
        except NativeHostError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (OSError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/chat/workspaces", status_code=201)
    def register_workspace(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        path = str(payload.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=400, detail="工作目录路径不能为空")
        try:
            resolved = application.core.sandbox_policy.validate_workspace(path)
            return application.core.sessions.store.create_workspace(
                str(resolved),
                str(payload.get("title") or ""),
            )
        except (OSError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.patch("/api/chat/workspaces/{workspace_id}")
    def update_workspace(
        workspace_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        allowed = {"title", "pinned"}
        if not payload or any(key not in allowed for key in payload):
            raise HTTPException(status_code=400, detail="工作区更新字段无效")
        title = payload.get("title") if "title" in payload else None
        pinned = payload.get("pinned") if "pinned" in payload else None
        if pinned is not None and not isinstance(pinned, bool):
            raise HTTPException(status_code=400, detail="pinned 必须是布尔值")
        try:
            updated = application.core.sessions.store.update_workspace(
                workspace_id,
                title=title,
                pinned=pinned,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if updated is None:
            raise HTTPException(status_code=404, detail="工作区不存在")
        return updated

    @app.post("/api/chat/workspaces/{workspace_id}/open", status_code=204)
    def open_workspace(workspace_id: str, request: Request) -> Response:
        require_local_host(request)
        workspace = application.core.sessions.store.get_workspace(workspace_id)
        if workspace is None:
            raise HTTPException(status_code=404, detail="工作区不存在")
        if not workspace["valid"]:
            raise HTTPException(status_code=409, detail="工作区目录当前不可用")
        try:
            host_directories.open_directory(Path(str(workspace["canonical_path"])))
        except NativeHostUnavailable as error:
            raise HTTPException(status_code=501, detail=str(error)) from error
        except NativeHostError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return Response(status_code=204)

    @app.delete("/api/chat/workspaces/{workspace_id}", status_code=204)
    async def unregister_workspace(workspace_id: str) -> Response:
        session_keys = application.core.sessions.store.list_workspace_session_keys(
            workspace_id
        )
        if any(
            application.core.agent_loop.is_session_busy(session_key)
            for session_key in session_keys
        ):
            raise HTTPException(
                status_code=409,
                detail="工作区仍有关联会话正在运行或排队，请结束后再移除",
            )
        if not application.core.sessions.store.delete_workspace(workspace_id):
            raise HTTPException(status_code=404, detail="工作区不存在")
        # 删除关系可能让多个缓存策略失效；Policy 每次回源，不需要全局缓存失效。
        return Response(status_code=204)

    @app.get("/api/chat/sessions/{session_key:path}/messages")
    def list_messages(
        session_key: str,
        limit: int = Query(60, ge=1, le=200),
    ) -> dict[str, Any]:
        items, total, has_more, next_before_seq = application.core.sessions.store.list_latest_chat_messages(
            session_key,
            limit=limit,
        )
        items = _append_running_snapshot(items, application.core.agent_loop.get_active_turn_snapshot(session_key))
        items = [_public_chat_message(item) for item in items]
        return {
            "items": items,
            "total": total,
            "has_more": has_more,
            "next_before_seq": next_before_seq,
        }

    @app.get("/api/chat/sessions/{session_key:path}/messages/older")
    def list_older_messages(
        session_key: str,
        before_seq: int = Query(..., ge=0),
        limit: int = Query(60, ge=1, le=200),
    ) -> dict[str, Any]:
        items, has_more, next_before_seq = application.core.sessions.store.list_chat_messages_before(
            session_key,
            before_seq=before_seq,
            limit=limit,
        )
        return {
            "items": [_public_chat_message(item) for item in items],
            "has_more": has_more,
            "next_before_seq": next_before_seq,
        }

    @app.get("/api/chat/sessions/{session_key:path}/messages/around")
    def list_messages_around(
        session_key: str,
        anchor_seq: int = Query(..., ge=0),
        limit: int = Query(60, ge=1, le=200),
    ) -> dict[str, Any]:
        items, has_before, has_after, next_before_seq = application.core.sessions.store.list_chat_messages_from(
            session_key,
            anchor_seq=anchor_seq,
            limit=limit,
        )
        return {
            "items": [_public_chat_message(item) for item in items],
            "has_before": has_before,
            "has_after": has_after,
            "next_before_seq": next_before_seq,
        }

    @app.get("/api/chat/sessions/{session_key:path}/turns")
    def list_turns(session_key: str) -> dict[str, Any]:
        return {"items": application.core.sessions.store.list_chat_turns(session_key)}

    @app.get("/api/chat/sessions/{session_key:path}/sandbox")
    def get_session_sandbox(session_key: str) -> dict[str, Any]:
        require_web_session(session_key)
        snapshot = application.core.sessions.store.get_session_sandbox(session_key)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return snapshot

    def require_web_session(session_key: str) -> None:
        """主动设置只属于当前 Web channel，禁止借 path 参数访问其他渠道。"""

        channel_name = application.core.config.channels.chat.channel_name
        if not session_key.startswith(f"{channel_name}:"):
            raise HTTPException(status_code=404, detail="会话不存在")
        if application.core.sessions.store.get_session_meta(session_key) is None:
            raise HTTPException(status_code=404, detail="会话不存在")

    @app.get("/api/chat/sessions/{session_key:path}/proactive")
    def get_proactive_settings(session_key: str) -> dict[str, Any]:
        """返回前端语义配置与只读运行状态，不暴露内部算法参数。"""

        require_web_session(session_key)
        settings = application.core.proactive_store.get_settings(session_key)
        state = application.core.proactive_store.get_state(session_key)
        return {"settings": asdict(settings), "status": asdict(state)}

    @app.put("/api/chat/sessions/{session_key:path}/proactive")
    def update_proactive_settings(
        session_key: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """整体校验后保存允许修改的字段，未知字段不会进入持久化配置。"""

        require_web_session(session_key)
        current = asdict(application.core.proactive_store.get_settings(session_key))
        editable = {
            "reminders_enabled", "reminder_quiet_policy", "conversation_enabled",
            "activity_level", "min_conversation_interval_hours",
            "daily_conversation_limit", "quiet_hours_enabled", "quiet_start",
            "quiet_end", "timezone",
        }
        for key in editable:
            if key in payload:
                current[key] = payload[key]
        current["session_key"] = session_key
        try:
            saved = application.core.proactive_store.upsert_settings(
                SessionProactiveSettings(**current)
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"settings": asdict(saved)}

    @app.get("/api/chat/sessions/{session_key:path}/reminders")
    def list_reminders(session_key: str) -> dict[str, Any]:
        """列出由对话创建的 instant/soft 任务，Web 只负责管理。"""

        require_web_session(session_key)
        jobs = application.core.proactive_store.list_jobs(session_key)
        return {"items": [_scheduled_job_payload(job) for job in jobs]}

    @app.get("/api/chat/sessions/{session_key:path}/notifications")
    def list_notifications(session_key: str) -> dict[str, Any]:
        """返回独立通知供前端合并展示，不把内容注入 Agent 会话历史。"""

        require_web_session(session_key)
        items = application.core.proactive_store.list_notifications(session_key)
        return {"items": [_notification_payload(item) for item in items]}

    @app.delete("/api/chat/sessions/{session_key:path}/reminders/{job_id}", status_code=204)
    def delete_reminder(session_key: str, job_id: str) -> Response:
        """按会话归属删除提醒，阻止跨会话 ID 猜测。"""

        require_web_session(session_key)
        if not application.core.proactive_store.delete_job(job_id, session_key=session_key):
            raise HTTPException(status_code=404, detail="提醒不存在")
        return Response(status_code=204)

    @app.patch("/api/chat/sessions/{session_key:path}")
    async def update_session(
        session_key: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """修改会话标题或目录内置顶状态，不改变对话活动时间。"""

        allowed = {"title", "pinned"}
        if len(payload) != 1 or any(key not in allowed for key in payload):
            raise HTTPException(status_code=400, detail="会话更新字段无效")
        channel = application.core.config.channels.chat.channel_name
        if not session_key.startswith(f"{channel}:"):
            raise HTTPException(status_code=404, detail="会话不存在")
        if "title" in payload:
            clean_title = str(payload.get("title") or "").strip()
            if not clean_title or len(clean_title) > 60:
                raise HTTPException(status_code=400, detail="会话标题长度必须为 1-60 个字符")
            updated = await application.core.sessions.update_title(session_key, clean_title)
            if updated is None:
                raise HTTPException(status_code=404, detail="会话不存在")
        if "pinned" in payload:
            pinned = payload.get("pinned")
            if not isinstance(pinned, bool):
                raise HTTPException(status_code=400, detail="pinned 必须是布尔值")
            try:
                updated = await asyncio.to_thread(
                    application.core.sessions.store.set_chat_session_pinned,
                    session_key,
                    pinned,
                )
            except ValueError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            if updated is None:
                raise HTTPException(status_code=404, detail="会话不存在")
        meta = application.core.sessions.store.get_session_meta(session_key)
        if meta is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return {
            "key": session_key,
            "title": str(meta.get("metadata", {}).get("title") or ""),
            "updated_at": str(meta["updated_at"]),
            "last_activity_at": str(meta.get("last_activity_at") or meta["created_at"]),
            "pinned_at": meta.get("pinned_at"),
        }

    @app.delete("/api/chat/sessions/{session_key:path}", status_code=204)
    async def delete_session(session_key: str) -> Response:
        """删除原始会话记录；长期记忆拥有独立生命周期，不随聊天页面删除。"""

        channel = application.core.config.channels.chat.channel_name
        if not session_key.startswith(f"{channel}:"):
            raise HTTPException(status_code=404, detail="会话不存在")
        await application.core.sandbox_approvals.cancel_session(session_key)
        if not await application.core.sessions.delete(session_key):
            raise HTTPException(status_code=404, detail="会话不存在")
        await application.core.sandbox_runtime.close_session(session_key)
        return Response(status_code=204)

    @app.post("/api/chat/uploads")
    async def upload_file(
        request: Request,
        filename: str = Query(default="upload.txt"),
    ) -> dict[str, str]:
        data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="上传内容不能为空")
        clean_name = Path(filename).name
        suffix = Path(clean_name).suffix.lower()
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        is_image = content_type.startswith("image/")
        is_text = content_type.startswith("text/") or suffix in _TEXT_SUFFIXES
        if not is_image and not is_text:
            raise HTTPException(status_code=415, detail="仅支持文本文件和图片")
        if is_image:
            if len(data) > _MAX_IMAGE_UPLOAD:
                raise HTTPException(status_code=413, detail="图片不能超过 10 MiB")
            try:
                with Image.open(BytesIO(data)) as image:
                    image.verify()
                    image_format = str(image.format or "").upper()
            except (UnidentifiedImageError, OSError, SyntaxError) as error:
                raise HTTPException(status_code=400, detail="图片内容无效") from error
            if image_format not in _IMAGE_FORMATS:
                raise HTTPException(status_code=415, detail="不支持该图片格式")
            suffix = mimetypes.guess_extension(content_type) or suffix or ".img"
        else:
            if len(data) > _MAX_TEXT_UPLOAD:
                raise HTTPException(status_code=413, detail="文本文件不能超过 2 MiB")
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise HTTPException(status_code=400, detail="文本文件必须使用 UTF-8") from error
            suffix = suffix if suffix in _TEXT_SUFFIXES else ".txt"

        safe_name = f"{Path(clean_name).stem or 'upload'}{suffix}"
        # 唯一目录负责防覆盖，末级保留清洗后的原文件名，保证前端和历史恢复
        # 能展示用户认识的名称；两级路径都经过 resolve/relative_to 校验。
        bucket = (upload_dir / uuid4().hex).resolve()
        bucket.relative_to(upload_dir.resolve())
        await asyncio.to_thread(bucket.mkdir, parents=True, exist_ok=False)
        stored = (bucket / safe_name).resolve()
        stored.relative_to(upload_dir.resolve())
        await asyncio.to_thread(stored.write_bytes, data)
        return {
            "filename": safe_name,
            "upload_path": str(stored),
            "upload_url": f"/api/chat/media?path={quote(str(stored), safe='')}",
            "media_type": content_type or (mimetypes.guess_type(stored.name)[0] or "application/octet-stream"),
        }

    @app.get("/api/chat/media")
    def read_media(path: str = Query(...)) -> FileResponse:
        requested = Path(path).expanduser().resolve()
        try:
            requested.relative_to(upload_dir.resolve())
        except ValueError as error:
            raise HTTPException(status_code=404, detail="文件不存在") from error
        if not requested.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(requested)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await channel.handle_websocket(websocket)

    return app


def _validate_settings_session_key(session_key: str) -> None:
    if (
        not session_key.startswith("web:")
        or not session_key[4:]
        or len(session_key) > 200
        or any(char in session_key for char in ("/", "\\", "\x00"))
    ):
        raise ModelSettingsValidationError("会话标识无效")


def _scheduled_job_payload(job: Any) -> dict[str, Any]:
    """将内部任务转换为稳定的 Web 展示字段。"""

    return {
        "id": job.id,
        "name": job.name,
        "tier": job.tier,
        "trigger": job.trigger,
        "fire_at": job.fire_at.isoformat(),
        "enabled": job.enabled,
        "status": job.status,
        "run_count": job.run_count,
        "last_error": job.last_error,
    }


def _append_running_snapshot(
    items: list[dict[str, Any]],
    snapshot: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """把运行中 Turn 的内存快照追加为虚拟消息；不改变 SQLite 历史。"""

    if not snapshot:
        return items
    turn_id = str(snapshot.get("turn_id") or "")
    if not turn_id:
        return items
    if any(str(item.get("turn_id") or "") == turn_id for item in items):
        return items
    now = datetime.now().astimezone().isoformat()
    started_at = str(snapshot.get("started_at") or "").strip() or now
    metadata = {
        "running": True,
        "request_id": str(snapshot.get("request_id") or ""),
        "started_at": started_at,
    }
    tools = []
    for tool in snapshot.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        tools.append({
            "call_id": str(tool.get("call_id") or ""),
            "name": str(tool.get("name") or "tool"),
            "arguments": tool.get("arguments") if isinstance(tool.get("arguments"), dict) else {},
            "result": str(tool.get("result_preview") or ""),
            "status": str(tool.get("status") or "running"),
        })
    return [
        *items,
        {
            "id": f"running:user:{turn_id}",
            "session_key": str(snapshot.get("session_id") or ""),
            "seq": -2,
            "role": "user",
            "content": str(snapshot.get("user_message") or ""),
            "tool_chain": [],
            "timestamp": started_at,
            "turn_id": turn_id,
            "reasoning_content": "",
            "status": "running",
            "metadata": metadata,
            "media": list(snapshot.get("user_media") or []),
        },
        {
            "id": f"running:assistant:{turn_id}",
            "session_key": str(snapshot.get("session_id") or ""),
            "seq": -1,
            "role": "assistant",
            "content": str(snapshot.get("content") or ""),
            "tool_chain": [{"calls": tools}] if tools else [],
            "timestamp": now,
            "turn_id": turn_id,
            "reasoning_content": str(snapshot.get("thinking") or ""),
            "status": "running",
            "metadata": metadata,
            "media": [],
        },
    ]


def _notification_payload(item: Any) -> dict[str, Any]:
    """将通知记录转换为前端时间线格式。"""

    return {
        "id": item.id,
        "content": item.content,
        "source": item.source,
        "source_id": item.source_id,
        "scheduled_at": item.scheduled_at.isoformat(),
        "generated_at": item.generated_at.isoformat(),
        "delivered_at": item.delivered_at.isoformat() if item.delivered_at else None,
        "status": item.status,
        "recurring": item.recurring,
    }


__all__ = ["AppRuntime", "CoreRuntime", "MemoryMaintenanceLoop", "build_core_runtime", "create_fastapi_app"]
