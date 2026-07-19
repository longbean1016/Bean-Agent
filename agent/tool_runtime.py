"""单个 Turn 的工具可见性与执行上下文。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(slots=True)
class ToolRuntimeView:
    """保存单个 Turn 的工具状态，避免会话信息写入全局注册表。

    View 只保存工具名称，不拥有工具实例。Turn 结束后丢弃该对象，搜索解锁
    状态便不会泄漏到下一条消息或其他会话。
    """

    channel: str
    chat_id: str
    session_key: str
    visible_names: set[str] = field(default_factory=set)
    visible_order: list[str] = field(default_factory=list)
    unlocked_names: set[str] = field(default_factory=set)

    @classmethod
    def create(
        cls,
        *,
        channel: str,
        chat_id: str,
        session_key: str,
        visible_names: Iterable[str] = (),
    ) -> "ToolRuntimeView":
        """按稳定顺序创建视图，调用方传集合时也不会产生随机 Schema 顺序。"""

        ordered = list(dict.fromkeys(sorted(str(name) for name in visible_names)))
        return cls(
            channel=channel,
            chat_id=chat_id,
            session_key=session_key,
            visible_names=set(ordered),
            visible_order=ordered,
        )

    @property
    def context(self) -> dict[str, str]:
        """每次返回新字典，工具不能反向修改 Turn 自身身份信息。"""

        return {
            "channel": self.channel,
            "chat_id": self.chat_id,
            "session_key": self.session_key,
        }

    def unlock(self, names: Iterable[str]) -> list[str]:
        """把仍未可见的名称追加到本 Turn，并返回本次新增项。"""

        added: list[str] = []
        for raw_name in names:
            name = str(raw_name).strip()
            if not name or name in self.visible_names:
                continue
            self.visible_names.add(name)
            self.visible_order.append(name)
            self.unlocked_names.add(name)
            added.append(name)
        return added


__all__ = ["ToolRuntimeView"]
