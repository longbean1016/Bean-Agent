"""主动聊天循环的关键防打扰边界测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from proactive.chat_loop import ProactiveChatLoop
from proactive.models import SessionProactiveSettings
from proactive.store import ProactiveStore


class _SessionStore:
    def get_session_meta(self, session_key: str):
        return {"key": session_key, "updated_at": "2026-07-20T00:00:00+00:00"}

    def list_chat_messages(self, session_key: str, *, limit: int, offset: int):
        return ([{"role": "user", "content": "请继续处理这个普通请求"}], 1)


class _NeverProvider:
    async def complete(self, *args, **kwargs):
        raise AssertionError("普通回复未结束时不应调用主动判定模型")


class _NeverDelivery:
    async def deliver(self, **kwargs):
        raise AssertionError("普通回复未结束时不应主动投递")


class _AlwaysAdmit:
    def random(self) -> float:
        return 0.0

    def randint(self, start: int, _end: int) -> int:
        return start

    def uniform(self, _start: float, _end: float) -> float:
        return 0.0


@pytest.mark.asyncio
async def test_pending_passive_turn_is_never_repackaged_as_proactive(tmp_path) -> None:
    store = ProactiveStore(tmp_path / "proactive.db")
    store.upsert_settings(SessionProactiveSettings(
        session_key="web:a",
        conversation_enabled=True,
        activity_level="active",
        min_conversation_interval_hours=1,
    ))
    loop = ProactiveChatLoop(
        store,
        _SessionStore(),
        _NeverProvider(),
        _NeverDelivery(),
        is_session_busy=lambda _key: False,
        now_fn=lambda: datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
        rng=_AlwaysAdmit(),  # type: ignore[arg-type]
    )

    await loop.run_once()

    assert store.get_state("web:a").last_skip_reason == "unfinished_passive_turn"
    await loop.close()
    store.close()
