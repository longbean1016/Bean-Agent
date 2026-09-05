"""BeanAgent 最小生命周期事件总线。"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeAlias, TypeVar, cast

logger = logging.getLogger(__name__)

E = TypeVar("E")
EventHandler: TypeAlias = Callable[[E], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class TurnStarted:
    session_key: str
    turn_id: str
    request_id: str
    content: str


@dataclass(frozen=True, slots=True)
class ContextCompactionStarted:
    """当前 Turn 已命中 token gate，正在等待 checkpoint 摘要。"""

    session_key: str
    turn_id: str
    trigger: str
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class ContextCompactionCompleted:
    """checkpoint 摘要和 ledger 提交完成，当前 Turn 可以继续请求业务模型。"""

    session_key: str
    turn_id: str
    trigger: str
    estimated_tokens: int
    compacted: bool


@dataclass(frozen=True, slots=True)
class ContextCompactionFailed:
    """checkpoint 压缩失败；当前 Turn 会沿用既有错误处理。"""

    session_key: str
    turn_id: str
    trigger: str
    estimated_tokens: int
    error: str


@dataclass(frozen=True, slots=True)
class ContextUsageUpdated:
    """上下文压力、投影和组成明细的观测快照。"""

    session_key: str
    turn_id: str
    used_tokens: int
    context_window: int
    soft_limit_tokens: int
    hard_input_tokens: int
    context_window_source: str
    estimate_source: str
    breakdown: dict[str, int]
    sections: tuple[dict[str, object], ...] = ()
    # pressure 只接受供应商输入 usage；projected 是其相对当前 Prompt 表面的
    # 估算投影。两者分开，避免近似明细被误当成圆圈的精确主指标。
    pressure_tokens: int | None = None
    projected_tokens: int | None = None
    surface_tokens: int | None = None
    system_tokens: int | None = None
    tools_tokens: int | None = None
    message_tokens: int | None = None
    as_of_seq: int | None = None
    model_runtime_id: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class SessionUsageUpdated:
    """一次真实 Provider usage 去重后，会话累计用量发生变化。"""

    session_key: str
    turn_id: str
    total_uncached_input_tokens: int
    total_cache_read_tokens: int
    total_cache_write_tokens: int
    total_input_tokens: int
    cache_hit_rate: float | None
    total_output_tokens: int


@dataclass(frozen=True, slots=True)
class TurnQueued:
    """普通 Turn 已进入全局等待队列，position 从 1 开始。"""

    session_key: str
    request_id: str
    position: int


@dataclass(frozen=True, slots=True)
class TurnQueueRejected:
    """消息因同会话忙碌、队列已满或关闭而未被调度。"""

    session_key: str
    request_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class StreamDeltaReady:
    session_key: str
    turn_id: str
    content_delta: str = ""
    thinking_delta: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    session_key: str
    turn_id: str
    call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    session_key: str
    turn_id: str
    call_id: str
    tool_name: str
    status: str
    result_preview: str


@dataclass(frozen=True, slots=True)
class SandboxApprovalRequested:
    """工具越权后等待当前会话的 Web 用户给出一次性决定。"""

    session_key: str
    request: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TurnCommitted:
    """user 与 assistant 两条消息均持久化成功后的提交事件。"""

    session_key: str
    turn_id: str
    user_message_id: str
    assistant_message_id: str
    status: str


@dataclass(frozen=True, slots=True)
class SessionUpdated:
    """会话目录摘要已更新，可安全推送给前端刷新标题和排序。"""

    session_key: str
    session: dict[str, Any]


class EventBus:
    """按注册顺序派发生命周期事件，并隔离单个观察者故障。"""

    def __init__(self) -> None:
        self._handlers: dict[type[object], list[EventHandler[object]]] = {}

    def on(self, event_type: type[E], handler: EventHandler[E]) -> None:
        # 复制参考实现的有序注册语义；允许同一 handler 多次注册，off 每次只移除一项，
        # 使订阅列表的行为与普通 list 一致且容易审计。
        handlers = self._handlers.setdefault(cast(type[object], event_type), [])
        handlers.append(cast(EventHandler[object], handler))

    def off(self, event_type: type[E], handler: EventHandler[E]) -> None:
        handlers = self._handlers.get(cast(type[object], event_type))
        if not handlers:
            return
        try:
            handlers.remove(cast(EventHandler[object], handler))
        except ValueError:
            return
        if not handlers:
            self._handlers.pop(cast(type[object], event_type), None)

    async def emit(self, event: object) -> None:
        # 使用快照遍历，允许 handler 在回调中安全订阅或取消；变更从下一次 emit 生效。
        for handler in list(self._handlers.get(type(event), ())):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # Turn 和前端发送可能已经完成，观察者失败不能破坏其它 handler 或回滚主链。
                logger.exception(
                    "事件处理器执行失败: event=%s handler=%s",
                    type(event).__name__,
                    _handler_name(handler),
                )


def _handler_name(handler: EventHandler[object]) -> str:
    return str(getattr(handler, "__qualname__", getattr(handler, "__name__", repr(handler))))


__all__ = [
    "ContextCompactionCompleted",
    "ContextCompactionFailed",
    "ContextCompactionStarted",
    "ContextUsageUpdated",
    "SessionUsageUpdated",
    "EventBus",
    "EventHandler",
    "SessionUpdated",
    "SandboxApprovalRequested",
    "StreamDeltaReady",
    "ToolCallCompleted",
    "ToolCallStarted",
    "TurnCommitted",
    "TurnQueued",
    "TurnQueueRejected",
    "TurnStarted",
]
