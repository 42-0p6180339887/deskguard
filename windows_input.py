"""Windows activity signals without storing keys, mouse positions, or input text.

Nothing starts at import time. ``InputMonitor.start`` installs low-level hooks
only on the caller's interactive desktop. It counts eligible notifications; it
does not identify their source application or prove that a person is present.
The injected flag is a best-effort automation filter, not an identity check.
Touch promoted to mouse events may be observed, but neither every touch gesture
nor its treatment by this filter is guaranteed. Use the compatibility timestamp
mode if required after testing the actual touchscreen.

Microsoft references:
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowshookexw
https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelkeyboardproc
https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-kbdllhookstruct
https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-msllhookstruct
https://learn.microsoft.com/en-us/windows/win32/winstation/desktops
"""

from __future__ import annotations

import ctypes
import math
import os
import threading


# Fixed-width Windows types also make the ABI declarations inspectable without
# loading a DLL. WPARAM/LPARAM/LRESULT must remain pointer-sized on 64-bit Python.
DWORD = ctypes.c_uint32
UINT = ctypes.c_uint32
BOOL = ctypes.c_int32
LONG = ctypes.c_int32
HANDLE = ctypes.c_void_p
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
HOOKPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    LRESULT, ctypes.c_int, WPARAM, LPARAM
)

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01
WM_QUIT = 0x0012
PM_NOREMOVE = 0
DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2
UOI_IO = 6
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", HANDLE),
        ("message", UINT),
        ("wParam", WPARAM),
        ("lParam", LPARAM),
        ("time", DWORD),
        ("pt", POINT),
        ("lPrivate", DWORD),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    # The first eight bytes contain vkCode/scanCode. They are deliberately
    # unnamed input content. The callback reads only the flags word by offset.
    _fields_ = [
        ("_unused_key_fields", ctypes.c_ubyte * 8),
        ("flags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    # Skip cursor coordinates and mouseData; neither is needed or recorded.
    _fields_ = [
        ("_unused_mouse_fields", ctypes.c_ubyte * 12),
        ("flags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", UINT), ("dwTime", DWORD)]


def should_count_input(flags: int, *, keyboard: bool, ignore_injected: bool) -> bool:
    """Pure policy helper: an injected notification is excluded when requested."""
    mask = LLKHF_INJECTED if keyboard else LLMHF_INJECTED
    return not (ignore_injected and bool(flags & mask))


class _WinAPI:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("This activity monitor requires Windows.")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        def declare(dll, name, args, result):
            function = getattr(dll, name)
            function.argtypes = args
            function.restype = result
            setattr(self, name, function)

        declare(self.user32, "SetWindowsHookExW", [ctypes.c_int, HOOKPROC, HANDLE, DWORD], HANDLE)
        declare(self.user32, "UnhookWindowsHookEx", [HANDLE], BOOL)
        declare(self.user32, "CallNextHookEx", [HANDLE, ctypes.c_int, WPARAM, LPARAM], LRESULT)
        declare(self.user32, "PeekMessageW", [ctypes.POINTER(MSG), HANDLE, UINT, UINT, UINT], BOOL)
        # GetMessage must be signed: -1 is an error, 0 is WM_QUIT.
        declare(self.user32, "GetMessageW", [ctypes.POINTER(MSG), HANDLE, UINT, UINT], ctypes.c_int)
        declare(self.user32, "PostThreadMessageW", [DWORD, UINT, WPARAM, LPARAM], BOOL)
        declare(self.user32, "GetLastInputInfo", [ctypes.POINTER(LASTINPUTINFO)], BOOL)
        declare(self.user32, "OpenInputDesktop", [DWORD, BOOL, DWORD], HANDLE)
        declare(self.user32, "GetUserObjectInformationW", [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD)], BOOL)
        declare(self.user32, "CloseDesktop", [HANDLE], BOOL)
        declare(self.kernel32, "GetCurrentThreadId", [], DWORD)
        declare(self.kernel32, "GetModuleHandleW", [ctypes.c_wchar_p], HANDLE)
        declare(self.kernel32, "SetThreadExecutionState", [DWORD], DWORD)


_api_instance: _WinAPI | None = None
_api_lock = threading.Lock()


def _api() -> _WinAPI:
    global _api_instance
    with _api_lock:
        if _api_instance is None:
            _api_instance = _WinAPI()
        return _api_instance


def _native_error(operation: str) -> OSError:
    code = ctypes.get_last_error()
    detail = ctypes.FormatError(code).strip() if code else "Windows returned failure"
    return OSError(code, f"{operation}: {detail}")


class InputMonitor:
    """Count mouse/keyboard notifications; explicitly call start() to enable.

    ``start(timeout)`` returns True only after both hooks and the message queue
    are ready. It returns False and sets ``error`` on failure. ``stop()`` waits
    at most two seconds for cleanup and reports a failure through ``error``.
    A snapshot is only a sequence counter, with no event payload or timestamp.
    """

    def __init__(self, ignore_injected: bool = True) -> None:
        self.ignore_injected = bool(ignore_injected)
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._error: str | None = None
        self._callbacks: tuple | None = None
        self._started = False

    @property
    def error(self) -> str | None:
        return self._error

    def snapshot(self) -> int:
        with self._sequence_lock:
            return self._sequence

    def start(self, timeout: float = 3.0) -> bool:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return self._started and not self._stop_requested.is_set() and self._error is None
            self._error = None
            self._started = False
            self._ready.clear()
            self._stop_requested.clear()
            self._thread_id = None
            self._thread = threading.Thread(
                target=self._run, name="DeskGuard input activity", daemon=True
            )
            self._thread.start()
            if not self._ready.wait(timeout):
                self._error = "Input monitoring did not start before the timeout."
                self._request_stop()
                self._thread.join(timeout=1.0)
                return False
            return self._started and self._error is None

    def _request_stop(self) -> None:
        self._stop_requested.set()
        thread_id = self._thread_id
        if thread_id is not None:
            # Queue creation precedes publication of _thread_id. A false return
            # can simply mean the worker already finished; stop() checks that.
            try:
                _api().PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
            except OSError:
                pass

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._request_stop()
            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)
                if thread.is_alive():
                    self._error = "Input monitoring did not stop before the timeout."

    def _run(self) -> None:
        api = None
        hooks: list[int] = []
        cleanup_ok = True
        try:
            api = _api()
            message = MSG()
            # PeekMessage creates this thread's queue before start can succeed
            # or another thread can request a WM_QUIT shutdown.
            api.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_NOREMOVE)
            self._thread_id = int(api.GetCurrentThreadId())
            if self._stop_requested.is_set():
                return

            def make_callback(flags_offset: int, keyboard: bool):
                @HOOKPROC
                def callback(code, wparam, lparam):
                    try:
                        if code == HC_ACTION and lparam and not self._stop_requested.is_set():
                            # Access exactly the flags word, never the key or
                            # cursor fields. No I/O, camera work, or user callback.
                            flags = DWORD.from_address(lparam + flags_offset).value
                            if should_count_input(
                                flags,
                                keyboard=keyboard,
                                ignore_injected=self.ignore_injected,
                            ):
                                with self._sequence_lock:
                                    self._sequence += 1
                    except Exception:
                        self._error = "Input activity callback failed."
                    # Never suppress input, including injected notifications.
                    return api.CallNextHookEx(None, code, wparam, lparam)

                return callback

            keyboard_callback = make_callback(KBDLLHOOKSTRUCT.flags.offset, True)
            mouse_callback = make_callback(MSLLHOOKSTRUCT.flags.offset, False)
            self._callbacks = (keyboard_callback, mouse_callback)
            module = api.GetModuleHandleW(None)
            if not module:
                raise _native_error("GetModuleHandleW")
            for kind, callback in (
                (WH_KEYBOARD_LL, keyboard_callback),
                (WH_MOUSE_LL, mouse_callback),
            ):
                if self._stop_requested.is_set():
                    return
                hook = api.SetWindowsHookExW(kind, callback, module, 0)
                if not hook:
                    raise _native_error("SetWindowsHookExW")
                hooks.append(hook)
            if self._stop_requested.is_set():
                return
            self._started = True
            self._ready.set()
            while not self._stop_requested.is_set():
                result = api.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise _native_error("GetMessageW")
                if result == 0:
                    break
                # Low-level callbacks are dispatched during GetMessage. This
                # thread owns no windows and needs no content message handling.
        except Exception as exc:
            self._error = str(exc)
        finally:
            if not self._stop_requested.is_set() and self._error is None:
                self._error = "Input monitoring stopped unexpectedly."
            if api is not None:
                for hook in reversed(hooks):
                    if not api.UnhookWindowsHookEx(hook):
                        cleanup_ok = False
                        self._error = str(_native_error("UnhookWindowsHookEx"))
            self._started = False
            self._thread_id = None
            # Retain callbacks if unhook reported failure; do not free a callback
            # while a native hook may still hold its function pointer.
            if cleanup_ok:
                self._callbacks = None
            self._ready.set()


def read_last_input_tick() -> int:
    """Get current-session activity time for compatibility mode, including automation.

    The DWORD can wrap or move backwards; compare !=, never >. This is an input
    activity signal, not a per-event counter or a hardware-only detector.
    https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getlastinputinfo
    """
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not _api().GetLastInputInfo(ctypes.byref(info)):
        raise _native_error("GetLastInputInfo")
    return int(info.dwTime)


def keep_awake(enable: bool) -> bool:
    """Set/clear this calling thread's idle-sleep request, without simulating input.

    Always enable and disable from the same long-lived thread. Does not request
    display power, disable locking, or prevent explicit user-requested sleep.
    https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate
    """
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enable else 0)
    if not _api().SetThreadExecutionState(flags):
        raise OSError("SetThreadExecutionState failed.")
    return True


def is_interactive_desktop() -> bool:
    """Return True only when the readable input desktop is the active Default.

    Fail closed on access denial or query failure. This never switches desktop
    or changes security permissions. Call before accepting a capture trigger.
    https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-openinputdesktop
    https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getuserobjectinformationw
    """
    try:
        api = _api()
        desktop = api.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not desktop:
            return False
        try:
            name = ctypes.create_unicode_buffer(256)
            needed = DWORD()
            if not api.GetUserObjectInformationW(
                desktop, UOI_NAME, name, ctypes.sizeof(name), ctypes.byref(needed)
            ):
                return False
            if name.value.casefold() != "default":
                return False
            receiving_input = BOOL()
            if not api.GetUserObjectInformationW(
                desktop, UOI_IO, ctypes.byref(receiving_input),
                ctypes.sizeof(receiving_input), ctypes.byref(needed)
            ):
                return False
            return bool(receiving_input.value)
        finally:
            api.CloseDesktop(desktop)
    except OSError:
        return False
