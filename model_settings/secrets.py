"""API Key 的系统凭据存储边界。"""

from __future__ import annotations

from typing import Protocol


class SecretStoreError(RuntimeError):
    """系统凭据存储不可用或操作失败。"""


class SecretStore(Protocol):
    def get(self, secret_ref: str) -> str | None: ...
    def set(self, secret_ref: str, value: str) -> None: ...
    def delete(self, secret_ref: str) -> None: ...


class KeyringSecretStore:
    """通过 keyring 使用 Windows Credential Manager 等系统凭据后端。"""

    def __init__(self, service_name: str = "BeanAgent.ModelConnections") -> None:
        try:
            import keyring
        except ImportError as error:
            raise SecretStoreError("系统凭据组件未安装") from error
        self._keyring = keyring
        self._service_name = service_name

    def get(self, secret_ref: str) -> str | None:
        try:
            return self._keyring.get_password(self._service_name, secret_ref)
        except Exception as error:
            raise SecretStoreError("读取系统凭据失败") from error

    def set(self, secret_ref: str, value: str) -> None:
        if not value:
            raise SecretStoreError("API Key 不能为空")
        try:
            self._keyring.set_password(self._service_name, secret_ref, value)
        except Exception as error:
            raise SecretStoreError("保存系统凭据失败") from error

    def delete(self, secret_ref: str) -> None:
        try:
            self._keyring.delete_password(self._service_name, secret_ref)
        except self._keyring.errors.PasswordDeleteError:
            return
        except Exception as error:
            raise SecretStoreError("删除系统凭据失败") from error


class UnavailableSecretStore:
    """系统凭据组件不可用时保持服务可启动，但绝不降级明文。"""

    def _raise(self) -> None:
        raise SecretStoreError("系统凭据存储不可用，请安装并配置 keyring 后端")

    def get(self, secret_ref: str) -> str | None:
        self._raise()

    def set(self, secret_ref: str, value: str) -> None:
        self._raise()

    def delete(self, secret_ref: str) -> None:
        self._raise()


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


def create_system_secret_store() -> SecretStore:
    try:
        return KeyringSecretStore()
    except SecretStoreError:
        return UnavailableSecretStore()


__all__ = [
    "KeyringSecretStore",
    "MemorySecretStore",
    "SecretStore",
    "SecretStoreError",
    "UnavailableSecretStore",
    "create_system_secret_store",
]
