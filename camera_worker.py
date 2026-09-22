"""Local Windows camera child; a kill-on-close job prevents orphaned cameras."""
import base64
import ctypes
from pathlib import Path
from queue import Empty, Queue
import subprocess
import threading
import time
from guard_core import check_storage, save_jpeg

HOST_PATH = Path(__file__).with_name("CameraHost.exe")

class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]

class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IOCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

class KillOnCloseJob:
    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self.api.CreateJobObjectW.restype = ctypes.c_void_p
        self.api.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        self.api.SetInformationJobObject.restype = ctypes.c_int
        self.api.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.api.AssignProcessToJobObject.restype = ctypes.c_int
        self.api.CloseHandle.argtypes = [ctypes.c_void_p]
        self.api.CloseHandle.restype = ctypes.c_int
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error
    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

def decode_photo(line):
    if not line.startswith("PHOTO "):
        raise ValueError("摄像头返回了无法识别的照片数据。")
    photo = base64.b64decode(line[6:].strip(), validate=True)
    if not photo.startswith(b"\xff\xd8") or not photo.endswith(b"\xff\xd9"):
        raise ValueError("摄像头返回的 JPEG 照片不完整。")
    return photo

def _read_lines(stream, lines):
    try:
        for line in stream:
            lines.put(line.strip())
    finally:
        lines.put(None)

def run_camera(index, output_dir, max_bytes, commands, events, stop, heartbeat):
    process = None
    job = None
    try:
        if stop.is_set():
            return
        job = KillOnCloseJob()
        process = subprocess.Popen([str(HOST_PATH), str(index)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        # OPEN is sent only after the kill-on-close job owns the camera child.
        job.assign(process)
        lines = Queue()
        threading.Thread(target=_read_lines, args=(process.stdout, lines), daemon=True).start()
        process.stdin.write("OPEN\n")
        process.stdin.flush()
        ready = False
        pending = None
        while not stop.is_set():
            if ready and pending is None:
                heartbeat.value = time.monotonic()
                try:
                    pending = str(commands.get_nowait())
                    process.stdin.write("SNAP\n")
                    process.stdin.flush()
                except Empty:
                    pass
            try:
                line = lines.get(timeout=0.1)
            except Empty:
                continue
            if line is None:
                raise RuntimeError("摄像头组件意外退出，请检查权限或设备后重试。")
            if line.startswith("READY "):
                parts = line.split()
                ready = True
                heartbeat.value = time.monotonic()
                events.put(("ready", int(parts[1]), int(parts[2])))
            elif line.startswith("PHOTO "):
                if pending is None:
                    raise RuntimeError("摄像头返回了未请求的照片，已停止。")
                photo = decode_photo(line)
                if stop.is_set():
                    return
                check_storage(Path(output_dir), max_bytes, len(photo))
                path = save_jpeg(Path(output_dir), photo, pending)
                events.put(("saved", str(path), len(photo), pending))
                pending = None
                heartbeat.value = time.monotonic()
            elif line.startswith("ERR "):
                raise RuntimeError(line[4:])
    except Exception as exc:
        events.put(("error", str(exc)))
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    process.stdin.write("STOP\n")
                    process.stdin.flush()
                    process.wait(timeout=0.3)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass
        if job is not None:
            job.close()
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
            for stream in (process.stdin, process.stdout):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass
