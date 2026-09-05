from pathlib import Path

from model_settings.secrets import MemorySecretStore, SqliteSecretStore


def test_memory_secret_store_contract() -> None:
    store = MemorySecretStore()
    store.set("connection:one", "secret")
    assert store.get("connection:one") == "secret"
    store.delete("connection:one")
    assert store.get("connection:one") is None


def test_sqlite_secret_store_persists_and_deletes_value(tmp_path: Path) -> None:
    path = tmp_path / "model-settings.db"
    store = SqliteSecretStore(path)
    store.set("connection:one", "secret")

    assert SqliteSecretStore(path).get("connection:one") == "secret"
    store.delete("connection:one")
    assert store.get("connection:one") is None
