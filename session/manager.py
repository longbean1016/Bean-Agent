"""对齐 akashic-agent 的 Session 缓存、历史恢复与持久化编排。"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from session.store import NewMessage, SessionStore

_TOOL_RESULT_CHAR_BUDGET = 10_000


def _truncate_tool_result(content: object) -> str:
    """按 akashic 的预算保留工具结果首尾，避免历史被单次输出占满。"""

    text = content if isinstance(content, str) else str(content)
    if len(text) <= _TOOL_RESULT_CHAR_BUDGET:
        return text
    omitted = len(text) - _TOOL_RESULT_CHAR_BUDGET
    while True:
        marker = f"…{omitted} chars truncated…"
        keep = max(0, _TOOL_RESULT_CHAR_BUDGET - len(marker))
        actual_omitted = len(text) - keep
        if actual_omitted == omitted:
            break
        omitted = actual_omitted
    head = keep // 2
    tail = keep - head
    truncated = text[:head] + marker + (text[-tail:] if tail else "")
    return f"Total output lines: {len(text.splitlines())}\n\n{truncated}"


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
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
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
            "timestamp": datetime.now().astimezone().isoformat(),
            **kwargs,
        }
        if media:
            message["media"] = list(media)
        self.messages.append(message)
        self.updated_at = datetime.now()
        return message

    def get_history(
        self,
        max_messages: int = 500,
        *,
        start_index: int | None = None,
    ) -> list[dict[str, Any]]:
        """从完整消息缓存构建 OpenAI 格式历史，并保持完整 user Turn。"""

        if start_index is not None:
            if max_messages <= 0:
                return []
            start = max(0, int(start_index))
            if start >= len(self.messages):
                return []
            # 窗口落在 assistant 上时向前回退到最近 user，不能截断其工具链。
            while start > 0 and self.messages[start].get("role") != "user":
                start -= 1
            messages = self.messages[start:]
            while messages and messages[0].get("role") != "user":
                messages = messages[1:]
        elif max_messages <= 0:
            messages = []
        else:
            messages = self.messages[-max_messages:]

        history: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "user":
                content: object = message.get("llm_user_content")
                if content is None:
                    text = str(message.get("content", ""))
                    media = message.get("media") or []
                    content = _rebuild_user_content(text, list(media)) if media else text
                history.append({"role": "user", "content": content})
                continue
            if role != "assistant":
                continue

            for group in message.get("tool_chain") or []:
                calls = group.get("calls") or []
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
                reasoning = group.get("reasoning_content")
                if isinstance(reasoning, str):
                    assistant_message["reasoning_content"] = reasoning
                history.append(assistant_message)
                for call in calls:
                    history.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["call_id"],
                            "content": _truncate_tool_result(call.get("result", "")),
                        }
                    )

            final_message: dict[str, Any] = {
                "role": "assistant",
                "content": message.get("content", "") or "",
            }
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str):
                final_message["reasoning_content"] = reasoning
            history.append(final_message)
        return history

    def clear(self) -> None:
        """清空内存会话并重置记忆归档 cursor。"""

        self.messages = []
        self.updated_at = datetime.now()
        self.last_consolidated = 0


class SessionManager:
    """管理完整 Session 缓存，并按 session_key 串行同步至 SQLite。

    与 akashic-agent 一致，Manager 拥有 workspace 下的 Session 目录和数据库，
    同时把完整消息列表保存在内存 Session 中。调用方修改 Session 后必须调用
    ``append_messages()`` 或 ``save_async()``，这样内存对象和 SQLite 才会在
    同一个会话锁保护下完成同步。
    """

    _METADATA_REFRESH_EVERY = 10

    def __init__(self, workspace: Path, history_window: int = 40) -> None:
        if history_window <= 0:
            raise ValueError("history_window 必须大于 0")

        # 对齐 akashic 的目录布局：sessions/ 为后续会话附件或导出能力预留，
        # 结构化会话和消息统一写入 workspace 根目录下的 sessions.db。
        self.workspace = Path(workspace)
        self.session_dir = self.workspace / "sessions"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workspace / "sessions.db"
        self._store = SessionStore(self.db_path)
        self._history_window = int(history_window)

        # cache 保存完整 Session 而非只保存元数据。命中缓存时历史生成不访问
        # SQLite；invalidate 后才从数据库重新构建完整消息快照。
        self._cache: dict[str, Session] = {}

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
            now = datetime.now()
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
            await self._persist_messages(session, copied)
            await self._save_metadata(session)
            self._cache[session.key] = session

    async def load_history(
        self,
        session_key: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """从完整 Session 缓存生成最近历史，并向前补齐 user Turn。

        limit 表示基础消息窗口，不是严格返回数量。若窗口落在 assistant 上，
        会向前扩展到最近 user，确保工具调用与最终回复不会脱离所属 Turn。
        """

        session = await self.get_or_create(session_key)
        actual_limit = self._history_window if limit is None else max(0, int(limit))
        start = max(0, len(session.messages) - actual_limit)
        return session.get_history(max_messages=actual_limit, start_index=start)

    def invalidate(self, session_key: str) -> None:
        """丢弃整个 Session 缓存，下次访问从 SQLite 完整重载。"""

        self._ensure_open()
        self._cache.pop(self._validate_session_key(session_key), None)

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
            # 原地更新很关键：调用方持有的消息字典立即获得稳定 ID，后续
            # TurnCommitted 和重复保存都以同一对象为准。
            message.update(row)
        session.updated_at = datetime.now()

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


def _parse_datetime(value: object) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"无效的 Session 时间戳: {value!r}") from error


__all__ = ["Session", "SessionManager"]
