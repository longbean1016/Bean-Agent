"""Web 命令服务的传输无关契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.agent_loop import InterruptResult
from agent.message_bus import MessageBus
from agent.web_commands import (
    WebCommandError,
    WebCommandService,
    normalize_web_session_id,
)


class Interrupt:
    def __init__(self) -> None:
        self.requested: list[str] = []

    async def request_interrupt(self, session_key: str) -> InterruptResult:
        self.requested.append(session_key)
        return InterruptResult("interrupted", session_key, "turn-1", 12, "ended")

    def get_active_turn_snapshot(self, session_key: str) -> dict[str, str] | None:
        if session_key != "web:chat":
            return None
        return {"session_id": session_key, "turn_id": "turn-1"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("chat", "web:chat"),
        (" web:chat ", "web:chat"),
        ("other:chat", None),
        ("web:", None),
    ],
)
def test_normalize_web_session_id(value: object, expected: str | None) -> None:
    assert normalize_web_session_id(value) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "media", "code"),
    [
        ("   ", [], "empty_message"),
        ("x" * (32 * 1024 + 1), [], "message_too_large"),
        ("x", [str(index) for index in range(9)], "message_too_large"),
    ],
    ids=["empty", "text-too-large", "media-too-large"],
)
async def test_validation_failure_does_not_create_or_publish(
    text: str,
    media: list[str],
    code: str,
) -> None:
    bus = MessageBus()
    created: list[str] = []

    async def ensure_session(session_key: str) -> object:
        created.append(session_key)
        return object()

    service = WebCommandService(bus, Interrupt(), ensure_session=ensure_session)

    with pytest.raises(WebCommandError) as raised:
        await service.prepare_message(
            request_id="request-1",
            session_id=None,
            text=text,
            media=media,
        )

    assert raised.value.code == code
    assert created == []
    assert bus._inbound.empty()


@pytest.mark.asyncio
async def test_invalid_media_does_not_create_or_publish(tmp_path: Path) -> None:
    media_root = tmp_path / "uploads"
    media_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    bus = MessageBus()
    created: list[str] = []

    async def ensure_session(session_key: str) -> object:
        created.append(session_key)
        return object()

    service = WebCommandService(
        bus,
        Interrupt(),
        media_root=media_root,
        ensure_session=ensure_session,
    )

    with pytest.raises(WebCommandError) as raised:
        await service.prepare_message(
            request_id="request-media",
            session_id=None,
            text="read",
            media=[str(outside)],
        )

    assert raised.value.code == "invalid_media"
    assert created == []
    assert bus._inbound.empty()


@pytest.mark.asyncio
async def test_prepare_and_submit_are_separate_and_preserve_message() -> None:
    bus = MessageBus()
    created: list[str] = []

    async def ensure_session(session_key: str) -> object:
        created.append(session_key)
        return object()

    service = WebCommandService(bus, Interrupt(), ensure_session=ensure_session)

    prepared = await service.prepare_message(
        request_id="request-new",
        session_id=None,
        text="hello",
        media=[" image.png ", "", 1],
    )

    assert prepared.created_session is True
    assert prepared.session_key.startswith("web:")
    assert created == [prepared.session_key]
    assert bus._inbound.empty()
    assert prepared.message.session_key == prepared.session_key
    assert prepared.message.content == "hello"
    assert prepared.message.media == ["image.png"]
    assert prepared.message.metadata == {"request_id": "request-new"}

    await service.submit_message(prepared)

    assert await bus.consume_inbound() is prepared.message


@pytest.mark.asyncio
async def test_existing_session_does_not_create_another_session() -> None:
    bus = MessageBus()

    async def ensure_session(_session_key: str) -> object:
        raise AssertionError("existing session must not be recreated")

    service = WebCommandService(bus, Interrupt(), ensure_session=ensure_session)
    prepared = await service.prepare_message(
        request_id="request-existing",
        session_id="chat",
        text="hello",
        media=None,
    )

    assert prepared.created_session is False
    assert prepared.session_key == "web:chat"


@pytest.mark.asyncio
async def test_interrupt_and_snapshot_are_delegated_without_transport_types() -> None:
    interrupt = Interrupt()
    service = WebCommandService(MessageBus(), interrupt)

    result = await service.stop_turn("web:chat")

    assert result == InterruptResult("interrupted", "web:chat", "turn-1", 12, "ended")
    assert interrupt.requested == ["web:chat"]
    assert service.get_active_turn_snapshot("web:chat") == {
        "session_id": "web:chat",
        "turn_id": "turn-1",
    }
