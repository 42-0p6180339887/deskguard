"""Open the packaged update window without touching DeskGuard's user files."""

import ctypes
from pathlib import Path
import subprocess


def _system_powershell():
    # Resolve the Windows component through the OS, never the working directory/PATH.
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError("无法定位 Windows PowerShell。")
    executable = Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_file():
        raise OSError("Windows PowerShell 不可用。")
    return executable


def _regular_packaged_file(path, parent):
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise OSError("更新组件不能使用符号链接或目录联接。")
    resolved = path.resolve(strict=True)
    if resolved.parent != parent or not resolved.is_file():
        raise OSError("更新组件不在程序目录中。")
    return resolved


def launch_updater(app_dir, startup=False):
    base = Path(app_dir).resolve(strict=True)
    candidate = base / "updates"
    if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
        raise OSError("更新组件不能使用符号链接或目录联接。")
    resource_dir = candidate.resolve(strict=True)
    if resource_dir.parent != base or not resource_dir.is_dir():
        raise OSError("更新组件不在程序目录中。")
    script = _regular_packaged_file(resource_dir / "AppUpdates.ps1", resource_dir)
    config = _regular_packaged_file(resource_dir / "app-update.json", resource_dir)
    return subprocess.Popen(
        [str(_system_powershell()), "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
         "-File", str(script), "-Config", str(config)] + (["-StartupCheck"] if startup else []),
        cwd=str(base), shell=False, creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
