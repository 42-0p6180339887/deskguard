"""Visible Windows notification-area controls and optional global hotkeys.

No keyboard content is captured. The supplied callback is invoked on a daemon
message thread and must only queue commands for the application's UI thread.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading
from typing import Callable
import uuid


if os.name == "nt":
    _LRESULT = ctypes.c_ssize_t
    _WPARAM = ctypes.c_size_t
    _LPARAM = ctypes.c_ssize_t
    _WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM)

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", _WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", wintypes.BYTE * 8),
        ]

    class _NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", _GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]


class DesktopControls:
    """Own one visible tray icon and Ctrl+Alt+F9/F10 registrations.

    ``start()`` succeeds when the tray is usable even if hotkeys conflict.
    Inspect ``hotkeys_available`` and ``hotkey_errors`` for that partial case.
    Calling ``stop()`` releases the icon, window, and all registered hotkeys.
    """

    _WM_TRAY = 0x8001
    _WM_STATUS = 0x8002
    _WM_STOP = 0x8003
    _HOTKEY_TOGGLE = 1
    _HOTKEY_SHOW = 2

    def __init__(self, callback: Callable[[str], None], icon_path: os.PathLike[str] | str | None = None):
        self._callback = callback
        self._icon_path = os.fspath(icon_path) if icon_path is not None else None
        self._custom_icon = None
        self.error = ""
        self.hotkeys_available = False
        self.hotkey_errors: list[str] = []
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._lock = threading.Lock()
        self._text = "离席守护 · 未启用"
        self._armed = False
        self._hwnd = None
        self._started = False
        self._icon_added = False
        self._hotkeys: list[int] = []
        self._class_name = "DeskGuardControls_" + uuid.uuid4().hex

    def start(self) -> bool:
        if os.name != "nt":
            self.error = "托盘和快捷键只支持 Windows。"
            return False
        if self._thread is not None and self._thread.is_alive():
            return self._started
        self.error = ""
        self.hotkey_errors = []
        self.hotkeys_available = False
        self._ready.clear()
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._run, name="DeskGuardTray", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            self.error = "Windows 托盘启动超时，请保持控制面板打开。"
            self.stop()
            return False
        return self._started

    def set_status(self, text: str, armed: bool) -> None:
        with self._lock:
            self._text = str(text)
            self._armed = bool(armed)
        if self._hwnd:
            self._user32.PostMessageW(self._hwnd, self._WM_STATUS, 0, 0)

    @property
    def running(self) -> bool:
        return bool(self._started and self._thread and self._thread.is_alive())

    def stop(self) -> None:
        self._stop_requested.set()
        if self._hwnd:
            self._user32.PostMessageW(self._hwnd, self._WM_STOP, 0, 0)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)

    def _setup_apis(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        declarations = [
            (self._kernel32, "GetModuleHandleW", [wintypes.LPCWSTR], wintypes.HMODULE),
            (self._user32, "RegisterClassW", [ctypes.POINTER(_WNDCLASSW)], wintypes.ATOM),
            (self._user32, "UnregisterClassW", [wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL),
            (self._user32, "CreateWindowExW", [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HWND),
            (self._user32, "DefWindowProcW", [wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM], _LRESULT),
            (self._user32, "DestroyWindow", [wintypes.HWND], wintypes.BOOL),
            (self._user32, "GetMessageW", [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT], ctypes.c_int),
            (self._user32, "TranslateMessage", [ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
            (self._user32, "DispatchMessageW", [ctypes.POINTER(wintypes.MSG)], _LRESULT),
            (self._user32, "PostMessageW", [wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM], wintypes.BOOL),
            (self._user32, "PostQuitMessage", [ctypes.c_int], None),
            (self._user32, "RegisterHotKey", [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
            (self._user32, "UnregisterHotKey", [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            (self._user32, "LoadIconW", [wintypes.HINSTANCE, wintypes.LPCWSTR], wintypes.HICON),
            (self._user32, "LoadImageW", [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.HANDLE),
            (self._user32, "DestroyIcon", [wintypes.HICON], wintypes.BOOL),
            (self._user32, "GetSystemMetrics", [ctypes.c_int], ctypes.c_int),
            (self._user32, "RegisterWindowMessageW", [wintypes.LPCWSTR], wintypes.UINT),
            (self._user32, "CreatePopupMenu", [], wintypes.HMENU),
            (self._user32, "AppendMenuW", [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR], wintypes.BOOL),
            (self._user32, "DestroyMenu", [wintypes.HMENU], wintypes.BOOL),
            (self._user32, "GetCursorPos", [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            (self._user32, "SetForegroundWindow", [wintypes.HWND], wintypes.BOOL),
            (self._user32, "TrackPopupMenu", [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p], wintypes.UINT),
            (self._shell32, "Shell_NotifyIconW", [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)], wintypes.BOOL),
        ]
        for library, name, arguments, result in declarations:
            function = getattr(library, name)
            function.argtypes = arguments
            function.restype = result

    def _run(self) -> None:
        registered_class = False
        instance = None
        try:
            self._setup_apis()
            instance = self._kernel32.GetModuleHandleW(None)
            self._window_callback = _WNDPROC(self._window_proc)
            window_class = _WNDCLASSW()
            window_class.lpfnWndProc = self._window_callback
            window_class.hInstance = instance
            window_class.lpszClassName = self._class_name
            if not self._user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered_class = True
            self._taskbar_created = self._user32.RegisterWindowMessageW("TaskbarCreated")
            self._hwnd = self._user32.CreateWindowExW(
                0, self._class_name, "离席守护通知控制", 0,
                0, 0, 0, 0, None, None, instance, None,
            )
            if not self._hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            if self._stop_requested.is_set():
                return
            self._load_custom_icon()
            if not self._update_icon(add=True):
                raise RuntimeError("无法创建 Windows 通知区域图标。")
            for identifier, key, label in (
                (self._HOTKEY_TOGGLE, 0x78, "Ctrl+Alt+F9"),
                (self._HOTKEY_SHOW, 0x79, "Ctrl+Alt+F10"),
            ):
                if self._user32.RegisterHotKey(self._hwnd, identifier, 0x4003, key):
                    self._hotkeys.append(identifier)
                else:
                    code = ctypes.get_last_error()
                    self.hotkey_errors.append(f"{label} 注册失败（错误 {code}），可能已被其他程序占用。")
            self.hotkeys_available = len(self._hotkeys) == 2
            if self.hotkey_errors:
                self.error = "\n".join(self.hotkey_errors) + " 可使用托盘菜单。"
            self._started = True
            self._ready.set()
            message = wintypes.MSG()
            while not self._stop_requested.is_set():
                result = self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._user32.TranslateMessage(ctypes.byref(message))
                self._user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self.error = f"托盘控制不可用：{exc}"
        finally:
            self._started = False
            self.hotkeys_available = False
            if self._hwnd:
                for identifier in self._hotkeys:
                    self._user32.UnregisterHotKey(self._hwnd, identifier)
                self._hotkeys.clear()
                if self._icon_added:
                    data = _NOTIFYICONDATAW()
                    data.cbSize = ctypes.sizeof(data)
                    data.hWnd = self._hwnd
                    data.uID = 1
                    self._shell32.Shell_NotifyIconW(2, ctypes.byref(data))
                    self._icon_added = False
                self._user32.DestroyWindow(self._hwnd)
                self._hwnd = None
            self._release_custom_icon()
            if registered_class:
                self._user32.UnregisterClassW(self._class_name, instance)
            self._ready.set()

    def _load_custom_icon(self) -> None:
        if self._icon_path and not self._custom_icon:
            width = self._user32.GetSystemMetrics(49) or 16  # SM_CXSMICON
            height = self._user32.GetSystemMetrics(50) or 16  # SM_CYSMICON
            # File icons are private handles: never use LR_SHARED here.
            self._custom_icon = self._user32.LoadImageW(
                None, self._icon_path, 1, width, height, 0x10  # IMAGE_ICON, LR_LOADFROMFILE
            )

    def _release_custom_icon(self) -> None:
        if self._custom_icon:
            self._user32.DestroyIcon(self._custom_icon)
            self._custom_icon = None

    def _update_icon(self, add: bool = False) -> bool:
        with self._lock:
            text, armed = self._text, self._armed
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = 0x01 | 0x02 | 0x04  # message, icon, tooltip; no balloon
        data.uCallbackMessage = self._WM_TRAY
        # The artwork stays constant; the tooltip/menu describe actual state.
        if self._custom_icon:
            data.hIcon = self._custom_icon
        else:
            stock_id = 32515 if armed else 32516  # warning / information
            data.hIcon = self._user32.LoadIconW(None, ctypes.cast(ctypes.c_void_p(stock_id), wintypes.LPCWSTR))
        # Truncate by UTF-16 units so emoji cannot overflow the native buffer.
        encoded = text.encode("utf-16-le", errors="replace")[:254]
        data.szTip = encoded.decode("utf-16-le", errors="ignore")
        result = bool(self._shell32.Shell_NotifyIconW(0 if add else 1, ctypes.byref(data)))
        if result:
            self._icon_added = True
        return result

    def _refresh_icon_or_stop(self, *, add: bool = False) -> None:
        """Recover a missing/existing icon or return control to the main UI."""
        detail = ""
        try:
            # Explorer may have lost the icon before a status update, or may
            # broadcast TaskbarCreated after another update already restored it.
            # Try the complementary operation before treating it as unavailable.
            if self._update_icon(add=add) or self._update_icon(add=not add):
                return
        except Exception as exc:
            detail = " " + str(exc)
        self.error = "Windows 托盘图标无法恢复，已停用托盘控制；请使用主窗口操作。" + detail
        # Publish this immediately: GuardApp.tick() detects running=False and
        # shows the main window even while this thread is releasing its handles.
        self._started = False
        self.hotkeys_available = False
        self._stop_requested.set()
        self._user32.PostQuitMessage(0)

    def _dispatch(self, command: str) -> None:
        try:
            self._callback(command)
        except Exception as exc:
            self.error = f"托盘命令处理失败：{exc}"

    def _show_menu(self) -> None:
        menu = self._user32.CreatePopupMenu()
        if not menu:
            return
        try:
            with self._lock:
                armed = self._armed
            toggle = "停止守护" if armed else "启用守护"
            self._user32.AppendMenuW(menu, 0, 1, toggle + "\tCtrl+Alt+F9")
            self._user32.AppendMenuW(menu, 0, 2, "打开控制面板\tCtrl+Alt+F10")
            self._user32.AppendMenuW(menu, 0x800, 0, None)
            self._user32.AppendMenuW(menu, 0, 3, "退出离席守护")
            point = wintypes.POINT()
            if not self._user32.GetCursorPos(ctypes.byref(point)):
                return
            self._user32.SetForegroundWindow(self._hwnd)
            selected = self._user32.TrackPopupMenu(
                menu, 0x100 | 0x80 | 0x02, point.x, point.y, 0, self._hwnd, None
            )
            self._user32.PostMessageW(self._hwnd, 0, 0, 0)
            command = {1: "toggle", 2: "show", 3: "quit"}.get(selected)
            if command:
                self._dispatch(command)
        finally:
            self._user32.DestroyMenu(menu)

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == self._WM_STOP:
                self._user32.PostQuitMessage(0)
                return 0
            if message == 0x02:  # WM_DESTROY
                self._user32.PostQuitMessage(0)
                return 0
            if message == self._WM_STATUS:
                self._refresh_icon_or_stop()
                return 0
            if message == self._WM_TRAY:
                if lparam in (0x205, 0x7B):  # right-click / context menu
                    self._show_menu()
                elif lparam in (0x203, 0x400, 0x401):  # double-click / keyboard select
                    self._dispatch("show")
                return 0
            if message == 0x312:  # WM_HOTKEY
                if wparam == self._HOTKEY_TOGGLE:
                    self._dispatch("toggle")
                elif wparam == self._HOTKEY_SHOW:
                    self._dispatch("toggle_window")
                return 0
            if message == getattr(self, "_taskbar_created", -1):
                self._refresh_icon_or_stop(add=True)
                return 0
        except Exception as exc:
            self.error = f"托盘消息处理失败：{exc}"
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)
