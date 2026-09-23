"""Start and stop the optional native, click-through desktop goose."""

from __future__ import annotations

from pathlib import Path
import subprocess


class DesktopPet:
    """Own the patrol child process; closing its stdin also closes the pet."""

    def __init__(self, owner, image_path: Path):
        self.image_path = Path(image_path)
        self.host_path = self.image_path.parent.parent / "_app" / "PetHost.exe"
        self._process: subprocess.Popen | None = None
        self._options = ("large", "normal", False)

    def configure(self, *, size="large", speed="normal", gentle=False) -> None:
        if size not in ("small", "medium", "large") or speed not in ("slow", "normal", "brisk"):
            raise ValueError("桌宠大小或速度选项无效。")
        options = (size, speed, bool(gentle))
        if options == self._options:
            return
        was_visible = self.visible
        self._options = options
        if was_visible:
            self.close()
            self.set_visible(True)

    @property
    def visible(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def set_visible(self, visible: bool) -> None:
        if not visible:
            self.close()
            return
        if self.visible:
            return
        self.close()
        alternate = self.image_path.with_name("deskguard-pet-step.png")
        for path in (self.host_path, self.image_path, alternate):
            if not path.is_file():
                raise FileNotFoundError(f"桌宠文件缺失：{path}")
        size, speed, gentle = self._options
        self._process = subprocess.Popen(
            [str(self.host_path), str(self.image_path), str(alternate),
             size, speed, "gentle" if gentle else "full"],
            cwd=str(self.image_path.parent.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            exit_code = self._process.wait(timeout=0.15)
        except subprocess.TimeoutExpired:
            return
        if self._process.stdin is not None:
            self._process.stdin.close()
        self._process = None
        raise RuntimeError(f"桌宠启动失败（退出码 {exit_code}）。")

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
