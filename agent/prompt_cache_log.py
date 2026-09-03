"""按会话隔离保存 Prompt Cache 指标的 JSONL 运行日志。

日志只包含缓存 token、命中率和最小定位字段，不保存 Prompt、用户消息、Skill
正文或工具结果。每次写入短暂打开文件，避免会话数量增长时长期占用大量文件句柄；
共享写入和轮转由线程锁串行化，不能破坏同一文件的 JSONL 边界。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agent.prompt_cache_diagnostics import PromptCacheRequestDiagnostics

_LOCAL_TZ = ZoneInfo("Asia/Shanghai")
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")


class PromptCacheLogWriter:
    """将每次 Provider 缓存结果写入对应会话的轮转日志。"""

    def __init__(
        self,
        workspace: str | Path,
        *,
        max_bytes: int = 5 * 1024 * 1024,
        backup_count: int = 3,
    ) -> None:
        self.log_dir = (
            Path(workspace).expanduser().resolve() / "logs" / "prompt-cache"
        )
        self._max_bytes = max(1, int(max_bytes))
        self._backup_count = max(0, int(backup_count))
        self._lock = threading.Lock()

    def write(
        self,
        *,
        session_key: str,
        turn_id: str,
        iteration: int,
        prompt_tokens: int | None,
        hit_tokens: int | None,
        diagnostics: PromptCacheRequestDiagnostics | None = None,
        timestamp: datetime | None = None,
    ) -> Path:
        """追加一条缓存记录并返回目标文件路径。

        Provider 未返回 usage 时仍写入 ``unavailable``，这样可以区分“没有命中”
        与“供应商没有提供指标”，避免后续统计把缺失数据误当作 0%。
        """

        path = self.log_dir / self._filename(session_key)
        occurred_at = (timestamp or datetime.now(_LOCAL_TZ)).astimezone(_LOCAL_TZ)
        row: dict[str, object] = {
            "timestamp": occurred_at.isoformat(timespec="seconds"),
            "session": session_key,
            "turn": turn_id,
            "iteration": int(iteration),
        }
        if prompt_tokens is None or hit_tokens is None:
            row["cache_status"] = "unavailable"
        else:
            row.update(
                {
                    "cache_status": "available",
                    "prompt_tokens": int(prompt_tokens),
                    "hit_tokens": int(hit_tokens),
                    "hit_rate": (
                        round(hit_tokens / prompt_tokens, 6)
                        if prompt_tokens > 0
                        else 0.0
                    ),
                }
            )
        if diagnostics is not None:
            # 只写结构摘要，避免把用户正文、动态 frame 或工具结果带入日志。
            row.update(diagnostics.as_log_fields())
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded_size = len(line.encode("utf-8"))

        with self._lock:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size + encoded_size > self._max_bytes:
                self._rotate(path)
            with path.open("a", encoding="utf-8", newline="") as stream:
                stream.write(line)
        return path

    def _filename(self, session_key: str) -> str:
        readable = _SAFE_NAME_RE.sub("-", session_key).strip("-_").lower()
        readable = (readable or "session")[:48]
        digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:12]
        return f"{readable}-{digest}.log"

    def _rotate(self, path: Path) -> None:
        """保留固定数量备份；轮转只处理由安全文件名生成的同会话文件。"""

        if self._backup_count == 0:
            path.unlink(missing_ok=True)
            return
        oldest = Path(f"{path}.{self._backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self._backup_count - 1, 0, -1):
            source = Path(f"{path}.{index}")
            if source.exists():
                source.replace(Path(f"{path}.{index + 1}"))
        path.replace(Path(f"{path}.1"))


__all__ = ["PromptCacheLogWriter"]
