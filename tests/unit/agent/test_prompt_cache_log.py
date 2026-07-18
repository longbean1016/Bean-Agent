"""按会话隔离的 Prompt Cache JSONL 日志测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

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
