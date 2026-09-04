"""模型连接 API Key 的独立持久化边界。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol

from model_settings.models import utc_now


class SecretStoreError(RuntimeError):
    """连接密钥持久化不可用或操作失败。"""


class SecretStore(Protocol):
    def get(self, secret_ref: str) -> str | None: ...
    def set(self, secret_ref: str, value: str) -> None: ...
    def delete(self, secret_ref: str) -> None: ...


class SqliteSecretStore:
    """将连接密钥保存在模型设置数据库的独立表中。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(self.path) as database:
                database.execute(_SCHEMA)
        except sqlite3.Error as error:
            raise SecretStoreError("初始化连接密钥表失败") from error

    def get(self, secret_ref: str) -> str | None:
        try:
            with sqlite3.connect(self.path) as database:
                row = database.execute(
                    "SELECT api_key FROM model_connection_secrets WHERE secret_ref = ?",
                    (secret_ref,),
                ).fetchone()
        except sqlite3.Error as error:
            raise SecretStoreError("读取连接密钥失败") from error
        return str(row[0]) if row else None

    def set(self, secret_ref: str, value: str) -> None:
        if not value:
            raise SecretStoreError("API Key 不能为空")
        try:
            with sqlite3.connect(self.path) as database:
                database.execute(
                    """
                    INSERT INTO model_connection_secrets (secret_ref, api_key, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(secret_ref) DO UPDATE SET
                        api_key=excluded.api_key, updated_at=excluded.updated_at
                    """,
                    (secret_ref, value, utc_now()),
                )
        except sqlite3.Error as error:
            raise SecretStoreError("保存连接密钥失败") from error

    def delete(self, secret_ref: str) -> None:
        try:
            with sqlite3.connect(self.path) as database:
                database.execute(
                    "DELETE FROM model_connection_secrets WHERE secret_ref = ?",
                    (secret_ref,),
                )
        except sqlite3.Error as error:
            raise SecretStoreError("删除连接密钥失败") from error


class MemorySecretStore:
    """仅供测试注入，生产组装不得使用。"""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, secret_ref: str) -> str | None:
        return self._values.get(secret_ref)

    def set(self, secret_ref: str, value: str) -> None:
        self._values[secret_ref] = value

    def delete(self, secret_ref: str) -> None:
        self._values.pop(secret_ref, None)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_connection_secrets (
    secret_ref TEXT PRIMARY KEY,
    api_key TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


__all__ = [
    "MemorySecretStore",
    "SecretStore",
    "SecretStoreError",
    "SqliteSecretStore",
]
