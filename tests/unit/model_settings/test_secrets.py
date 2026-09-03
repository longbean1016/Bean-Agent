from model_settings.secrets import MemorySecretStore, SecretStoreError, UnavailableSecretStore


def test_memory_secret_store_contract() -> None:
    store = MemorySecretStore()
    store.set("connection:one", "secret")
    assert store.get("connection:one") == "secret"
    store.delete("connection:one")
    assert store.get("connection:one") is None


def test_unavailable_store_never_falls_back_to_plaintext() -> None:
    store = UnavailableSecretStore()
    try:
        store.set("connection:one", "secret")
    except SecretStoreError as error:
        assert "不可用" in str(error)
    else:
        raise AssertionError("凭据组件不可用时必须拒绝保存")
