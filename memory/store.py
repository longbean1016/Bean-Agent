"""SQLite + sqlite-vec 长期记忆存储。"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import sqlite_vec

from memory.ranker import combine_scores, hotness_score


class MemoryStore2:
    """保存结构化记忆，并为向量与关键词检索提供同一数据源。"""

    def __init__(self, db_path: str | Path, vec_dim: int = 1024) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.vec_dim = int(vec_dim)
        if self.vec_dim <= 0:
            raise ValueError("vec_dim 必须大于 0")
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._vec_available = self._load_vec()
        self._init_schema()

    def _load_vec(self) -> bool:
        try:
            self._db.enable_load_extension(True)
            sqlite_vec.load(self._db)
            return True
        except (AttributeError, sqlite3.Error):
            # 主表保存 JSON embedding，因此扩展不可用时仍可用余弦全扫描保证正确性。
            return False
        finally:
            try:
                self._db.enable_load_extension(False)
            except (AttributeError, sqlite3.Error):
                pass

    def _init_schema(self) -> None:
        with self._lock:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_items (
                    id TEXT PRIMARY KEY,
                    memory_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    embedding TEXT,
                    reinforcement INTEGER NOT NULL DEFAULT 1,
                    emotional_weight INTEGER NOT NULL DEFAULT 0,
                    extra_json TEXT,
                    source_ref TEXT,
                    happened_at TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_hash
                    ON memory_items(content_hash, memory_type);
                CREATE TABLE IF NOT EXISTS consolidation_events (
                    source_ref TEXT PRIMARY KEY,
                    item_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS consolidation_outbox (
                    source_ref TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS consolidation_writes (
                    source_ref TEXT PRIMARY KEY,
                    digest TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );
            """)
            if self._vec_available:
                self._db.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0(embedding float[{self.vec_dim}])"
                )
            self._db.commit()

    def upsert_item(self, memory_type: str, summary: str, embedding: list[float], source_ref: str = "", *, extra: dict[str, object] | None = None, happened_at: str | None = None, emotional_weight: int = 0) -> str:
        memory_type = str(memory_type).strip()
        summary = str(summary).strip()
        if not memory_type or not summary:
            raise ValueError("memory_type 和 summary 不能为空")
        vector = self._validate_vector(embedding)
        digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:16]
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._ensure_open()
            row = self._db.execute(
                "SELECT id,status FROM memory_items WHERE content_hash=? AND memory_type=?",
                (digest, memory_type),
            ).fetchone()
            if row is not None:
                item_id = str(row["id"])
                self._db.execute(
                    "UPDATE memory_items SET status='active', reinforcement=reinforcement+1, emotional_weight=MAX(emotional_weight, ?), updated_at=? WHERE id=?",
                    (max(0, min(int(emotional_weight), 10)), now, item_id),
                )
                self._db.commit()
                return f"reinforced:{item_id}"

            item_id = uuid.uuid4().hex
            cursor = self._db.execute(
                """INSERT INTO memory_items
                (id,memory_type,summary,content_hash,embedding,reinforcement,emotional_weight,
                 extra_json,source_ref,happened_at,status,created_at,updated_at)
                VALUES (?,?,?,?,?,1,?,?,?,?, 'active',?,?)""",
                (item_id, memory_type, summary, digest, json.dumps(vector), max(0, min(int(emotional_weight), 10)), json.dumps(extra or {}, ensure_ascii=False), source_ref, happened_at, now, now),
            )
            self._vec_insert(int(cursor.lastrowid), vector)
            self._db.commit()
        return f"new:{item_id}"

    def upsert_consolidation_event(self, source_ref: str, summary: str, embedding: list[float], *, extra: dict[str, object] | None = None, happened_at: str | None = None, emotional_weight: int = 0) -> str:
        ref = str(source_ref).strip()
        if not ref:
            raise ValueError("source_ref 不能为空")
        with self._lock:
            self._ensure_open()
            if self.has_consolidation_source_ref(ref):
                return f"skipped:{ref}"
            result = self.upsert_item("event", summary, embedding, ref, extra=extra, happened_at=happened_at, emotional_weight=emotional_weight)
            item_id = result.split(":", 1)[1]
            self._db.execute(
                "INSERT INTO consolidation_events(source_ref,item_id,created_at) VALUES(?,?,?)",
                (ref, item_id, datetime.now(timezone.utc).isoformat()),
            )
            self._db.commit()
            return f"saved:{item_id}"

    def has_consolidation_source_ref(self, source_ref: str) -> bool:
        with self._lock:
            self._ensure_open()
            return self._db.execute(
                "SELECT 1 FROM consolidation_events WHERE source_ref=?", (source_ref,)
            ).fetchone() is not None

    def enqueue_consolidation(self, source_ref: str, payload: dict[str, object]) -> None:
        """在 cursor 推进前持久化待同步事件，重复 source_ref 保留首次快照。"""

        with self._lock:
            self._ensure_open()
            self._db.execute(
                "INSERT OR IGNORE INTO consolidation_outbox(source_ref,payload_json,created_at) VALUES(?,?,?)",
                (source_ref, json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )
            self._db.commit()

    def list_pending_consolidations(self) -> list[dict[str, object]]:
        with self._lock:
            self._ensure_open()
            rows = self._db.execute(
                "SELECT source_ref,payload_json FROM consolidation_outbox ORDER BY created_at,source_ref"
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    def complete_consolidation(self, source_ref: str) -> None:
        with self._lock:
            self._ensure_open()
            self._db.execute("DELETE FROM consolidation_outbox WHERE source_ref=?", (source_ref,))
            self._db.commit()

    def has_consolidation_write(self, source_ref: str) -> bool:
        """判断单个 checkpoint 副作用是否已完成，支持重放跳过已写条目。"""

        with self._lock:
            self._ensure_open()
            return self._db.execute(
                "SELECT 1 FROM consolidation_writes WHERE source_ref=?",
                (str(source_ref),),
            ).fetchone() is not None

    def record_consolidation_write(self, source_ref: str, digest: str) -> None:
        with self._lock:
            self._ensure_open()
            self._db.execute(
                "INSERT OR IGNORE INTO consolidation_writes(source_ref,digest,completed_at) VALUES(?,?,?)",
                (str(source_ref), str(digest), datetime.now(timezone.utc).isoformat()),
            )
            self._db.commit()

    def vector_search(self, query_vec: list[float], top_k: int = 8, memory_types: list[str] | None = None, score_threshold: float = 0.0, scope_channel: str | None = None, scope_chat_id: str | None = None, require_scope_match: bool = False, hotness_alpha: float = 0.20, hotness_half_life_days: float = 14.0, time_start: datetime | None = None, time_end: datetime | None = None) -> list[dict[str, object]]:
        query = self._validate_vector(query_vec)
        rows = self._active_rows(memory_types)
        hits: list[dict[str, object]] = []
        for row in rows:
            item = self._row_to_item(row)
            if not _scope_matches(item, scope_channel, scope_chat_id, require_scope_match):
                continue
            if not _time_matches(item, time_start, time_end):
                continue
            vector = item.pop("embedding")
            similarity = _cosine(query, vector if isinstance(vector, list) else [])
            if similarity < float(score_threshold):
                continue
            try:
                updated_at = datetime.fromisoformat(str(item["updated_at"]))
            except ValueError:
                updated_at = datetime.now(timezone.utc)
            hotness = hotness_score(
                int(item["reinforcement"]),
                updated_at,
                half_life_days=hotness_half_life_days,
                emotional_weight=int(item["emotional_weight"]),
            )
            item["vector_score"] = similarity
            item["hotness"] = hotness
            item["score"] = combine_scores(similarity, hotness, alpha=hotness_alpha)
            hits.append(item)
        hits.sort(key=lambda value: (-float(value["score"]), str(value["id"])))
        return hits[: max(1, int(top_k))]

    def vector_search_batch(self, vectors: list[list[float]], **kwargs: Any) -> list[list[dict[str, object]]]:
        return [self.vector_search(vector, **kwargs) for vector in vectors]

    def keyword_search_summary(self, terms: list[str], memory_types: list[str] | None = None, limit: int = 20, time_start: datetime | None = None, time_end: datetime | None = None, scope_channel: str | None = None, scope_chat_id: str | None = None, require_scope_match: bool = False) -> list[dict[str, object]]:
        """使用 SQLite OR-LIKE 检索摘要，并按关键词覆盖率排序。"""

        clean_terms = [str(term).strip() for term in terms if len(str(term).strip()) >= 2]
        if not clean_terms:
            return []
        actual_limit = max(1, int(limit))

        type_filter = ""
        type_params: list[object] = []
        if memory_types:
            placeholders = ",".join("?" for _ in memory_types)
            type_filter = f" AND memory_type IN ({placeholders})"
            type_params.extend(memory_types)

        scope_filter = ""
        scope_params: list[object] = []
        if require_scope_match:
            scope_filter = (
                " AND COALESCE(TRIM(json_extract(extra_json, '$.scope_channel')), '') = ?"
                " AND COALESCE(TRIM(json_extract(extra_json, '$.scope_chat_id')), '') = ?"
            )
            scope_params.extend([
                str(scope_channel or "").strip(),
                str(scope_chat_id or "").strip(),
            ])

        conditions = " OR ".join("summary LIKE ?" for _ in clean_terms)
        score_expression = " + ".join(
            "(CASE WHEN summary LIKE ? THEN 1 ELSE 0 END)"
            for _ in clean_terms
        )
        like_values = [f"%{term}%" for term in clean_terms]
        has_time_filter = time_start is not None or time_end is not None
        batch_size = max(actual_limit, 1000) if has_time_filter else actual_limit
        sql = (
            "SELECT id,memory_type,summary,reinforcement,emotional_weight,"
            "extra_json,source_ref,happened_at,status,created_at,updated_at,"
            f"({score_expression}) AS keyword_hits "
            "FROM memory_items "
            f"WHERE status='active' AND ({conditions}){type_filter}{scope_filter} "
            "ORDER BY keyword_hits DESC,reinforcement DESC,id ASC "
            "LIMIT ? OFFSET ?"
        )

        results: list[dict[str, object]] = []
        offset = 0
        with self._lock:
            self._ensure_open()
            while True:
                params: list[object] = [
                    *like_values,
                    *like_values,
                    *type_params,
                    *scope_params,
                    batch_size,
                    offset,
                ]
                rows = self._db.execute(sql, params).fetchall()
                if not rows:
                    break
                for row in rows:
                    item: dict[str, object] = {
                        "id": str(row["id"]),
                        "memory_type": str(row["memory_type"]),
                        "summary": str(row["summary"]),
                        "reinforcement": int(row["reinforcement"]),
                        "emotional_weight": int(row["emotional_weight"]),
                        "extra_json": json.loads(row["extra_json"] or "{}"),
                        "source_ref": str(row["source_ref"] or ""),
                        "happened_at": str(row["happened_at"] or ""),
                        "status": str(row["status"]),
                        "created_at": str(row["created_at"]),
                        "updated_at": str(row["updated_at"]),
                        "keyword_score": float(row["keyword_hits"]) / len(clean_terms),
                    }
                    if not _time_matches(item, time_start, time_end):
                        continue
                    results.append(item)
                    if len(results) >= actual_limit:
                        return results
                if not has_time_filter or len(rows) < batch_size:
                    break
                offset += batch_size
        return results

    def list_events_by_time_range(
        self,
        time_start: datetime,
        time_end: datetime,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        """按事件实际发生时间返回结构化时间线，不使用创建时间补齐缺失值。"""

        start = _aware_datetime(time_start)
        end = _aware_datetime(time_end)
        if end <= start:
            return []
        hits: list[tuple[datetime, dict[str, object]]] = []
        for row in self._active_rows(["event"]):
            item = self._row_to_item(row)
            happened_at = str(item.get("happened_at") or "").strip()
            if not happened_at:
                continue
            try:
                event_time = _aware_datetime(datetime.fromisoformat(happened_at))
            except ValueError:
                continue
            if event_time < start or event_time >= end:
                continue
            item.pop("embedding", None)
            item["score"] = 1.0
            hits.append((event_time, item))

        # 先保留时间上最近的 limit 条，再按正序交给调用方展示时间线。
        max_items = max(1, min(int(limit), 200))
        selected = sorted(hits, key=lambda value: value[0], reverse=True)[:max_items]
        selected.sort(key=lambda value: value[0])
        return [item for _, item in selected]

    def keyword_match_procedures(self, action_tokens: list[str]) -> list[dict[str, object]]:
        action = " ".join(action_tokens).lower()
        result: list[dict[str, object]] = []
        for row in self._active_rows(["procedure"]):
            item = self._row_to_item(row)
            tags = item["extra_json"].get("trigger_tags", {}) if isinstance(item["extra_json"], dict) else {}
            if tags.get("scope") != "tool_triggered":
                continue
            keywords = [str(value) for value in tags.get("keywords", []) if len(str(value)) >= 3]
            tools = {str(value).lower() for value in tags.get("tools", [])}
            hit = any(value.lower() in action for value in keywords) if keywords else bool(tools & {t.lower() for t in action_tokens})
            if hit:
                item.pop("embedding", None)
                item["score"] = 1.0
                item["intercept"] = bool(tags.get("intercept", False))
                result.append(item)
        return result

    def mark_superseded_batch(self, ids: list[str]) -> tuple[list[str], list[str]]:
        clean = list(dict.fromkeys(str(value).strip() for value in ids if str(value).strip()))
        affected: list[str] = []
        missing: list[str] = []
        with self._lock:
            self._ensure_open()
            for item_id in clean:
                row = self._db.execute("SELECT rowid,status FROM memory_items WHERE id=?", (item_id,)).fetchone()
                if row is None or row["status"] != "active":
                    missing.append(item_id)
                    continue
                self._db.execute("UPDATE memory_items SET status='superseded',updated_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), item_id))
                self._vec_delete([int(row["rowid"])])
                affected.append(item_id)
            self._db.commit()
        return affected, missing

    def reinforce_items_batch(self, ids: list[str], emotional_weight: int = 0) -> None:
        clean = list(dict.fromkeys(ids))
        if not clean:
            return
        placeholders = ",".join("?" for _ in clean)
        with self._lock:
            self._ensure_open()
            self._db.execute(
                f"UPDATE memory_items SET reinforcement=reinforcement+1, emotional_weight=MAX(emotional_weight,?), updated_at=? WHERE id IN ({placeholders}) AND status='active'",
                (max(0, min(int(emotional_weight), 10)), datetime.now(timezone.utc).isoformat(), *clean),
            )
            self._db.commit()

    def replace_item_atomic(
        self,
        old_item_id: str,
        memory_type: str,
        summary: str,
        embedding: list[float],
        source_ref: str,
        *,
        extra: dict[str, object] | None = None,
        happened_at: str | None = None,
        emotional_weight: int = 0,
    ) -> str:
        """在同一 SQLite 事务内插入新记忆并将旧记忆标记为 superseded。"""

        vector = self._validate_vector(embedding)
        digest = hashlib.sha256(summary.strip().encode("utf-8")).hexdigest()[:16]
        now = datetime.now(timezone.utc).isoformat()
        new_item_id = uuid.uuid4().hex
        with self._lock:
            self._ensure_open()
            old = self._db.execute(
                "SELECT rowid,status FROM memory_items WHERE id=?", (old_item_id,)
            ).fetchone()
            if old is None or old["status"] != "active":
                raise KeyError(f"active 记忆不存在: {old_item_id}")
            try:
                self._db.execute("BEGIN")
                cursor = self._db.execute(
                    """INSERT INTO memory_items
                    (id,memory_type,summary,content_hash,embedding,reinforcement,emotional_weight,
                     extra_json,source_ref,happened_at,status,created_at,updated_at)
                    VALUES (?,?,?,?,?,1,?,?,?,?, 'active',?,?)""",
                    (new_item_id, memory_type, summary.strip(), digest, json.dumps(vector),
                     max(0, min(int(emotional_weight), 10)), json.dumps(extra or {}, ensure_ascii=False),
                     source_ref, happened_at, now, now),
                )
                self._vec_insert(int(cursor.lastrowid), vector)
                self._db.execute(
                    "UPDATE memory_items SET status='superseded',updated_at=? WHERE id=?",
                    (now, old_item_id),
                )
                self._vec_delete([int(old["rowid"])])
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return f"new:{new_item_id}"

    def record_consolidation_source_ref(self, source_ref: str, item_id: str) -> None:
        with self._lock:
            self._ensure_open()
            self._db.execute(
                "INSERT OR IGNORE INTO consolidation_events(source_ref,item_id,created_at) VALUES(?,?,?)",
                (source_ref, item_id, datetime.now(timezone.utc).isoformat()),
            )
            self._db.commit()

    def merge_item_raw(self, item_id: str, new_summary: str, new_embedding: list[float], new_extra: dict[str, object]) -> None:
        """原子更新合并目标，并同步主表与 vec 索引。"""

        vector = self._validate_vector(new_embedding)
        with self._lock:
            self._ensure_open()
            row = self._db.execute(
                "SELECT rowid,memory_type FROM memory_items WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"记忆不存在: {item_id}")
            digest = hashlib.sha256(new_summary.strip().encode("utf-8")).hexdigest()[:16]
            self._db.execute(
                """UPDATE memory_items SET summary=?,content_hash=?,embedding=?,extra_json=?,
                   reinforcement=reinforcement+1,updated_at=? WHERE id=?""",
                (new_summary.strip(), digest, json.dumps(vector), json.dumps(new_extra, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), item_id),
            )
            self._vec_delete([int(row["rowid"])])
            self._vec_insert(int(row["rowid"]), vector)
            self._db.commit()

    def find_similar_recent_events(self, embedding: list[float], *, days_back: int = 7, threshold: float = 0.92, top_k: int = 3) -> list[str]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days_back)))
        query = self._validate_vector(embedding)
        scored: list[tuple[str, float]] = []
        for row in self._active_rows(["event"]):
            item = self._row_to_item(row)
            try:
                created_at = datetime.fromisoformat(str(item["created_at"]))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if created_at < cutoff:
                continue
            score = _cosine(query, item["embedding"] if isinstance(item["embedding"], list) else [])
            if score >= float(threshold):
                scored.append((str(item["id"]), score))
        scored.sort(key=lambda value: (-value[1], value[0]))
        return [item_id for item_id, _ in scored[: max(1, int(top_k))]]

    def get_items_by_ids(self, ids: list[str]) -> list[dict[str, object]]:
        clean = list(dict.fromkeys(ids))
        if not clean:
            return []
        placeholders = ",".join("?" for _ in clean)
        order = " ".join(f"WHEN ? THEN {index}" for index in range(len(clean)))
        with self._lock:
            self._ensure_open()
            rows = self._db.execute(
                f"SELECT rowid,* FROM memory_items WHERE id IN ({placeholders}) ORDER BY CASE id {order} END",
                (*clean, *clean),
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def delete_by_source_ref(self, source_ref: str) -> int:
        with self._lock:
            self._ensure_open()
            rows = self._db.execute("SELECT rowid FROM memory_items WHERE source_ref=?", (source_ref,)).fetchall()
            cursor = self._db.execute("DELETE FROM memory_items WHERE source_ref=?", (source_ref,))
            self._vec_delete([int(row["rowid"]) for row in rows])
            self._db.commit()
            return int(cursor.rowcount or 0)

    def has_item_by_source_ref(self, source_ref: str, memory_type: str | None = None) -> bool:
        sql = "SELECT 1 FROM memory_items WHERE source_ref=?"
        params: tuple[object, ...] = (source_ref,)
        if memory_type:
            sql += " AND memory_type=?"
            params += (memory_type,)
        with self._lock:
            self._ensure_open()
            return self._db.execute(sql + " LIMIT 1", params).fetchone() is not None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._db.close()
            self._closed = True

    def _active_rows(self, memory_types: list[str] | None) -> list[sqlite3.Row]:
        sql = "SELECT rowid,* FROM memory_items WHERE status='active'"
        params: list[object] = []
        if memory_types:
            placeholders = ",".join("?" for _ in memory_types)
            sql += f" AND memory_type IN ({placeholders})"
            params.extend(memory_types)
        with self._lock:
            self._ensure_open()
            return self._db.execute(sql, params).fetchall()

    def _row_to_item(self, row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": str(row["id"]), "memory_type": str(row["memory_type"]),
            "summary": str(row["summary"]), "embedding": json.loads(row["embedding"] or "[]"),
            "reinforcement": int(row["reinforcement"]), "emotional_weight": int(row["emotional_weight"]),
            "extra_json": json.loads(row["extra_json"] or "{}"), "source_ref": str(row["source_ref"] or ""),
            "happened_at": str(row["happened_at"] or ""), "status": str(row["status"]),
            "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"]),
        }

    def _validate_vector(self, vector: list[float]) -> list[float]:
        values = [float(value) for value in vector]
        if len(values) != self.vec_dim:
            raise ValueError(f"向量维度应为 {self.vec_dim}，实际为 {len(values)}")
        return values

    def _vec_insert(self, rowid: int, vector: list[float]) -> None:
        if self._vec_available:
            self._db.execute("INSERT INTO vec_items(rowid,embedding) VALUES(?,?)", (rowid, sqlite_vec.serialize_float32(vector)))

    def _vec_delete(self, rowids: list[int]) -> None:
        if self._vec_available:
            self._db.executemany("DELETE FROM vec_items WHERE rowid=?", [(rowid,) for rowid in rowids])

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("MemoryStore2 已关闭")


def _scope_matches(item: dict[str, object], channel: str | None, chat_id: str | None, required: bool) -> bool:
    if not required:
        return True
    extra = item.get("extra_json")
    values = extra if isinstance(extra, dict) else {}
    return str(values.get("scope_channel", "")).strip() == str(channel or "").strip() and str(values.get("scope_chat_id", "")).strip() == str(chat_id or "").strip()


def _time_matches(item: dict[str, object], start: datetime | None, end: datetime | None) -> bool:
    if start is None and end is None:
        return True
    raw = str(item.get("happened_at") or item.get("created_at") or "")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (start is None or value >= start) and (end is None or value < end)


def _aware_datetime(value: datetime) -> datetime:
    """统一时间比较基准，兼容旧数据中的无时区 ISO 时间。"""

    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    denominator = math.sqrt(sum(v * v for v in left)) * math.sqrt(sum(v * v for v in right))
    return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0


__all__ = ["MemoryStore2"]
