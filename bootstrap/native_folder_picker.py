"""Windows 原生目录选择与资源管理器 Host 能力。"""

from __future__ import annotations

import ctypes
import ipaddress
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Protocol
from uuid import UUID


class NativeHostError(RuntimeError):
    """本机 Host 能力执行失败。"""


class NativeHostUnavailable(NativeHostError):
    """当前平台不支持请求的 Host 能力。"""


class NativePickerBusy(NativeHostError):
    """已经有一个原生目录选择器等待用户操作。"""


class DirectoryPicker(Protocol):
    """供 FastAPI 注入和测试替换的最小目录选择接口。"""

    def pick_directory(self) -> Path | None: ...

    def open_directory(self, path: Path) -> None: ...


class WindowsDirectoryPicker:
    """串行打开 Windows IFileOpenDialog，取消时返回 None。"""

    def __init__(self) -> None:
        self._dialog_lock = threading.Lock()

    def pick_directory(self) -> Path | None:
        if sys.platform != "win32":
            raise NativeHostUnavailable("原生目录选择器当前仅支持 Windows")
        if not self._dialog_lock.acquire(blocking=False):
            raise NativePickerBusy("已有目录选择窗口等待操作")
        try:
            selected = _pick_windows_directory()
            return Path(selected) if selected else None
        except NativeHostError:
            raise
        except Exception as error:
            raise NativeHostError(f"Windows 目录选择器失败：{error}") from error
        finally:
            self._dialog_lock.release()

    def open_directory(self, path: Path) -> None:
        if sys.platform != "win32":
            raise NativeHostUnavailable("在资源管理器中打开当前仅支持 Windows")
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as error:
            raise NativeHostError(f"无法在资源管理器中打开目录：{error}") from error


def is_loopback_client(host: str | None) -> bool:
    """只接受明确的回环地址，未知主机名按远程请求拒绝。"""

    value = str(host or "").strip().split("%", 1)[0]
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, value: str) -> _GUID:
        raw = UUID(value).bytes_le
        return cls(
            int.from_bytes(raw[0:4], "little"),
            int.from_bytes(raw[4:6], "little"),
            int.from_bytes(raw[6:8], "little"),
            (ctypes.c_ubyte * 8).from_buffer_copy(raw[8:]),
        )


_CLSID_FILE_OPEN_DIALOG = _GUID.parse("dc1c5a9c-e88a-4dde-a5a1-60f82a20aef7")
_IID_FILE_OPEN_DIALOG = _GUID.parse("d57c7288-d4ad-4768-be02-9d969532d960")
_CLSCTX_INPROC_SERVER = 0x1
_COINIT_APARTMENTTHREADED = 0x2
_FOS_PICKFOLDERS = 0x20
_FOS_FORCEFILESYSTEM = 0x40
_FOS_PATHMUSTEXIST = 0x800
_FOS_DONTADDTORECENT = 0x02000000
_SIGDN_FILESYSPATH = 0x80058000
_ERROR_CANCELLED_HRESULT = ctypes.c_long(0x800704C7).value


def _com_method(
    instance: ctypes.c_void_p,
    index: int,
    restype: object,
    *argtypes: object,
):
    vtable = ctypes.cast(
        instance,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
    ).contents
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    function = prototype(vtable[index])
    return lambda *args: function(instance, *args)


def _check_hresult(result: int, operation: str) -> None:
    if result < 0:
        unsigned = ctypes.c_ulong(result).value
        raise NativeHostError(f"{operation} 失败（HRESULT 0x{unsigned:08X}）")


def _release(instance: ctypes.c_void_p | None) -> None:
    if instance and instance.value:
        _com_method(instance, 2, wintypes.ULONG)()


def _foreground_window() -> wintypes.HWND | None:
    """取得用户当前正在操作的窗口，供系统选择器建立 owner 关系。"""

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsWindow.argtypes = (wintypes.HWND,)
    user32.IsWindow.restype = wintypes.BOOL
    owner = user32.GetForegroundWindow()
    return owner if owner and user32.IsWindow(owner) else None


def _pick_windows_directory() -> str | None:
    """在当前 STA 线程阻塞显示现代 Windows 文件夹选择器。"""

    # HTTP Host 并不拥有浏览器窗口；显式绑定当前前台窗口可让系统对话框直接置前。
    owner = _foreground_window()
    ole32 = ctypes.OleDLL("ole32")
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = ()
    ole32.CoCreateInstance.argtypes = (
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)

    initialized = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    _check_hresult(initialized, "初始化 Windows COM")
    dialog = ctypes.c_void_p()
    item = ctypes.c_void_p()
    raw_path = ctypes.c_void_p()
    try:
        _check_hresult(
            ole32.CoCreateInstance(
                ctypes.byref(_CLSID_FILE_OPEN_DIALOG),
                None,
                _CLSCTX_INPROC_SERVER,
                ctypes.byref(_IID_FILE_OPEN_DIALOG),
                ctypes.byref(dialog),
            ),
            "创建 Windows 文件夹选择器",
        )
        options = wintypes.DWORD()
        _check_hresult(
            _com_method(dialog, 10, ctypes.c_long, ctypes.POINTER(wintypes.DWORD))(
                ctypes.byref(options)
            ),
            "读取目录选择器选项",
        )
        options.value |= (
            _FOS_PICKFOLDERS
            | _FOS_FORCEFILESYSTEM
            | _FOS_PATHMUSTEXIST
            | _FOS_DONTADDTORECENT
        )
        _check_hresult(
            _com_method(dialog, 9, ctypes.c_long, wintypes.DWORD)(options),
            "设置目录选择器选项",
        )
        _check_hresult(
            _com_method(dialog, 17, ctypes.c_long, wintypes.LPCWSTR)(
                "选择 BeanAgent 工作目录"
            ),
            "设置目录选择器标题",
        )
        shown = _com_method(dialog, 3, ctypes.c_long, wintypes.HWND)(owner)
        if shown == _ERROR_CANCELLED_HRESULT:
            return None
        _check_hresult(shown, "显示目录选择器")
        _check_hresult(
            _com_method(dialog, 20, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(item)
            ),
            "读取选中目录",
        )
        _check_hresult(
            _com_method(
                item,
                5,
                ctypes.c_long,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_void_p),
            )(_SIGDN_FILESYSPATH, ctypes.byref(raw_path)),
            "读取选中目录路径",
        )
        return ctypes.wstring_at(raw_path.value)
    finally:
        if raw_path.value:
            ole32.CoTaskMemFree(raw_path)
        _release(item)
        _release(dialog)
        ole32.CoUninitialize()


__all__ = [
    "DirectoryPicker",
    "NativeHostError",
    "NativeHostUnavailable",
    "NativePickerBusy",
    "WindowsDirectoryPicker",
    "is_loopback_client",
]
