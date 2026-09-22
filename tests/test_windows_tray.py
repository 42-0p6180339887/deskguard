"""Offline tray recovery tests; all native calls and worker threads are mocked."""
import unittest
from unittest.mock import Mock, call

from windows_tray import DesktopControls


class TrayRecoveryTests(unittest.TestCase):
    def controls(self, results):
        controls = DesktopControls(Mock())
        controls._thread = Mock()
        controls._thread.is_alive.return_value = True
        controls._started = True
        controls.hotkeys_available = True
        controls._user32 = Mock()
        controls._hwnd = 123
        controls._taskbar_created = 0xC123
        controls._update_icon = Mock(side_effect=results)
        return controls

    def assert_running(self, controls):
        self.assertTrue(controls.running)
        self.assertTrue(controls.hotkeys_available)
        self.assertFalse(controls._stop_requested.is_set())
        self.assertEqual(controls.error, "")
        controls._user32.PostQuitMessage.assert_not_called()
        controls._user32.DefWindowProcW.assert_not_called()

    def test_successful_status_update_keeps_controls_running(self):
        controls = self.controls([True])
        self.assertEqual(controls._window_proc(123, controls._WM_STATUS, 0, 0), 0)
        controls._update_icon.assert_called_once_with(add=False)
        self.assert_running(controls)

    def test_missing_icon_during_status_update_is_readded(self):
        controls = self.controls([False, True])
        self.assertEqual(controls._window_proc(123, controls._WM_STATUS, 0, 0), 0)
        self.assertEqual(controls._update_icon.call_args_list, [call(add=False), call(add=True)])
        self.assert_running(controls)

    def test_explorer_restart_readds_icon(self):
        controls = self.controls([True])
        self.assertEqual(controls._window_proc(123, controls._taskbar_created, 0, 0), 0)
        controls._update_icon.assert_called_once_with(add=True)
        self.assert_running(controls)

    def test_existing_icon_on_restart_is_updated_without_false_failure(self):
        controls = self.controls([False, True])
        self.assertEqual(controls._window_proc(123, controls._taskbar_created, 0, 0), 0)
        self.assertEqual(controls._update_icon.call_args_list, [call(add=True), call(add=False)])
        self.assert_running(controls)

    def test_failed_recovery_marks_controls_unavailable_for_ui_fallback(self):
        for initial_add in (False, True):
            with self.subTest(initial_add=initial_add):
                controls = self.controls([False, False])
                message = controls._taskbar_created if initial_add else controls._WM_STATUS
                self.assertEqual(controls._window_proc(123, message, 0, 0), 0)
                self.assertEqual(controls._update_icon.call_args_list,
                                 [call(add=initial_add), call(add=not initial_add)])
                self.assertFalse(controls.running)
                self.assertFalse(controls.hotkeys_available)
                self.assertTrue(controls._stop_requested.is_set())
                self.assertIn("主窗口", controls.error)
                controls._user32.PostQuitMessage.assert_called_once_with(0)
                controls._user32.DefWindowProcW.assert_not_called()

    def test_icon_update_exception_also_enables_ui_fallback(self):
        controls = self.controls(OSError("simulated shell failure"))
        self.assertEqual(controls._window_proc(123, controls._WM_STATUS, 0, 0), 0)
        self.assertFalse(controls.running)
        self.assertTrue(controls._stop_requested.is_set())
        self.assertIn("simulated shell failure", controls.error)
        controls._user32.PostQuitMessage.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
