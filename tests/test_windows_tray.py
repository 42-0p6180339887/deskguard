"""Offline tray recovery tests; all native calls and worker threads are mocked."""
import os
from pathlib import Path
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


@unittest.skipUnless(os.name == "nt", "Native structure layout is Windows-only")
class TrayIconLifetimeTests(unittest.TestCase):
    def controls(self, loaded_icon=789, add_succeeds=True):
        controls = DesktopControls(Mock(), icon_path=Path("assets") / "deskguard.ico")
        controls._setup_apis = Mock()
        controls._user32 = Mock()
        controls._kernel32 = Mock()
        controls._shell32 = Mock()
        controls._kernel32.GetModuleHandleW.return_value = 1
        controls._user32.RegisterClassW.return_value = 1
        controls._user32.RegisterWindowMessageW.return_value = 0xC123
        controls._user32.CreateWindowExW.return_value = 123
        controls._user32.GetSystemMetrics.return_value = 16
        controls._user32.LoadImageW.return_value = loaded_icon
        controls._user32.LoadIconW.return_value = 456
        controls._user32.GetMessageW.side_effect = [1, 1, 0]
        controls._user32.DispatchMessageW.side_effect = lambda message: controls._update_icon()
        events = []

        def notify(operation, data):
            events.append(("notify", operation, data._obj.hIcon))
            return add_succeeds

        controls._shell32.Shell_NotifyIconW.side_effect = notify
        controls._user32.DestroyIcon.side_effect = lambda icon: events.append(("destroy", icon)) or True
        return controls, events

    def test_custom_icon_is_reused_and_freed_after_tray_removal(self):
        controls, events = self.controls()
        controls._run()
        controls._user32.LoadImageW.assert_called_once_with(
            None, str(Path("assets") / "deskguard.ico"), 1, 16, 16, 0x10
        )
        controls._user32.LoadIconW.assert_not_called()
        self.assertEqual(events, [
            ("notify", 0, 789), ("notify", 1, 789), ("notify", 1, 789),
            ("notify", 2, None), ("destroy", 789),
        ])
        controls._release_custom_icon()
        controls._user32.DestroyIcon.assert_called_once_with(789)
        self.assertIsNone(controls._custom_icon)

    def test_missing_or_invalid_artwork_keeps_stock_icon_without_destroying_it(self):
        controls, events = self.controls(loaded_icon=None)
        controls._run()
        controls._user32.LoadImageW.assert_called_once()
        self.assertEqual(events, [
            ("notify", 0, 456), ("notify", 1, 456), ("notify", 1, 456),
            ("notify", 2, None),
        ])
        controls._user32.DestroyIcon.assert_not_called()
        self.assertEqual(controls.error, "")

    def test_failed_tray_creation_still_releases_loaded_icon(self):
        controls, events = self.controls(add_succeeds=False)
        controls._run()
        self.assertEqual(events, [("notify", 0, 789), ("destroy", 789)])
        controls._user32.DestroyIcon.assert_called_once_with(789)
        self.assertFalse(controls.running)
        self.assertIn("无法创建", controls.error)


if __name__ == "__main__":
    unittest.main()
