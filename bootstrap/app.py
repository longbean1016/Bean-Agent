"""BeanAgent 应用依赖组装与生命周期容器。"""

from __future__ import annotations

import asyncio
import mimetypes
import logging
from datetime import datetime
from io import BytesIO
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from uuid import uuid4

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError

from agent.agent_loop import AgentLoop
from agent.channel import WebChannel
from agent.config_models import Config
from agent.event_bus import EventBus
from agent.message_bus import MessageBus
from agent.mcp.manage_tools import McpAddTool, McpListTool, McpRemoveTool
from agent.mcp.registry import McpServerRegistry
from agent.pipeline import Pipeline
from agent.prompt_assembler import MessageEnvelopeBuilder, PromptAssembler
from agent.prompt_block import SectionCache, SystemPromptBuilder, default_prompt_blocks
from agent.prompt_cache_log import PromptCacheLogWriter
from agent.provider import LLMProvider, create_vision_provider
from agent.skills import SkillsLoader
from memory.embedder import Embedder
from memory.engine import MemoryEngine
from session.manager import SessionManager
from tools import ToolRegistry, register_all

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
            self._started = True

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            if self._shutdown:
                return
            self._shutdown = True

            # 顺序与参考 AppRuntime 一致：先阻止新工作进入，再等待/取消执行任务，
            # 最后按所有权从上层服务向底层 HTTP/SQLite 资源释放。
            await _cleanup_step("web_channel.close", self.channel.close)
            self.core.agent_loop.stop()
            if self.agent_task is not None and not self.agent_task.done():
                self.agent_task.cancel()
                await asyncio.gather(self.agent_task, return_exceptions=True)
            await _cleanup_step("mcp_registry.shutdown", self.core.mcp_registry.shutdown)
            if self.maintenance is not None:
                await _cleanup_step("memory_maintenance.close", self.maintenance.close)
            if self.core.memory is not None:
                await _cleanup_step("memory.close", self.core.memory.close)
            await _cleanup_step("sessions.close", self.core.sessions.close)
            if self.core.vision_provider is not None:
                await _cleanup_step("vision_provider.close", self.core.vision_provider.close)
            await _cleanup_step("provider.close", self.core.provider.close)


async def _cleanup_step(name: str, callback: Any) -> None:
    try:
        await callback()
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
    skills = SkillsLoader(root)
    prompt_cache_log = PromptCacheLogWriter(root)
    tools = ToolRegistry()
    register_all(
        tools,
        allowed_dir=workdir,
        multimodal=config.llm.multimodal,
        vl_provider=vision_provider,
        vl_model=config.llm.vl.model if config.llm.vl else "",
        session_store=sessions.store,
        memory_engine=memory,
        skills=skills,
    )
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
        history_limit=config.session.history_window,
        max_iterations=config.llm.max_iterations or 10,
        # 主模型和独立视觉模型是两条互斥的图片消费路径：前者直接接收图片块，
        # 后者只通过 read_image_vision 工具读取本地上传路径。
        multimodal=config.llm.multimodal,
        vl_available=vision_provider is not None,
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
        mcp_registry=mcp_registry,
        message_bus=messages,
        event_bus=events,
        assembler=assembler,
        pipeline=pipeline,
        agent_loop=agent_loop,
        vision_provider=vision_provider,
    )


def create_fastapi_app(runtime: CoreRuntime | AppRuntime) -> FastAPI:
    """为已组装 Runtime 暴露 WebSocket 路由，不复制或隐式替换依赖。"""

    application = runtime if isinstance(runtime, AppRuntime) else AppRuntime(runtime)

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

    @app.get("/", response_model=None)
    def chat_index() -> FileResponse | dict[str, str]:
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

    @app.get("/api/chat/sessions/{session_key:path}/messages")
    def list_messages(
        session_key: str,
        page: int = Query(1, ge=1),
        page_size: int = Query(100, ge=1, le=500),
    ) -> dict[str, Any]:
        items, total = application.core.sessions.store.list_chat_messages(
            session_key,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        return {"items": items, "total": total}

    @app.patch("/api/chat/sessions/{session_key:path}")
    async def rename_session(
        session_key: str,
        title: str = Body(embed=True),
    ) -> dict[str, str]:
        """修改会话展示标题；原始消息和长期记忆均不参与该操作。"""

        clean_title = str(title).strip()
        if not clean_title or len(clean_title) > 60:
            raise HTTPException(status_code=400, detail="会话标题长度必须为 1-60 个字符")
        channel = application.core.config.channels.chat.channel_name
        if not session_key.startswith(f"{channel}:"):
            raise HTTPException(status_code=404, detail="会话不存在")
        updated = await application.core.sessions.update_title(session_key, clean_title)
        if updated is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return {"key": session_key, "title": clean_title}

    @app.delete("/api/chat/sessions/{session_key:path}", status_code=204)
    async def delete_session(session_key: str) -> Response:
        """删除原始会话记录；长期记忆拥有独立生命周期，不随聊天页面删除。"""

        channel = application.core.config.channels.chat.channel_name
        if not session_key.startswith(f"{channel}:"):
            raise HTTPException(status_code=404, detail="会话不存在")
        if not await application.core.sessions.delete(session_key):
            raise HTTPException(status_code=404, detail="会话不存在")
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


__all__ = ["AppRuntime", "CoreRuntime", "MemoryMaintenanceLoop", "build_core_runtime", "create_fastapi_app"]
