"""Offline camera bridge tests: fake native process/job, no camera access."""
import base64
import json
from pathlib import Path
from queue import Queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import camera_worker


class FakeInput:
    def __init__(self):
        self.writes = []
        self.closed = False
    def write(self, text):
        self.writes.append(text)
    def flush(self):
        pass
    def close(self):
        self.closed = True


class FakeOutput:
    def __init__(self, lines):
        self.lines = lines
        self.closed = False
    def __iter__(self):
        return iter(self.lines)
    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, lines):
        self.stdin = FakeInput()
        self.stdout = FakeOutput(lines)
        self.returncode = None
        self.kill = Mock(side_effect=self._kill)
        self.wait = Mock(side_effect=self._wait)
    def poll(self):
        return self.returncode
    def _kill(self):
        self.returncode = -1
    def _wait(self, timeout):
        self.returncode = 0
        return 0


class InlineReaderThread:
    """Read supplied text synchronously; never create actual threads."""
    def __init__(self, target, args, daemon):
        self.target = target
        self.args = args
    def start(self):
        self.target(*self.args)


class CameraBridgeTests(unittest.TestCase):
    def test_stop_before_launch_never_creates_job_or_child(self):
        stop = Mock()
        stop.is_set.return_value = True
        with patch.object(camera_worker, "KillOnCloseJob") as job, \
             patch.object(camera_worker.subprocess, "Popen") as launch:
            camera_worker.run_camera(0, ".", 1024, Queue(), Queue(), stop, SimpleNamespace(value=0))
        job.assert_not_called()
        launch.assert_not_called()

    def test_missing_executable_reports_error_and_closes_job(self):
        job = Mock()
        stop = Mock()
        stop.is_set.return_value = False
        events = Queue()
        with patch.object(camera_worker, "KillOnCloseJob", return_value=job), \
             patch.object(camera_worker.subprocess, "Popen", side_effect=FileNotFoundError("missing CameraHost.exe")):
            camera_worker.run_camera(0, ".", 1024, Queue(), events, stop, SimpleNamespace(value=0))
        kind, message = events.get_nowait()
        self.assertEqual(kind, "error")
        self.assertIn("missing CameraHost.exe", message)
        self.assertTrue(events.empty())
        job.assign.assert_not_called()
        job.close.assert_called_once_with()

    def run_bridge(self, lines, folder, commands=None, *, assign_error=None):
        process = FakeProcess(lines)
        job = Mock()
        job.assign.side_effect = assign_error
        stopped = SimpleNamespace(value=False)
        stop = SimpleNamespace(is_set=lambda: stopped.value)
        received = []
        class Events:
            def put(self, event):
                received.append(event)
                if event[0] in ("saved", "error"):
                    stopped.value = True
        heartbeat = SimpleNamespace(value=0)
        with patch.object(camera_worker, "KillOnCloseJob", return_value=job), \
             patch.object(camera_worker.subprocess, "Popen", return_value=process) as launch, \
             patch.object(camera_worker.threading, "Thread", InlineReaderThread), \
             patch.object(camera_worker, "check_storage") as storage:
            camera_worker.run_camera(2, folder, 1024**3, commands or Queue(), Events(), stop, heartbeat)
        return process, job, received, heartbeat, launch, storage

    def assert_cleaned_up(self, process, job):
        job.assign.assert_called_once_with(process)
        job.close.assert_called_once_with()
        self.assertIn("STOP\n", process.stdin.writes)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertIsNotNone(process.poll())

    def test_ready_snap_photo_saves_bytes_and_trigger_metadata(self):
        jpeg = b"\xff\xd8synthetic-jpeg-envelope\xff\xd9"
        photo_line = "PHOTO " + base64.b64encode(jpeg).decode("ascii")
        commands = Queue()
        commands.put("input_activity")
        with tempfile.TemporaryDirectory(prefix="deskguard-camera-bridge-") as folder:
            process, job, events, heartbeat, launch, storage = self.run_bridge(
                ["READY 1280 720\n", photo_line + "\n"], folder, commands)
            self.assertEqual(events[0], ("ready", 1280, 720))
            self.assertEqual(events[1][0], "saved")
            saved = Path(events[1][1])
            self.assertEqual(saved.read_bytes(), jpeg)
            self.assertEqual(events[1][2:], (len(jpeg), "input_activity"))
            metadata = json.loads((Path(folder) / "events.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(metadata["reason"], "input_activity")
            self.assertEqual(metadata["file"], saved.relative_to(folder).as_posix())
            self.assertEqual(len(list(Path(folder).rglob("*.jpg"))), 1)
        self.assertEqual(process.stdin.writes, ["OPEN\n", "SNAP\n", "STOP\n"])
        self.assertGreater(heartbeat.value, 0)
        self.assertEqual(launch.call_args.args[0][1], "2")
        storage.assert_called_once_with(Path(folder), 1024**3, len(jpeg))
        self.assert_cleaned_up(process, job)

    def test_native_error_stops_and_cleans_up_child(self):
        process, job, events, _, _, storage = self.run_bridge(["ERR camera disconnected\n"], ".")
        self.assertEqual(events, [("error", "camera disconnected")])
        self.assertEqual(process.stdin.writes, ["OPEN\n", "STOP\n"])
        storage.assert_not_called()
        self.assert_cleaned_up(process, job)

    def test_failed_job_assignment_never_sends_open(self):
        process, job, events, _, _, _ = self.run_bridge([], ".", assign_error=OSError("job denied"))
        self.assertNotIn("OPEN\n", process.stdin.writes)
        self.assertEqual(events, [("error", "job denied")])
        self.assert_cleaned_up(process, job)

    def test_corrupt_base64_or_jpeg_is_rejected_without_saving(self):
        lines = ["PHOTO !!!not-base64!!!", "PHOTO " + base64.b64encode(b"not a JPEG").decode("ascii")]
        for line in lines:
            with self.subTest(line=line), tempfile.TemporaryDirectory() as folder:
                commands = Queue()
                commands.put("manual_test")
                process, job, events, _, _, storage = self.run_bridge(["READY 64 48", line], folder, commands)
                self.assertEqual([event[0] for event in events], ["ready", "error"])
                self.assertEqual(list(Path(folder).rglob("*.jpg")), [])
                self.assertFalse((Path(folder) / "events.jsonl").exists())
                storage.assert_not_called()
                self.assert_cleaned_up(process, job)

    def test_unrequested_photo_is_rejected(self):
        line = "PHOTO " + base64.b64encode(b"\xff\xd8fake\xff\xd9").decode("ascii")
        process, job, events, _, _, storage = self.run_bridge([line], ".")
        self.assertEqual(events[0][0], "error")
        self.assertNotIn("SNAP\n", process.stdin.writes)
        storage.assert_not_called()
        self.assert_cleaned_up(process, job)


if __name__ == "__main__":
    unittest.main()
