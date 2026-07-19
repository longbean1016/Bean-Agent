"""全部内置工具的基础集合与依赖条件注册测试。"""

from __future__ import annotations

from pathlib import Path

from memory.contracts import MemoryToolProfile, MemoryToolSpec
from session.store import SessionStore
from tools.registration import register_all
from tools.registry import ToolRegistry


class FakeMemory:
    def __init__(self, profile: MemoryToolProfile) -> None:
        self._profile = profile

    def tool_profile(self) -> MemoryToolProfile:
        return self._profile


def spec(name: str) -> MemoryToolSpec:
    return MemoryToolSpec(
        description=name,
        parameters={"type": "object", "properties": {}},
    )


def test_register_all_without_optional_services_exposes_base_tools(
    tmp_path: Path,
) -> None:
    registry = register_all(
        ToolRegistry(),
        allowed_dir=tmp_path,
        multimodal=False,
        allow_shell_network=False,
    )

    assert registry.get_registered_names() == {
        "tool_search", "shell", "read_file", "write_file", "edit_file", "list_dir",
        "web_search", "web_fetch",
    }


def test_register_all_adds_session_and_only_profile_enabled_memory_tools(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    memory = FakeMemory(
        MemoryToolProfile(recall=spec("recall"), memorize=None, forget=spec("forget"))
    )
    try:
        registry = register_all(
            ToolRegistry(),
            allowed_dir=tmp_path,
            multimodal=False,
            session_store=store,
            memory_engine=memory,
        )
    finally:
        store.close()

    names = registry.get_registered_names()
    assert {"fetch_messages", "search_messages", "recall_memory", "forget_memory"} <= names
    assert "memorize" not in names
