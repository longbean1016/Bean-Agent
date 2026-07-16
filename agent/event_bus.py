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
class TurnCommitted:
    """user 与 assistant 两条消息均持久化成功后的提交事件。"""

    session_key: str
    turn_id: str
    user_message_id: str
    assistant_message_id: str
    status: str


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
    "EventBus",
    "EventHandler",
    "StreamDeltaReady",
    "ToolCallCompleted",
    "ToolCallStarted",
    "TurnCommitted",
    "TurnStarted",
]
