"""Offline lifecycle regressions; never construct UI, camera, hooks or hotkeys."""

import json
from queue import Queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import app


def fake_app():
    """Exercise real lifecycle methods without running GuardApp.__init__."""
    guard = app.GuardApp.__new__(app.GuardApp)
    guard.root = Mock()
    guard.state = "idle"
    guard.process = None
    guard.monitor = None
    guard.controls = None
    guard.awake = False
    guard.shot_pending = False
    guard.stats_label = Mock()
    guard.busy = Mock()
    guard.set_status = Mock()
    guard.log = Mock()
    guard.save_settings = Mock()
    guard.read_config = Mock(return_value={
        "camera": 0,
        "folder": Path("unused-photo-folder"),
        "max_bytes": 1024**3,
        "interval": 3,
        "delay": 15,
        "compatible": False,
        "awake": True,
    })
    return guard


class AppCleanupTests(unittest.TestCase):
    def test_process_start_failure_releases_input_and_power_requests(self):
        guard = fake_app()
        monitor = Mock(error=None)
        monitor.start.return_value = True
        process = Mock(pid=None)
        process.start.side_effect = OSError("simulated camera spawn failure")
        commands, events = Mock(), Mock()

        with patch.object(app, "InputMonitor", return_value=monitor), \
             patch.object(app, "is_interactive_desktop", return_value=True), \
             patch.object(app, "keep_awake", return_value=True) as power, \
             patch.object(app.mp, "Process", return_value=process), \
             patch.object(app.mp, "Queue", side_effect=[commands, events]), \
             patch.object(app.mp, "Event", return_value=Mock()), \
             patch.object(app.mp, "Value", return_value=Mock()):
            guard.begin(test=False)

        process.start.assert_called_once_with()
        process.close.assert_called_once_with()
        process.join.assert_not_called()
        monitor.stop.assert_called_once_with()
        self.assertEqual(power.call_args_list, [call(True), call(False)])
        for queue in (commands, events):
            queue.cancel_join_thread.assert_called_once_with()
            queue.close.assert_called_once_with()
        self.assertIsNone(guard.process)
        self.assertIsNone(guard.monitor)
        self.assertFalse(guard.awake)
        self.assertEqual(guard.state, "idle")
        self.assertEqual(guard.busy.call_args_list, [call(True), call(False)])
        self.assertIn("simulated camera spawn failure", guard.set_status.call_args.args[1])

        # A follow-up Stop must remain safe, without touching already closed IPC.
        self.assertEqual(guard.stop(), [])
        process.close.assert_called_once_with()
        commands.close.assert_called_once_with()
        events.close.assert_called_once_with()

    def test_power_release_error_restores_idle_and_can_be_retried(self):
        guard = fake_app()
        guard.state = "armed"
        guard.awake = True
        guard.monitor = monitor = Mock()
        with patch.object(app, "keep_awake", side_effect=OSError("simulated power failure")):
            errors = guard.stop()

        monitor.stop.assert_called_once_with()
        self.assertIsNone(guard.monitor)
        self.assertEqual(guard.state, "idle")
        self.assertTrue(guard.awake, "Preserve a failed power request for later cleanup")
        guard.busy.assert_called_once_with(False)
        self.assertEqual(len(errors), 1)
        self.assertIn("simulated power failure", errors[0])
        self.assertTrue(guard.set_status.call_args.kwargs["error"])
        guard.root.deiconify.assert_called_once_with()

        with patch.object(app, "keep_awake", return_value=True) as power:
            self.assertEqual(guard.stop(), [])
        power.assert_called_once_with(False)
        self.assertFalse(guard.awake)
        self.assertEqual(guard.state, "idle")

    def test_monitor_stop_exception_does_not_skip_power_cleanup(self):
        guard = fake_app()
        guard.state = "armed"
        guard.awake = True
        guard.monitor = Mock()
        guard.monitor.stop.side_effect = OSError("simulated monitor failure")
        with patch.object(app, "keep_awake", return_value=True) as power:
            errors = guard.stop()
        power.assert_called_once_with(False)
        self.assertFalse(guard.awake)
        self.assertEqual(guard.state, "idle")
        guard.busy.assert_called_once_with(False)
        self.assertEqual(len(errors), 1)
        self.assertIn("simulated monitor failure", errors[0])

    def test_lock_cleanup_failure_does_not_allow_camera_reconnection(self):
        guard = fake_app()
        guard.state = "armed"
        guard.testing = False
        guard.closing = False
        guard.control_events = Queue()
        guard.process = original_process = Mock()
        guard.stop_camera = Mock(return_value=["simulated process still running"])
        guard.start_camera = Mock()

        with patch.object(app, "is_interactive_desktop", return_value=False):
            guard.tick()
        self.assertEqual(guard.state, "stopping")
        self.assertIs(guard.process, original_process)

        # Unlocking must not replace the handle to a camera that failed to stop.
        with patch.object(app, "is_interactive_desktop", return_value=True):
            guard.tick()
        guard.start_camera.assert_not_called()
        self.assertIs(guard.process, original_process)


class AppSettingsTests(unittest.TestCase):
    def test_non_object_settings_are_ignored_without_overwriting_defaults(self):
        with tempfile.TemporaryDirectory(prefix="deskguard-settings-test-") as folder:
            for value in (None, [], ["camera"], 1, "camera", True):
                with self.subTest(value=value):
                    guard = app.GuardApp.__new__(app.GuardApp)
                    guard.settings_file = Path(folder) / "settings.json"
                    guard.settings_file.write_text(json.dumps(value), encoding="utf-8")
                    variables = {}
                    for name in ("folder", "camera", "interval", "delay", "limit", "prevent_sleep", "compatible"):
                        variables[name] = Mock()
                        setattr(guard, name, variables[name])
                    guard.load_settings()
                    for variable in variables.values():
                        variable.set.assert_not_called()


if __name__ == "__main__":
    unittest.main()
