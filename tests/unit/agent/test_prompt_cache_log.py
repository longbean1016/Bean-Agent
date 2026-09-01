"""按会话隔离的 Prompt Cache JSONL 日志测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agent.prompt_cache_diagnostics import PromptCacheDiagnostics
from agent.prompt_cache_log import PromptCacheLogWriter


def _read_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_writer_separates_sessions_and_uses_safe_hashed_filenames(
    tmp_path: Path,
) -> None:
    writer = PromptCacheLogWriter(tmp_path)
    timestamp = datetime(2026, 7, 18, 12, 30, tzinfo=timezone.utc)

    first = writer.write(
        session_key="web:../../same",
        turn_id="turn-1",
        iteration=1,
        prompt_tokens=120,
        hit_tokens=90,
        timestamp=timestamp,
    )
    second = writer.write(
        session_key="telegram:../../same",
        turn_id="turn-2",
        iteration=2,
        prompt_tokens=None,
        hit_tokens=None,
        timestamp=timestamp,
    )

    assert first != second
    assert first.parent == second.parent == tmp_path / "logs" / "prompt-cache"
    assert first.parent.resolve().is_relative_to(tmp_path.resolve())
    assert ":" not in first.name and ".." not in first.name
    assert len(first.stem.rsplit("-", 1)[-1]) == 12
    assert _read_rows(first) == [
        {
            "timestamp": "2026-07-18T20:30:00+08:00",
            "session": "web:../../same",
            "turn": "turn-1",
            "iteration": 1,
            "cache_status": "available",
            "prompt_tokens": 120,
            "hit_tokens": 90,
            "hit_rate": 0.75,
        }
    ]
    assert _read_rows(second)[0]["cache_status"] == "unavailable"
    assert "prompt_tokens" not in _read_rows(second)[0]


def test_writer_rotates_each_session_independently(tmp_path: Path) -> None:
    writer = PromptCacheLogWriter(tmp_path, max_bytes=220, backup_count=2)

    for iteration in range(1, 8):
        writer.write(
            session_key="web:c",
            turn_id=f"turn-{iteration}",
            iteration=iteration,
            prompt_tokens=100,
            hit_tokens=50,
        )

    files = sorted((tmp_path / "logs" / "prompt-cache").glob("*.log*"))
    assert 2 <= len(files) <= 3
    assert any(path.name.endswith(".log.1") for path in files)
    assert not any(path.name.endswith(".log.3") for path in files)
    assert all(path.stat().st_size <= 220 for path in files)


def test_diagnostics_hashes_canonical_payload_and_isolates_sessions() -> None:
    diagnostics = PromptCacheDiagnostics()
    first = diagnostics.observe(
        "web:a",
        [
            {"role": "system", "content": "固定"},
            {"role": "user", "content": [{"type": "text", "text": "第一轮"}]},
        ],
        [{"function": {"name": "echo", "description": "回显"}, "type": "function"}],
    )
    second = diagnostics.observe(
        "web:a",
        [
            {"content": "固定", "role": "system"},
            {"role": "user", "content": [{"text": "第一轮", "type": "text"}]},
            {"role": "assistant", "content": "完成"},
        ],
        [{"type": "function", "function": {"description": "回显", "name": "echo"}}],
    )
    other = diagnostics.observe(
        "web:b",
        [{"role": "system", "content": "固定"}],
        [],
    )

    assert second.canonical_hash != first.canonical_hash
    assert second.common_prefix_messages == 2
    assert second.common_prefix_tokens > 0
    assert other.common_prefix_messages == 0


def test_writer_persists_structure_only_diagnostics(tmp_path: Path) -> None:
    diagnostics = PromptCacheDiagnostics()
    snapshot = diagnostics.observe(
        "web:a",
        [{"role": "system", "content": "固定"}],
    )
    writer = PromptCacheLogWriter(tmp_path)
    path = writer.write(
        session_key="web:a",
        turn_id="turn-1",
        iteration=1,
        prompt_tokens=10,
        hit_tokens=5,
        diagnostics=snapshot,
    )
    row = _read_rows(path)[0]

    assert row["canonical_hash"] == snapshot.canonical_hash
    assert row["message_count"] == 1
    assert row["common_prefix_messages"] == 0
    assert "固定" not in json.dumps(row, ensure_ascii=False)
