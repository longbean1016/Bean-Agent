"""Windows 目录能力 SID 与 DACL 管理。"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sandbox.errors import SandboxUnavailable

_GRANT_MASK = 0x00110156
_FILE_ALL_ACCESS = 0x001F01FF
_INHERIT_FLAGS = 0x1 | 0x2


def workspace_write_sid(workspace: str | Path) -> str:
    return _sid_from_digest(str(Path(workspace).resolve()).encode("utf-8"), temp=False)


def temp_write_sid(temp_dir: str | Path) -> str:
    payload = b"temp\0" + str(Path(temp_dir).resolve()).encode("utf-8")
    return _sid_from_digest(payload, temp=True)


def _sid_from_digest(payload: bytes, *, temp: bool) -> str:
    digest = hashlib.sha256(payload).digest()
    limit = 2**30 - 1
    first = int.from_bytes(digest[:4], "little") % limit + 1
    second = int.from_bytes(digest[4:8], "little") % limit + 1
    suffix = "-1" if temp else ""
    return f"S-1-4-{first}-{second}{suffix}"


class WindowsAclProvider:
    """串行维护 standing workspace ACE 和可撤销 temp ACE。"""

    def __init__(self) -> None:
        self._workspace_grants: set[tuple[str, str]] = set()
        self._temp_grants: dict[str, str] = {}
        self._lock = threading.RLock()

    def grant_workspace(self, path: Path, sid_text: str) -> None:
        key = (os.path.normcase(str(path.resolve())), sid_text)
        with self._lock:
            if key in self._workspace_grants:
                return
            self._grant(path, sid_text)
            self._workspace_grants.add(key)

    def grant_temp(self, path: Path, sid_text: str) -> None:
        key = os.path.normcase(str(path.resolve()))
        with self._lock:
            if self._temp_grants.get(key) == sid_text:
                return
            self._grant(path, sid_text)
            self._temp_grants[key] = sid_text

    def revoke_temp(self, path: Path) -> None:
        key = os.path.normcase(str(path.resolve()))
        with self._lock:
            sid_text = self._temp_grants.pop(key, None)
            if sid_text is not None and path.exists():
                self._revoke(path, sid_text)

    def close(self) -> None:
        failures: list[Exception] = []
        with self._lock:
            grants = list(self._temp_grants.items())
        for raw_path, _ in grants:
            try:
                self.revoke_temp(Path(raw_path))
            except Exception as error:
                failures.append(error)
        if failures:
            raise SandboxUnavailable(
                f"撤销会话临时目录 ACL 失败：{failures[0]}"
            ) from failures[0]

    def _grant(self, path: Path, sid_text: str) -> None:
        win32security, _ = _security_modules()
        target = path.resolve(strict=True)
        if not target.is_dir():
            raise SandboxUnavailable(f"ACL 目标不是目录：{target}")
        sid = win32security.ConvertStringSidToSid(sid_text)
        with _path_lock(target):
            descriptor = win32security.GetNamedSecurityInfo(
                str(target),
                win32security.SE_FILE_OBJECT,
                win32security.OWNER_SECURITY_INFORMATION
                | win32security.DACL_SECURITY_INFORMATION,
            )
            current_sid = _assert_owned_by_current_user(
                descriptor, target, win32security
            )
            dacl = descriptor.GetSecurityDescriptorDacl()
            if dacl is None:
                dacl = win32security.ACL()
            if _has_capability_ace(dacl, sid_text):
                return
            # 所有者隐式访问在部分继承 ACL 上不会形成普通 Token 的显式写 ACE；
            # Restricted Token 的两阶段检查需要先保留调用用户原本拥有的访问。
            owner_rights_sid = win32security.ConvertStringSidToSid("S-1-3-4")
            if (
                not _has_full_access_ace(dacl, str(current_sid))
                and _has_full_access_ace(dacl, str(owner_rights_sid))
            ):
                dacl.AddAccessAllowedAceEx(
                    win32security.ACL_REVISION_DS,
                    _INHERIT_FLAGS,
                    _FILE_ALL_ACCESS,
                    current_sid,
                )
            dacl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION_DS,
                _INHERIT_FLAGS,
                _GRANT_MASK,
                sid,
            )
            win32security.SetNamedSecurityInfo(
                str(target),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl,
                None,
            )

    def _revoke(self, path: Path, sid_text: str) -> None:
        win32security, _ = _security_modules()
        target = path.resolve(strict=True)
        sid_indexes: list[int] = []
        with _path_lock(target):
            descriptor = win32security.GetNamedSecurityInfo(
                str(target),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION,
            )
            dacl = descriptor.GetSecurityDescriptorDacl()
            if dacl is None:
                return
            for index in range(dacl.GetAceCount()):
                ace = dacl.GetAce(index)
                if len(ace) >= 3 and str(ace[2]) == sid_text:
                    sid_indexes.append(index)
            for index in reversed(sid_indexes):
                dacl.DeleteAce(index)
            if sid_indexes:
                win32security.SetNamedSecurityInfo(
                    str(target),
                    win32security.SE_FILE_OBJECT,
                    win32security.DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    dacl,
                    None,
                )


def _has_capability_ace(dacl: object, sid_text: str) -> bool:
    for index in range(dacl.GetAceCount()):  # type: ignore[attr-defined]
        ace = dacl.GetAce(index)  # type: ignore[attr-defined]
        if len(ace) < 3:
            continue
        header, mask, sid = ace[:3]
        ace_type = int(header[0]) if isinstance(header, tuple) else -1
        ace_flags = int(header[1]) if isinstance(header, tuple) else 0
        if (
            ace_type == 0
            and int(mask) == _GRANT_MASK
            and ace_flags & _INHERIT_FLAGS == _INHERIT_FLAGS
            and str(sid) == sid_text
        ):
            return True
    return False


def _has_full_access_ace(dacl: object, sid_text: str) -> bool:
    for index in range(dacl.GetAceCount()):  # type: ignore[attr-defined]
        ace = dacl.GetAce(index)  # type: ignore[attr-defined]
        if len(ace) >= 3 and int(ace[1]) == _FILE_ALL_ACCESS and str(ace[2]) == sid_text:
            return True
    return False


def _assert_owned_by_current_user(
    descriptor: object,
    path: Path,
    win32security: object,
) -> object:
    import win32api
    import win32con

    token = win32security.OpenProcessToken(  # type: ignore[attr-defined]
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        token_user = win32security.GetTokenInformation(  # type: ignore[attr-defined]
            token, win32security.TokenUser  # type: ignore[attr-defined]
        )
        current_sid = token_user[0] if isinstance(token_user, tuple) else token_user
    finally:
        token.Close()
    owner_sid = descriptor.GetSecurityDescriptorOwner()  # type: ignore[attr-defined]
    if str(owner_sid) != str(current_sid):
        raise SandboxUnavailable(f"目录必须由当前 Windows 用户拥有：{path}")
    return current_sid


@contextmanager
def _path_lock(path: Path) -> Iterator[None]:
    """跨进程串行 DACL 的读改写，避免并发授权互相覆盖。"""

    if os.name != "nt":
        raise SandboxUnavailable("Windows ACL 沙箱仅支持 Windows")
    import msvcrt

    digest = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:16]
    lock_root = Path(tempfile.gettempdir()) / "beanagent-acl-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / f"{digest}.lock"
    with lock_path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _security_modules() -> tuple[object, object]:
    if os.name != "nt":
        raise SandboxUnavailable("Windows ACL 沙箱仅支持 Windows")
    try:
        import win32security
        import pywintypes
    except ImportError as error:
        raise SandboxUnavailable(
            "缺少 Windows 沙箱依赖 pywin32，已拒绝不受限执行"
        ) from error
    return win32security, pywintypes


__all__ = ["WindowsAclProvider", "temp_write_sid", "workspace_write_sid"]
