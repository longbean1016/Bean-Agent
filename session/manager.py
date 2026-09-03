"""对齐 akashic-agent 的 Session 缓存、历史恢复与持久化编排。"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from session.model_surface import INTERRUPTED_TOOL_RESULT_CONTENT
from session.store import NewMessage, NewSessionEvent, NewSurfaceEvent, SessionStore
from tools.base import ToolResult
from tools.runtime import serialize_tool_result_messages

_TEXT_ATTACHMENT_CHAR_BUDGET = 100_000
_TEXT_ATTACHMENT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".py", ".json", ".toml", ".yaml", ".yml",
    ".csv", ".log", ".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".xml",
}
_LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def _now_local() -> datetime:
    return datetime.now(_LOCAL_TZ)


def _rebuild_user_content(text: str, media_paths: list[str]) -> str | list[dict[str, Any]]:
    """把持久化附件恢复成模型可消费内容，不改变用户可见正文。"""

    # 这里只消费 Channel 已解析并持久化的附件路径，不接受模型生成的任意路径；
    # 上游保存附件时必须完成 workspace/path 安全校验。
    images: list[dict[str, Any]] = []
    file_refs: list[str] = []
    for path in media_paths:
        file_path = Path(path)
        mime, _ = mimetypes.guess_type(file_path)
        if mime and mime.startswith("image/") and file_path.is_file():
            try:
                encoded = base64.b64encode(file_path.read_bytes()).decode()
                images.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{encoded}"},
                    }
                )
            except OSError:
                # 附件属于历史增强信息，单个文件读取失败不能阻断整轮恢复。
                file_refs.append(f"[图片（读取失败）: {file_path.name}]")
        elif file_path.is_file() and file_path.suffix.lower() in _TEXT_ATTACHMENT_SUFFIXES:
            try:
                content = file_path.read_text(encoding="utf-8")
                if len(content) > _TEXT_ATTACHMENT_CHAR_BUDGET:
                    omitted = len(content) - _TEXT_ATTACHMENT_CHAR_BUDGET
                    content = f"{content[:_TEXT_ATTACHMENT_CHAR_BUDGET]}\n...[省略 {omitted} 个字符]"
                file_refs.append(f"[文本附件: {file_path.name}]\n```text\n{content}\n```")
            except (OSError, UnicodeDecodeError):
                # 历史恢复失败只降级为文件引用，不能破坏整个 Session 的加载。
                file_refs.append(f"[文件（读取失败）: {file_path.name}]")
        elif file_path.is_file():
            file_refs.append(f"[文件: {path}]")
        else:
            file_refs.append(f"[文件（已失效）: {file_path.name}]")

    prefix = "\n".join(file_refs) + "\n" if file_refs else ""
    combined_text = (prefix + text).strip()
    if not images:
        return combined_text
    return images + [{"type": "text", "text": combined_text}]


@dataclass(slots=True)
class Session:
    """单次对话的完整内存快照，与 akashic 一样缓存全部消息。"""

    session_key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now_local)
    updated_at: datetime = field(default_factory=_now_local)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0

    @property
    def key(self) -> str:
        """保留 akashic 的 `session.key` 使用习惯。"""

        return self.session_key

    def add_message(
        self,
        role: str,
        content: str,
        media: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """先追加内存消息；Manager 持久化后会原地补齐 id 和 seq。"""

        message: dict[str, Any] = {
            "role": role,
            "content": content,
            "timestamp": _now_local().isoformat(),
            **kwargs,
        }
        if media:
            message["media"] = list(media)
        self.messages.append(message)
        self.updated_at = _now_local()
        return message

    def get_history(
        self,
        max_messages: int = 500,
        *,
        start_index: int | None = None,
        boundary_floor: int = 0,
    ) -> list[dict[str, Any]]:
        """从完整消息缓存构建 OpenAI 格式历史，并保持完整 user Turn。

        boundary_floor 是已压缩区的右边界。窗口可以在活动区内向前补齐，
        但不能跨过该边界重新加载已由近期摘要承接的原文。
        """

        if start_index is not None:
            if max_messages <= 0:
                return []
            floor = max(0, min(int(boundary_floor), len(self.messages)))
            start = max(floor, int(start_index))
            if start >= len(self.messages):
                return []
            # 窗口落在 assistant 上时向前回退到最近 user，不能截断其工具链。
            while start > floor and self.messages[start].get("role") != "user":
                start -= 1
            messages = self.messages[start:]
            # cursor 异常落在 assistant 上时，不能越过 cursor 找已压缩 user；
            # 丢弃孤立前缀直到下一个合法 Turn，比发送残缺工具链更安全。
            while messages and messages[0].get("role") != "user":
                messages = messages[1:]
        elif max_messages <= 0:
            messages = []
        else:
            messages = self.messages[-max_messages:]

        history: list[dict[str, Any]] = []
        projected_turn_ids: set[str] = set()
        for message in messages:
            role = message.get("role")
            if role == "user":
                surface_messages = message.get("llm_surface_messages")
                if isinstance(surface_messages, list) and surface_messages:
                    history.extend(
                        deepcopy(
                            [
                                item
                                for item in surface_messages
                                if isinstance(item, dict)
                            ]
                        )
                    )
                    turn_id = str(message.get("turn_id") or "")
                    if turn_id:
                        projected_turn_ids.add(turn_id)
                    continue
                content: object = message.get("llm_user_content")
                if content is None:
                    text = str(message.get("content", ""))
                    media = message.get("media") or []
                    content = _rebuild_user_content(text, list(media)) if media else text
                frame = message.get("llm_context_frame")
                if isinstance(frame, str) and frame.strip():
                    history.append({"role": "user", "content": frame})
                history.append({"role": "user", "content": content})
                continue
            if role != "assistant":
                continue
            if str(message.get("turn_id") or "") in projected_turn_ids:
                # provider surface 已包含该 Turn 的 assistant/tool 消息；语义
                # assistant 只供 UI 和记忆使用，不能再次展开到模型历史。
                continue

            interrupted = message.get("status") == "interrupted"
            for group in message.get("tool_chain") or []:
                calls = group.get("calls") or []
                if interrupted:
                    # 中断轮的语义快照保留完整审计数据；模型历史也保留已发出的
                    # tool-call，并用确定性的占位结果闭合尚未完成的调用。
                    calls = [
                        call for call in calls
                        if isinstance(call, dict)
                        and str(call.get("status") or "")
                        in {"ok", "completed", "error", "running", "interrupted"}
                    ]
                if not calls:
                    continue
                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": group.get("text"),
                    "tool_calls": [
                        {
                            "id": call["call_id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(
                                    call.get("arguments", {}), ensure_ascii=False
                                ),
                            },
                        }
                        for call in calls
                    ],
                }
                provider_fields = group.get("provider_fields")
                reasoning = (
                    provider_fields.get("reasoning_content")
                    if isinstance(provider_fields, dict)
                    else group.get("reasoning_content")
                )
                if isinstance(reasoning, str):
                    assistant_message["reasoning_content"] = reasoning
                history.append(assistant_message)
                for call in calls:
                    interrupted_call = interrupted and str(call.get("status") or "") in {
                        "running", "interrupted"
                    }
                    content_blocks = call.get("content_blocks")
                    tool_content: str | ToolResult = (
                        INTERRUPTED_TOOL_RESULT_CONTENT
                        if interrupted_call
                        else str(call.get("result", ""))
                    )
                    if not interrupted_call and isinstance(content_blocks, list) and content_blocks:
                        tool_content = ToolResult(
                            text=str(call.get("result", "")),
                            content_blocks=[
                                dict(block)
                                for block in content_blocks
                                if isinstance(block, dict)
                            ],
                        )
                    history.extend(
                        serialize_tool_result_messages(
                            tool_call_id=str(call.get("call_id", "")),
                            content=tool_content,
                            tool_name=str(call.get("name", "")) or None,
                        )
                    )

            final_message: dict[str, Any] = {
                "role": "assistant",
                "content": message.get("content", "") or "",
            }
            reasoning = message.get("reasoning_content")
            if not interrupted and isinstance(reasoning, str):
                final_message["reasoning_content"] = reasoning
            history.append(final_message)
        return history

    def clear(self) -> None:
        """清空内存会话并重置记忆归档 cursor。"""

        self.messages = []
        self.updated_at = _now_local()
        self.last_consolidated = 0


class SessionManager:
    """管理完整 Session 缓存，并按 session_key 串行同步至 SQLite。

    与 akashic-agent 一致，Manager 拥有 workspace 下的 Session 目录和数据库，
    同时把完整消息列表保存在内存 Session 中。调用方修改 Session 后必须调用
    ``append_messages()`` 或 ``save_async()``，这样内存对象和 SQLite 才会在
    同一个会话锁保护下完成同步。
    """

    _METADATA_REFRESH_EVERY = 10

    def __init__(self, workspace: Path, history_window: int | None = None) -> None:
        # 兼容旧调用方的参数形状，但它不再参与历史加载；活动边界只由
        # checkpoint.consolidated_through_seq 决定，limit=None 才是主链路语义。
        if history_window is not None and int(history_window) <= 0:
            raise ValueError("history_window 必须大于 0")
        # 对齐 akashic 的目录布局：sessions/ 为后续会话附件或导出能力预留，
        # 结构化会话和消息统一写入 workspace 根目录下的 sessions.db。
        self.workspace = Path(workspace)
        self.session_dir = self.workspace / "sessions"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workspace / "sessions.db"
        self._store = SessionStore(self.db_path)
        # cache 保存完整 Session 而非只保存元数据。命中缓存时历史生成不访问
        # SQLite；invalidate 后才从数据库重新构建完整消息快照。
        self._cache: dict[str, Session] = {}
        # 已删除键用于拦截仍持有旧 Session 对象的迟到 Turn，避免删除后被异步回写重新创建。
        self._deleted_keys: set[str] = set()

        # 每个 session_key 独享一把异步锁。同会话的 ID 分配、消息回填和元数据
        # 更新必须保持顺序，不同会话则不应被一把全局异步锁互相阻塞。
        self._write_locks: dict[str, asyncio.Lock] = {}
        self._close_lock = asyncio.Lock()
        self._closed = False

    async def get_or_create(self, session_key: str) -> Session:
        """优先返回完整缓存；未命中时从 SQLite 重建 Session。"""

        self._ensure_open()
        key = self._validate_session_key(session_key)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        async with self._lock_for(key):
            # 等待锁期间，另一个任务可能已经完成加载；必须再次检查缓存，
            # 否则同一会话会产生两个可变 Session 对象。
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            # 与 akashic 一致，这些 sqlite3 调用在当前事件循环线程同步完成。
            meta = self._store.get_session_meta(key)
            messages = self._store.fetch_session_messages(key)
            if meta is None and not messages:
                # 新会话先落元数据，确保随后第一条消息可以满足外键约束。
                meta = self._store.create_session(key)
            now = _now_local()
            session = Session(
                session_key=key,
                messages=messages,
                created_at=_parse_datetime(meta["created_at"]) if meta else now,
                updated_at=_parse_datetime(meta["updated_at"]) if meta else now,
                metadata=dict(meta.get("metadata", {})) if meta else {},
                last_consolidated=int(meta.get("last_consolidated", 0)) if meta else 0,
            )
            self._cache[key] = session
            return session

    async def peek_next_message_id(self, session_key: str) -> str:
        """预测当前会话下一条持久化消息 ID，供 Turn 内工具记录原文来源。"""

        key = self._validate_session_key(session_key)
        # 先确保 Session 元数据存在，再读取由消息写事务维护的 next_seq；不能用
        # 缓存消息数量推算，因为删除、恢复或中断补写后两者不一定相等。
        await self.get_or_create(key)
        async with self._lock_for(key):
            meta = self._store.get_session_meta(key)
            next_seq = int(meta.get("next_seq", 0)) if meta is not None else 0
        return f"{key}:{next_seq}"

    async def save_async(self, session: Session) -> None:
        """保存完整 Session 中所有尚未落库的消息。

        该入口对应 akashic 的 save_async，适合调用方已经连续修改多条消息或
        metadata/cursor 后统一保存。已有 ID 的消息不会重复插入。
        """

        self._ensure_open()
        async with self._lock_for(session.key):
            await self._persist_messages(session, session.messages)
            await self._save_metadata(session)
            self._cache[session.key] = session

    async def append_messages(
        self,
        session: Session,
        messages: list[dict[str, Any]],
    ) -> None:
        """只提交本轮新增消息，并原地回写稳定 ID。

        该入口对应 akashic 的 append_messages。调用方应先通过
        ``Session.add_message()`` 把同一批字典加入 Session，再把这些字典传入；
        持久化完成后 TurnCommitted 可以直接读取其 ``id``。
        """

        self._ensure_open()
        # 复制容器避免调用方在 await 期间增删原列表；消息字典本身不能深拷贝，
        # 因为持久化成功后需要在原对象上回填 id、seq 和 timestamp。
        copied = list(messages)
        async with self._lock_for(session.key):
            self._ensure_not_deleted(session.key)
            await self._persist_messages(session, copied)
            await self._save_metadata(session)
            self._cache[session.key] = session

    async def append_surface(self, event: NewSurfaceEvent) -> dict[str, Any]:
        """在会话锁内追加模型侧 surface，并保证序号和幂等键隔离。"""

        self._ensure_open()
        key = self._validate_session_key(event.session_key)
        async with self._lock_for(key):
            self._ensure_not_deleted(key)
            return await asyncio.to_thread(self._store.append_surface, event)

    async def append_session_event(self, event: NewSessionEvent) -> dict[str, Any]:
        """在会话锁内追加模型事件日志，chunk 与边界不进入语义消息。"""

        self._ensure_open()
        key = self._validate_session_key(event.session_key)
        async with self._lock_for(key):
            self._ensure_not_deleted(key)
            return await asyncio.to_thread(self._store.append_session_event, event)

    async def fetch_session_events(self, session_key: str) -> list[dict[str, Any]]:
        """按事件序号读取模型事件日志，供恢复器和诊断使用。"""

        self._ensure_open()
        key = self._validate_session_key(session_key)
        async with self._lock_for(key):
            return await asyncio.to_thread(self._store.fetch_session_events, key)

    async def get_turn_timing(self, session_key: str, turn_id: str) -> dict[str, Any]:
        """读取持久化 Turn 起止时间，供语义消息和 API 共用。"""

        self._ensure_open()
        key = self._validate_session_key(session_key)
        async with self._lock_for(key):
            return await asyncio.to_thread(self._store.get_turn_timing, key, turn_id)

    async def load_surface(
        self,
        session_key: str,
        *,
        epoch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """读取当前会话的模型消息投影，不触碰语义消息缓存。"""

        self._ensure_open()
        key = self._validate_session_key(session_key)
        async with self._lock_for(key):
            return await asyncio.to_thread(
                self._store.load_surface,
                key,
                epoch_id=epoch_id,
            )

    async def fetch_surface_events(
        self,
        session_key: str,
        *,
        epoch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """读取 surface 原始事件，供诊断和恢复流程使用。"""

        self._ensure_open()
        key = self._validate_session_key(session_key)
        async with self._lock_for(key):
            return await asyncio.to_thread(
                self._store.fetch_surface_events,
                key,
                epoch_id=epoch_id,
            )

    async def load_surface_events(self, session_key: str) -> list[dict[str, Any]]:
        """读取当前折叠后的 surface 节点，包含替换边界和序号。"""

        self._ensure_open()
        key = self._validate_session_key(session_key)
        async with self._lock_for(key):
            return await asyncio.to_thread(self._store.load_surface_events, key)

    async def replace_surface(self, event: NewSurfaceEvent) -> dict[str, Any]:
        """在会话锁内提交一个带边界的模型侧 surface replace。"""

        self._ensure_open()
        key = self._validate_session_key(event.session_key)
        async with self._lock_for(key):
            self._ensure_not_deleted(key)
            return await asyncio.to_thread(self._store.replace_surface, event)

    async def recover_surface(self, session_key: str) -> list[dict[str, Any]]:
        """读取当前会话待恢复的 surface 事件。"""

        self._ensure_open()
        key = self._validate_session_key(session_key)
        async with self._lock_for(key):
            return await asyncio.to_thread(self._store.recover_surface, key)

    async def load_history(
        self,
        session_key: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """从 cursor 后的活动 Session 生成历史，并向前补齐 user Turn。

        limit 表示基础消息窗口，不是严格返回数量。若窗口落在 assistant 上，
        会向前扩展到最近 user，确保工具调用与最终回复不会脱离所属 Turn。
        """

        session = await self.get_or_create(session_key)
        actual_limit = len(session.messages) if limit is None else max(0, int(limit))
        # MemoryEngine 直接持有同一个 Store 并在后台推进 generation；缓存 Session
        # 不会自动收到该变更，因此每次模型加载前以 ledger 的消息边界为权威。
        # Session 快照仍回写 generation 指针，供旧的 UI/测试观察；真正的消息边界
        # 单独从 active checkpoint 读取，不能再把这两个数混作一个 cursor。
        session.last_consolidated = self._store.get_cursor(session.key)
        cursor = max(
            0,
            min(self._store.get_active_message_boundary(session.key), len(session.messages)),
        )
        start = max(cursor, len(session.messages) - actual_limit)
        return session.get_history(
            max_messages=actual_limit,
            start_index=start,
            boundary_floor=cursor,
        )

    def invalidate(self, session_key: str) -> None:
        """丢弃整个 Session 缓存，下次访问从 SQLite 完整重载。"""

        self._ensure_open()
        self._cache.pop(self._validate_session_key(session_key), None)

    async def update_title(self, session_key: str, title: str) -> dict[str, Any] | None:
        """在会话锁内持久化标题，并同步缓存以防后续保存覆盖新 metadata。"""

        key = self._validate_session_key(session_key)
        async with self._lock_for(key):
            updated = await asyncio.to_thread(
                self._store.update_chat_session_title,
                key,
                title,
            )
            if updated is None:
                return None
            cached = self._cache.get(key)
            if cached is not None:
                cached.metadata["title"] = str(title).strip()
            return updated

    async def ensure_default_title(
        self,
        session_key: str,
        content: str,
        media: list[str],
    ) -> dict[str, Any] | None:
        """确保首条用户消息已有默认目录标题，并返回前端可用的会话摘要。"""

        key = self._validate_session_key(session_key)
        async with self._lock_for(key):
            updated = await asyncio.to_thread(
                self._store.ensure_default_chat_session_title,
                key,
                content,
                list(media),
            )
            if updated is None:
                return None
            cached = self._cache.get(key)
            if cached is not None and not str(cached.metadata.get("title") or "").strip():
                cached.metadata["title"] = str(updated.get("title") or "")
            return updated

    async def delete(self, session_key: str) -> bool:
        """删除会话并封锁旧对象的后续回写；长期记忆由独立组件持有，不参与删除。"""

        key = self._validate_session_key(session_key)
        async with self._lock_for(key):
            deleted = await asyncio.to_thread(self._store.delete_chat_session, key)
            if deleted:
                self._deleted_keys.add(key)
                self._cache.pop(key, None)
            return deleted

    async def delete_if_empty(self, session_key: str) -> bool:
        """清理没有消息的临时会话；有消息的会话不会被此入口删除。"""

        key = self._validate_session_key(session_key)
        async with self._lock_for(key):
            deleted = await asyncio.to_thread(
                self._store.delete_empty_chat_session,
                key,
            )
            if deleted:
                self._cache.pop(key, None)
            return deleted

    async def close(self) -> None:
        """幂等关闭 Store，并释放完整消息缓存和会话锁。"""

        async with self._close_lock:
            if self._closed:
                return
            # 先关闭 SQLite，再标记 Manager 已关闭；关闭失败时上层仍可重试。
            # Store 成功关闭后清缓存，避免关闭期间仍有对象被误认为可持久化。
            self._store.close()
            self._closed = True
            self._cache.clear()
            self._deleted_keys.clear()
            self._write_locks.clear()

    @property
    def store(self) -> SessionStore:
        return self._store

    async def _persist_messages(
        self,
        session: Session,
        messages: list[dict[str, Any]],
    ) -> None:
        # 与 akashic 一致：已有 id 表示消息已落库，重复 save 时必须跳过。
        for message in messages:
            if message.get("id"):
                continue
            content = message.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            metadata = message.get("metadata")
            clean_metadata = dict(metadata) if isinstance(metadata, dict) else {}
            fixed_fields = {
                "id", "session_key", "seq", "role", "content", "timestamp",
                "tool_chain", "turn_id", "reasoning_content", "status", "metadata",
            }
            # 与 akashic 的 _extract_extra 一致：附件和供应商兼容字段不设
            # 独立列，统一放入 extra JSON，重载时恢复到消息顶层。
            extra = {
                key: value for key, value in message.items() if key not in fixed_fields
            }
            row = self._store.add_message(
                NewMessage(
                    session_key=session.key,
                    role=str(message.get("role") or "assistant"),
                    content=content,
                    turn_id=str(message.get("turn_id", "")),
                    tool_chain=list(message.get("tool_chain") or []),
                    reasoning_content=str(message.get("reasoning_content", "")),
                    status=str(message.get("status", "ok")),
                    timestamp=str(message.get("timestamp") or "") or None,
                    metadata=clean_metadata,
                    extra=extra,
                )
            )
            if str(message.get("role") or "") == "user" and not session.metadata.get("title"):
                # Store 在首条用户消息事务内生成标题；这里同步缓存，防止随后的
                # metadata upsert 用旧内存快照覆盖刚生成的默认标题。
                stored_meta = self._store.get_session_meta(session.key)
                stored_title = (
                    stored_meta.get("metadata", {}).get("title")
                    if stored_meta is not None
                    else ""
                )
                if stored_title:
                    session.metadata["title"] = str(stored_title)
            # 原地更新很关键：调用方持有的消息字典立即获得稳定 ID，后续
            # TurnCommitted 和重复保存都以同一对象为准。
            message.update(row)
        session.updated_at = _now_local()

    async def _save_metadata(self, session: Session) -> None:
        # 消息写入和 Session 元数据是两个明确步骤：Store.add_message 原子维护
        # next_seq，本方法只刷新 updated_at、cursor 和业务 metadata。
        self._store.upsert_session(
            session.key,
            created_at=session.created_at.isoformat(),
            updated_at=session.updated_at.isoformat(),
            last_consolidated=session.last_consolidated,
            metadata=session.metadata,
        )

    def _lock_for(self, session_key: str) -> asyncio.Lock:
        # 同一会话的内存缓存和 SQLite 顺序必须一致；不同会话仍可并行。
        lock = self._write_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._write_locks[session_key] = lock
        return lock

    @staticmethod
    def _validate_session_key(session_key: str) -> str:
        key = str(session_key).strip()
        if not key:
            raise ValueError("session_key 不能为空")
        return key

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SessionManager 已关闭")

    def _ensure_not_deleted(self, session_key: str) -> None:
        if session_key in self._deleted_keys:
            raise RuntimeError(f"Session 已删除，拒绝迟到写入: {session_key}")


def _parse_datetime(value: object) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"无效的 Session 时间戳: {value!r}") from error


__all__ = ["Session", "SessionManager"]
