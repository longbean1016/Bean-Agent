"""模型连接、模型资料与路由的 SQLite 持久化。"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from model_settings.models import ModelConnection, ModelProfile, ModelRoute, utc_now


class ModelSettingsConflict(RuntimeError):
    pass


class ModelSettingsStore:
    """只负责持久化，不发网络请求也不读取系统凭据。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._closed = False
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._db.close()

    def list_connections(self) -> list[ModelConnection]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM model_connections ORDER BY created_at, id"
            ).fetchall()
        return [_connection(row) for row in rows]

    def get_connection(self, connection_id: str) -> ModelConnection | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM model_connections WHERE id = ?", (connection_id,)
            ).fetchone()
        return _connection(row) if row else None

    def save_connection(self, connection: ModelConnection) -> ModelConnection:
        with self._lock:
            current = self.get_connection(connection.id)
            revision = (current.revision + 1) if current else max(1, connection.revision)
            created_at = current.created_at if current else connection.created_at
            saved = replace(
                connection,
                revision=revision,
                created_at=created_at,
                updated_at=utc_now(),
            )
            self._db.execute(
                """
                INSERT INTO model_connections (
                    id, name, provider, base_url, secret_ref, enabled,
                    default_adapter, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, provider=excluded.provider,
                    base_url=excluded.base_url, secret_ref=excluded.secret_ref,
                    enabled=excluded.enabled, default_adapter=excluded.default_adapter,
                    revision=excluded.revision, updated_at=excluded.updated_at
                """,
                (
                    saved.id, saved.name, saved.provider, saved.base_url,
                    saved.secret_ref, int(saved.enabled), saved.default_adapter,
                    saved.revision, saved.created_at, saved.updated_at,
                ),
            )
            self._db.commit()
        return saved

    def delete_connection(self, connection_id: str) -> bool:
        with self._lock:
            route = self._db.execute(
                "SELECT scope FROM model_routes WHERE connection_id = ? LIMIT 1",
                (connection_id,),
            ).fetchone()
            if route:
                raise ModelSettingsConflict("连接仍被默认路由或会话引用")
            cursor = self._db.execute(
                "DELETE FROM model_connections WHERE id = ?", (connection_id,)
            )
            self._db.commit()
        return cursor.rowcount > 0

    def list_models(
        self, connection_id: str | None = None, *, include_unavailable: bool = True
    ) -> list[ModelProfile]:
        where: list[str] = []
        params: list[Any] = []
        if connection_id is not None:
            where.append("connection_id = ?")
            params.append(connection_id)
        if not include_unavailable:
            where.append("available = 1")
        query = "SELECT * FROM connection_models"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY connection_id, display_name COLLATE NOCASE, model_id"
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [_profile(row) for row in rows]

    def get_model(self, connection_id: str, model_id: str) -> ModelProfile | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM connection_models WHERE connection_id = ? AND model_id = ?",
                (connection_id, model_id),
            ).fetchone()
        return _profile(row) if row else None

    def save_model(self, profile: ModelProfile) -> ModelProfile:
        with self._lock:
            current = self.get_model(profile.connection_id, profile.model_id)
            revision = (current.revision + 1) if current else max(1, profile.revision)
            saved = replace(profile, revision=revision)
            self._db.execute(
                """
                INSERT INTO connection_models (
                    connection_id, model_id, display_name, context_window,
                    max_output_tokens, supports_tools, supports_vision,
                    supports_reasoning, reasoning_options, adapter,
                    metadata_source, metadata_updated_at, user_overrides,
                    available, revision, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(connection_id, model_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    context_window=excluded.context_window,
                    max_output_tokens=excluded.max_output_tokens,
                    supports_tools=excluded.supports_tools,
                    supports_vision=excluded.supports_vision,
                    supports_reasoning=excluded.supports_reasoning,
                    reasoning_options=excluded.reasoning_options,
                    adapter=excluded.adapter,
                    metadata_source=excluded.metadata_source,
                    metadata_updated_at=excluded.metadata_updated_at,
                    user_overrides=excluded.user_overrides,
                    available=excluded.available, revision=excluded.revision,
                    discovered_at=excluded.discovered_at
                """,
                _profile_values(saved),
            )
            self._db.commit()
        return saved

    def replace_discovered_models(
        self, connection_id: str, profiles: Iterable[ModelProfile]
    ) -> list[ModelProfile]:
        discovered = list(profiles)
        incoming = {profile.model_id for profile in discovered}
        with self._lock:
            current = self.list_models(connection_id)
            for profile in discovered:
                existing = next((item for item in current if item.model_id == profile.model_id), None)
                overrides = dict(existing.user_overrides) if existing else {}
                saved = profile.with_overrides(overrides) if overrides else profile
                self.save_model(replace(saved, available=True))
            for existing in current:
                if existing.model_id not in incoming and existing.available:
                    self.save_model(replace(existing, available=False))
        return self.list_models(connection_id)

    def set_route(self, scope: str, route: ModelRoute) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO model_routes (scope, connection_id, model_id, reasoning_effort, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope) DO UPDATE SET
                    connection_id=excluded.connection_id, model_id=excluded.model_id,
                    reasoning_effort=excluded.reasoning_effort, updated_at=excluded.updated_at
                """,
                (scope, route.connection_id, route.model_id, route.reasoning_effort, utc_now()),
            )
            self._db.commit()

    def get_route(self, scope: str) -> ModelRoute | None:
        with self._lock:
            row = self._db.execute(
                "SELECT connection_id, model_id, reasoning_effort FROM model_routes WHERE scope = ?",
                (scope,),
            ).fetchone()
        return ModelRoute(**dict(row)) if row else None

    def delete_route(self, scope: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM model_routes WHERE scope = ?", (scope,))
            self._db.commit()

    def get_catalog_state(self) -> dict[str, str | None]:
        with self._lock:
            rows = self._db.execute(
                "SELECT key, value FROM model_settings_meta WHERE key LIKE 'catalog.%'"
            ).fetchall()
        return {str(row["key"])[8:]: row["value"] for row in rows}

    def set_catalog_state(self, **values: str | None) -> None:
        with self._lock:
            for key, value in values.items():
                self._db.execute(
                    "INSERT OR REPLACE INTO model_settings_meta (key, value) VALUES (?, ?)",
                    (f"catalog.{key}", value),
                )
            self._db.commit()


def _connection(row: sqlite3.Row) -> ModelConnection:
    return ModelConnection(
        id=row["id"], name=row["name"], provider=row["provider"],
        base_url=row["base_url"], secret_ref=row["secret_ref"],
        enabled=bool(row["enabled"]), default_adapter=row["default_adapter"],
        revision=int(row["revision"]), created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _optional_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _profile(row: sqlite3.Row) -> ModelProfile:
    return ModelProfile(
        connection_id=row["connection_id"], model_id=row["model_id"],
        display_name=row["display_name"], context_window=row["context_window"],
        max_output_tokens=row["max_output_tokens"],
        supports_tools=_optional_bool(row["supports_tools"]),
        supports_vision=_optional_bool(row["supports_vision"]),
        supports_reasoning=_optional_bool(row["supports_reasoning"]),
        reasoning_options=tuple(json.loads(row["reasoning_options"] or "[]")),
        adapter=row["adapter"], metadata_source=row["metadata_source"],
        metadata_updated_at=row["metadata_updated_at"],
        user_overrides=json.loads(row["user_overrides"] or "{}"),
        available=bool(row["available"]), revision=int(row["revision"]),
        discovered_at=row["discovered_at"],
    )


def _profile_values(profile: ModelProfile) -> tuple[Any, ...]:
    as_int = lambda value: None if value is None else int(value)
    return (
        profile.connection_id, profile.model_id, profile.display_name,
        profile.context_window, profile.max_output_tokens,
        as_int(profile.supports_tools), as_int(profile.supports_vision),
        as_int(profile.supports_reasoning),
        json.dumps(list(profile.reasoning_options), ensure_ascii=False),
        profile.adapter, profile.metadata_source, profile.metadata_updated_at,
        json.dumps(profile.user_overrides, ensure_ascii=False, sort_keys=True),
        int(profile.available), profile.revision, profile.discovered_at,
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_connections (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    base_url TEXT NOT NULL,
    secret_ref TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    default_adapter TEXT NOT NULL DEFAULT 'generic_openai',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connection_models (
    connection_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    context_window INTEGER,
    max_output_tokens INTEGER,
    supports_tools INTEGER,
    supports_vision INTEGER,
    supports_reasoning INTEGER,
    reasoning_options TEXT NOT NULL DEFAULT '[]',
    adapter TEXT NOT NULL DEFAULT 'generic_openai',
    metadata_source TEXT NOT NULL DEFAULT 'unknown',
    metadata_updated_at TEXT,
    user_overrides TEXT NOT NULL DEFAULT '{}',
    available INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 1,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY (connection_id, model_id),
    FOREIGN KEY (connection_id) REFERENCES model_connections(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS model_routes (
    scope TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    reasoning_effort TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (connection_id, model_id)
        REFERENCES connection_models(connection_id, model_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS model_settings_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


__all__ = ["ModelSettingsConflict", "ModelSettingsStore"]
