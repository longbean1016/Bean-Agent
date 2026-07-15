"""Markdown 长期记忆、幂等追加与 PENDING 两阶段提交。"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from pathlib import Path

DEFAULT_SELF_MD = """# BeanAgent 的自我认知

## 人格与形象
- 我是 BeanAgent，一个直接、温暖、参与思考的长期协作伙伴。

## 我对当前用户的理解
- 我只根据可靠记忆逐步理解用户，不在缺少证据时编造画像。

## 我们关系的定义
- 我们以透明、尊重边界和持续协作为基础。
"""


class MarkdownMemoryStore:
    """管理 workspace/memory 下可审阅的长期记忆文件。"""

    def __init__(self, workspace: Path) -> None:
        self.memory_dir = Path(workspace) / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.self_file = self.memory_dir / "SELF.md"
        self.pending_file = self.memory_dir / "PENDING.md"
        self.pending_snapshot_file = self.memory_dir / "PENDING.snapshot.md"
        self.recent_context_file = self.memory_dir / "RECENT_CONTEXT.md"
        self.consolidation_db = self.memory_dir / "consolidation_writes.db"
        self._lock = threading.RLock()
        self._closed = False
        self.pending_file.touch(exist_ok=True)
        self.recent_context_file.touch(exist_ok=True)
        self._db = sqlite3.connect(str(self.consolidation_db), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS writes(source_ref TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(source_ref,kind))")
        self._db.commit()
        self._recover_pending_snapshot()

    def read_long_term(self) -> str: return self._read(self.memory_file)
    def read_self(self) -> str: return self._read(self.self_file)
    def read_recent_context(self) -> str: return self._read(self.recent_context_file)
    def read_pending(self) -> str: return self._read(self.pending_file)

    def write_long_term(self, content: str) -> None: self._atomic_write(self.memory_file, content)
    def write_self(self, content: str) -> None: self._atomic_write(self.self_file, content)
    def write_recent_context(self, content: str) -> None: self._atomic_write(self.recent_context_file, content)

    def get_memory_context(self) -> str:
        value = self.read_long_term().strip()
        return f"## Long-term Memory\n{value}" if value else ""

    def append_pending(self, facts: str) -> None:
        text = str(facts).strip()
        if not text:
            return
        with self._lock:
            current = self.read_pending().rstrip()
            self._atomic_write(self.pending_file, f"{current}\n{text}\n".lstrip())

    def append_pending_once(self, facts: str, *, source_ref: str, kind: str = "pending") -> bool:
        ref = str(source_ref).strip()
        if not ref:
            raise ValueError("source_ref 不能为空")
        with self._lock:
            if self._db.execute("SELECT 1 FROM writes WHERE source_ref=? AND kind=?", (ref, kind)).fetchone():
                return False
            # 文件先原子落盘，再提交索引；进程在两步间崩溃最多造成重试时重复文本，
            # 不会出现索引已提交但事实永久丢失。consolidation 自身 source_ref 仍可审计。
            self.append_pending(facts)
            self._db.execute("INSERT INTO writes(source_ref,kind) VALUES(?,?)", (ref, kind))
            self._db.commit()
            return True

    def clear_pending(self) -> None: self._atomic_write(self.pending_file, "")

    def snapshot_pending(self) -> str:
        with self._lock:
            self._recover_pending_snapshot()
            content = self.read_pending()
            if not content.strip():
                return ""
            os.replace(self.pending_file, self.pending_snapshot_file)
            self.pending_file.touch()
            return content

    def commit_pending_snapshot(self) -> None:
        with self._lock:
            self.pending_snapshot_file.unlink(missing_ok=True)
            self.pending_file.touch(exist_ok=True)

    def rollback_pending_snapshot(self) -> None:
        with self._lock:
            if not self.pending_snapshot_file.exists():
                return
            snapshot = self._read(self.pending_snapshot_file).rstrip()
            current = self.read_pending().lstrip()
            merged = f"{snapshot}\n{current}".rstrip() + "\n" if snapshot or current else ""
            self._atomic_write(self.pending_file, merged)
            self.pending_snapshot_file.unlink(missing_ok=True)

    def backup_long_term(self, backup_name: str = "MEMORY.bak.md") -> None:
        if self.memory_file.exists():
            shutil.copyfile(self.memory_file, self.memory_file.with_name(backup_name))

    def has_long_term_memory(self) -> bool: return bool(self.read_long_term().strip())

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._db.close()
            self._closed = True

    def _recover_pending_snapshot(self) -> None:
        # 遗留 snapshot 表示上次 Optimizer 未 commit，启动时必须先回滚再接受新任务。
        if self.pending_snapshot_file.exists():
            self.rollback_pending_snapshot()

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _atomic_write(self, path: Path, content: str) -> None:
        with self._lock:
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(str(content), encoding="utf-8")
            os.replace(temporary, path)


__all__ = ["DEFAULT_SELF_MD", "MarkdownMemoryStore"]
