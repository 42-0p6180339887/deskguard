"""Optional click-through goose mascot for visible desktop patrol mode."""

from __future__ import annotations

import math
import os
from pathlib import Path
import tkinter as tk


_KEY = "#ff00ff"
_STEP_MS = 100
_STEP_PIXELS = 7
_MARGIN = 8
_TASKBAR_CLEARANCE = 48


def advance(x: int, y: int, edge: int, max_x: int, max_y: int, step: int = _STEP_PIXELS) -> tuple[int, int, int]:
    """Move clockwise around the primary work area without overshooting corners."""
    if edge == 0:
        x = min(max_x, x + step)
        if x >= max_x:
            edge = 1
    elif edge == 1:
        y = min(max_y, y + step)
        if y >= max_y:
            edge = 2
    elif edge == 2:
        x = max(_MARGIN, x - step)
        if x <= _MARGIN:
            edge = 3
    else:
        y = max(_MARGIN, y - step)
        if y <= _MARGIN:
            edge = 0
    return x, y, edge


class DesktopPet:
    """A non-activating transparent goose window that never consumes clicks."""

    def __init__(self, owner: tk.Misc, image_path: Path):
        self.owner = owner
        self.image_path = Path(image_path)
        self.window: tk.Toplevel | None = None
        self.photo: tk.PhotoImage | None = None
        self._label: tk.Label | None = None
        self._user32 = None
        self._hwnd = None
        self._after_id = None
        self._visible = False
        self._x = float(_MARGIN)
        self._y = float(_MARGIN)
        self._edge = 0
        self._max_x = 0
        self._max_y = 0
        self.width = 0
        self.height = 0

    @property
    def visible(self) -> bool:
        return self._visible

    def set_visible(self, visible: bool) -> None:
        visible = bool(visible)
        if visible == self._visible:
            return
        if not visible:
            self._visible = False
            self._cancel_tick()
            if self.window is not None:
                self.window.withdraw()
            return
        self._create_window()
        self._visible = True
        self._x = float(_MARGIN)
        self._y = float(_MARGIN)
        self._edge = 0
        self.window.deiconify()
        self._position(show=True)
        self._schedule_tick()

    def _create_window(self) -> None:
        if self.window is not None:
            return
        window = tk.Toplevel(self.owner)
        window.withdraw()
        window.overrideredirect(True)
        window.configure(background=_KEY, borderwidth=0, highlightthickness=0)
        window.wm_attributes("-transparentcolor", _KEY)
        window.wm_attributes("-topmost", True)
        image = tk.PhotoImage(master=window, file=str(self.image_path))
        shrink = max(1, math.ceil(max(image.width(), image.height()) / 160))
        self.photo = image.subsample(shrink, shrink) if shrink > 1 else image
        self.width, self.height = self.photo.width(), self.photo.height()
        label = tk.Label(window, image=self.photo, background=_KEY, borderwidth=0,
                         highlightthickness=0, takefocus=False)
        label.pack()
        window.geometry(f"{self.width}x{self.height}+0+0")
        window.update_idletasks()
        self.window = window
        self._label = label
        try:
            self._set_native_clickthrough(window.winfo_id())
            screen_width, screen_height = window.winfo_screenwidth(), window.winfo_screenheight()
            self._max_x = max(_MARGIN, screen_width - self.width - _MARGIN)
            self._max_y = max(_MARGIN, screen_height - self.height - _TASKBAR_CLEARANCE)
        except Exception:
            self._destroy_window()
            raise

    def _set_native_clickthrough(self, hwnd: int) -> None:
        import ctypes
        from ctypes import wintypes

        if os.name != "nt":
            raise OSError("桌宠巡逻模式只支持 Windows。")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        get_long = user32.GetWindowLongPtrW
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        get_long.restype = ctypes.c_ssize_t
        set_long = user32.SetWindowLongPtrW
        set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_long.restype = ctypes.c_ssize_t
        user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.BYTE, wintypes.DWORD]
        user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL

        # EX_TRANSPARENT only passes mouse input through for a layered window.
        current = get_long(hwnd, -20)  # GWL_EXSTYLE
        styles = current | 0x00080000 | 0x00000020 | 0x00000080 | 0x08000000
        ctypes.set_last_error(0)
        if not set_long(hwnd, -20, styles) and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        # Detach the Toplevel from the hidden control-panel owner.
        ctypes.set_last_error(0)
        set_long(hwnd, -8, 0)  # GWL_HWNDPARENT for a top-level window is its owner.
        if ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        if not user32.SetLayeredWindowAttributes(hwnd, 0x00FF00FF, 0, 0x01):
            raise ctypes.WinError(ctypes.get_last_error())
        self._user32 = user32
        self._hwnd = hwnd

    def _position(self, *, show: bool = False) -> None:
        if self.window is None:
            return
        x, y = round(self._x), round(self._y)
        self.window.geometry(f"{self.width}x{self.height}+{x}+{y}")
        if self._user32 is not None and self._hwnd is not None:
            flags = 0x0010 | (0x0040 if show else 0x0001 | 0x0002)  # NOACTIVATE; SHOW or NOMOVE/NOSIZE
            insert_after = -1  # HWND_TOPMOST
            if not self._user32.SetWindowPos(self._hwnd, insert_after, x, y, self.width, self.height, flags):
                import ctypes
                raise ctypes.WinError(ctypes.get_last_error())

    def _schedule_tick(self) -> None:
        if self._visible and self.window is not None and self._after_id is None:
            self._after_id = self.window.after(_STEP_MS, self._tick)

    def _tick(self) -> None:
        self._after_id = None
        if not self._visible or self.window is None:
            return
        self._x, self._y, self._edge = advance(
            round(self._x), round(self._y), self._edge, self._max_x, self._max_y
        )
        self._position()
        self._schedule_tick()

    def _cancel_tick(self) -> None:
        if self._after_id is not None and self.window is not None:
            try:
                self.window.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _destroy_window(self) -> None:
        self._cancel_tick()
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
        self.window = None
        self.photo = None
        self._label = None
        self._hwnd = None
        self._user32 = None
        self._visible = False

    def close(self) -> None:
        self._destroy_window()
