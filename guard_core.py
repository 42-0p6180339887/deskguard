"""Input coalescing and local photo storage; no camera or input capture here."""

from __future__ import annotations

from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import shutil
import tempfile
import uuid


_LOG = logging.getLogger(__name__)
_PHOTO_SUFFIXES = {".jpg", ".jpeg"}


class TriggerGate:
    """Turn changed Windows input ticks into rate-limited capture requests.

    Call ``reset`` on arming, then pass a DWORD input tick and a monotonic time
    to ``due``. Input during the cooldown is coalesced into one pending request.
    The gate never requests another photo without a newly observed input tick.
    """

    def __init__(self, interval_seconds: float):
        interval_seconds = float(interval_seconds)
        if not math.isfinite(interval_seconds) or interval_seconds < 0:
            raise ValueError("拍摄间隔必须是大于或等于 0 的有限秒数。")
        self.interval_seconds = interval_seconds
        self._last_input_tick: int | None = None
        self._last_capture_at: float | None = None
        self._pending = False

    def reset(self, last_input_tick: int, now: float) -> None:
        if not math.isfinite(now):
            raise ValueError("计时值必须是有限数值。")
        self._last_input_tick = int(last_input_tick) & 0xFFFFFFFF
        self._last_capture_at = None
        self._pending = False

    def due(self, last_input_tick: int, now: float) -> bool:
        if self._last_input_tick is None:
            raise RuntimeError("启用监测前必须先重置触发器。")
        if not math.isfinite(now):
            raise ValueError("计时值必须是有限数值。")
        tick = int(last_input_tick) & 0xFFFFFFFF
        if tick != self._last_input_tick:
            self._last_input_tick = tick
            self._pending = True
        if self._pending and (
            self._last_capture_at is None
            or now - self._last_capture_at >= self.interval_seconds
        ):
            self._pending = False
            self._last_capture_at = now
            return True
        return False


def validate_output_dir(path: Path) -> Path:
    """Create/resolve an output folder and verify actual write/delete access."""
    probe: Path | None = None
    try:
        output_dir = Path(path).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".deskguard-probe-", suffix=".tmp", dir=output_dir, delete=False
        ) as handle:
            probe = Path(handle.name)
            handle.write(b"deskguard-write-check")
            handle.flush()
        probe.unlink()
        probe = None
        return output_dir
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"无法使用照片保存目录：{path}。{exc}") from exc
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass


def save_jpeg(output_dir: Path, jpeg_bytes: bytes, reason: str) -> Path:
    """Atomically save one JPEG, followed by a best-effort metadata-only log.

    JPEG marker checks catch empty/truncated/non-JPEG encoder output; decoding
    and image quality checks belong to the camera layer. A failed log write does
    not invalidate an already saved photo or cause it to be taken again.
    """
    if not isinstance(jpeg_bytes, bytes) or len(jpeg_bytes) < 4:
        raise ValueError("摄像头未返回有效的 JPEG 照片。")
    if not (jpeg_bytes.startswith(b"\xff\xd8") and jpeg_bytes.endswith(b"\xff\xd9")):
        raise ValueError("摄像头返回的照片不是完整的 JPEG 数据。")
    if not isinstance(reason, str):
        raise ValueError("触发原因必须是文字。")

    captured_at = datetime.now().astimezone()
    output_dir = Path(output_dir)
    day_dir = output_dir / captured_at.strftime("%Y-%m-%d")
    temp_path: Path | None = None
    try:
        day_dir.mkdir(parents=True, exist_ok=True)
        # UUID names keep separate instances and captures in one millisecond
        # distinct. Exclusive temp creation also prevents accidental reuse.
        while True:
            filename = (
                captured_at.strftime("%H-%M-%S-")
                + f"{captured_at.microsecond // 1000:03d}-{uuid.uuid4().hex}.jpg"
            )
            photo_path = day_dir / filename
            if photo_path.exists():
                continue
            candidate = day_dir / ("." + filename + ".tmp")
            try:
                handle = candidate.open("xb")
            except FileExistsError:
                continue
            temp_path = candidate
            break
        with handle:
            handle.write(jpeg_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, photo_path)
        temp_path = None
    except OSError as exc:
        raise RuntimeError(f"照片保存失败：{day_dir}。{exc}") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    event = {
        "time": captured_at.isoformat(timespec="milliseconds"),
        "reason": reason,
        "file": photo_path.relative_to(output_dir).as_posix(),
    }
    try:
        with (output_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:
        _LOG.warning("照片已保存到 %s，但事件日志写入失败：%s", photo_path, exc)
    return photo_path


def directory_size_bytes(output_dir: Path) -> int:
    """Count only local JPEG files, without following directory/file symlinks."""
    total = 0

    def walk_error(exc: OSError) -> None:
        raise exc

    try:
        for folder, dirs, names in os.walk(output_dir, followlinks=False, onerror=walk_error):
            dirs[:] = [name for name in dirs if not (Path(folder) / name).is_symlink()]
            for name in names:
                file_path = Path(folder) / name
                if file_path.suffix.lower() not in _PHOTO_SUFFIXES or file_path.is_symlink():
                    continue
                try:
                    total += file_path.stat().st_size
                except FileNotFoundError:
                    # A user may move/remove a photo while the scan is running.
                    continue
    except OSError as exc:
        raise RuntimeError(f"无法统计照片目录大小：{output_dir}。{exc}") from exc
    return total


def check_storage(
    output_dir: Path,
    max_bytes: int,
    incoming_bytes: int,
    min_free_bytes: int = 200 * 1024 * 1024,
) -> None:
    """Raise before writing if the photo cap or free-space reserve would fail."""
    if max_bytes <= 0 or incoming_bytes < 0 or min_free_bytes < 0:
        raise ValueError("存储上限必须大于 0，照片大小和保留空间不能为负数。")
    used_bytes = directory_size_bytes(output_dir)
    if used_bytes + incoming_bytes > max_bytes:
        raise RuntimeError("照片已达到设置的存储上限。请更换保存目录或手动整理照片后重新启用。")
    try:
        free_bytes = shutil.disk_usage(output_dir).free
    except OSError as exc:
        raise RuntimeError(f"无法检查磁盘可用空间：{output_dir}。{exc}") from exc
    if free_bytes - incoming_bytes < min_free_bytes:
        raise RuntimeError("磁盘可用空间不足，已停止保存照片。请释放空间后重新启用。")
