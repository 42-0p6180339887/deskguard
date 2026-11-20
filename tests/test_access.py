import json
from pathlib import Path
from queue import Queue
import tempfile
import unittest
from unittest.mock import Mock, patch

from access_control import AccessControl, AccessError
import app
from test_app import fake_app


class PasswordStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'access.json'
        self.access = AccessControl(self.path)

    def test_persisted_salted_verifier_never_contains_password(self):
        self.access.set_password('goose-test-123')
        first = self.path.read_text()
        self.assertNotIn('goose-test-123', first)
        restored = AccessControl(self.path)
        self.assertTrue(restored.verify('goose-test-123'))
        self.assertFalse(restored.verify('wrong-password'))
        self.access.set_password('goose-test-123')
        self.assertNotEqual(first, self.path.read_text())

    def test_invalid_and_cancelled_setup_cannot_create_credentials(self):
        for password in ('short', ' ' * 10, 'x' * 129, None):
            with self.assertRaises(AccessError):
                self.access.set_password(password)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.access.configured)

    def test_corrupt_file_fails_closed(self):
        for value in ('{', 'null', '{}', '{"version":1,"iterations":600000,"algorithm":"pbkdf2-sha256","salt":3}'):
            self.path.write_text(value)
            locked = AccessControl(self.path)
            self.assertTrue(locked.error)
            with self.assertRaises(AccessError):
                locked.verify('goose-test-123')
            with self.assertRaises(AccessError):
                locked.set_password('replacement-123')
            self.assertEqual(self.path.read_text(), value)

    def test_five_failures_block_even_correct_password_until_timeout(self):
        self.access.set_password('goose-test-123')
        with patch('access_control.time.monotonic', return_value=100):
            for _ in range(5):
                self.assertFalse(self.access.verify('wrong-password'))
            self.assertEqual(self.access.wait_seconds(), 30)
            with self.assertRaises(AccessError):
                self.access.verify('goose-test-123')
        with patch('access_control.time.monotonic', return_value=131):
            self.assertTrue(self.access.verify('goose-test-123'))

    def test_failed_replacement_preserves_old_password_and_removes_temp(self):
        self.access.set_password('goose-test-123')
        before = self.path.read_bytes()
        with patch('access_control.os.replace', side_effect=OSError('read only')):
            with self.assertRaises(OSError):
                self.access.set_password('new-goose-test-123')
        self.assertEqual(before, self.path.read_bytes())
        self.assertTrue(self.access.verify('goose-test-123'))
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_deleting_file_does_not_reset_running_password(self):
        self.access.set_password('goose-test-123')
        self.path.unlink()
        self.assertTrue(self.access.configured)
        self.assertFalse(self.access.verify('replacement-123'))
        self.assertTrue(self.access.verify('goose-test-123'))


class ProtectedActionsTests(unittest.TestCase):
    def setUp(self):
        self.guard = fake_app()
        self.guard.access = Mock(configured=True, error=None)
        self.guard.access.wait_seconds.return_value = 0
        self.guard.show_window = Mock()
        self.guard.stop = Mock()
        self.guard.close = Mock()

    def test_cancel_or_incorrect_password_never_stops_or_exits(self):
        for answer in (None, False):
            for action in (self.guard.request_stop, self.guard.request_close, self.guard.toggle):
                self.guard.state = 'armed'
                self.guard.access.verify.return_value = False
                with patch.object(app, 'VerifyDialog', return_value=Mock(result=answer)), patch.object(app.messagebox, 'showerror'):
                    action()
                self.guard.stop.assert_not_called()
                self.guard.close.assert_not_called()
                self.assertEqual(self.guard.state, 'armed')
                self.assertFalse(self.guard.auth_dialog_active)

    def test_correct_password_allows_stop_and_exit(self):
        self.guard.state = 'armed'
        self.guard.access.verify.return_value = True
        with patch.object(app, 'VerifyDialog', return_value=Mock(result=True)):
            self.guard.request_stop()
            self.guard.request_close()
        self.guard.stop.assert_called_once_with()
        self.guard.close.assert_called_once_with()

    def test_all_photo_entries_require_authentication(self):
        self.guard.authorize = Mock(return_value=False)
        self.guard.latest = Path('private.jpg')
        with patch.object(app.os, 'startfile') as launch, patch.object(app.filedialog, 'askdirectory') as browse:
            self.guard.open_folder()
            self.guard.view_latest()
            self.guard.browse()
        launch.assert_not_called()
        browse.assert_not_called()
        self.assertEqual(self.guard.authorize.call_count, 3)

    def test_missing_setup_prevents_camera_and_input_start(self):
        self.guard.ensure_password.return_value = False
        with patch.object(app, 'InputMonitor') as monitor, patch.object(app.mp, 'Process') as process:
            self.guard.begin(test=False)
            self.guard.begin(test=True)
        monitor.assert_not_called()
        process.assert_not_called()
        self.assertEqual(self.guard.state, 'idle')

    def test_existing_password_change_requires_old_password(self):
        self.guard.authorize = Mock(return_value=False)
        with patch.object(app.simpledialog, 'askstring') as prompt:
            self.assertFalse(self.guard.configure_password())
        prompt.assert_not_called()
        self.guard.access.set_password.assert_not_called()

    def test_reentrant_stop_or_close_does_not_create_second_dialog(self):
        self.guard.state = 'armed'
        self.guard.auth_dialog_active = True
        with patch.object(app.simpledialog, 'askstring') as prompt:
            self.guard.request_stop()
            self.guard.request_close()
            self.guard.toggle()
        prompt.assert_not_called()
        self.guard.stop.assert_not_called()
        self.guard.close.assert_not_called()

    def test_password_prompt_cannot_hide_its_own_parent(self):
        self.guard.auth_dialog_active = True
        self.guard.hide_window()
        self.guard.toggle_window()
        self.guard.root.withdraw.assert_not_called()
        self.guard.root.iconify.assert_not_called()

    def test_hotkey_and_tray_password_actions_leave_monitor_tick_scheduled(self):
        self.guard.closing = False
        self.guard.control_events = Queue()
        self.guard.control_events.put('toggle')
        self.guard.control_events.put('quit')
        self.guard.tick()
        self.assertEqual(self.guard.root.after_idle.call_count, 2)
        self.guard.root.after.assert_called_once_with(100, self.guard.tick)
        self.guard.stop.assert_not_called()
        self.guard.close.assert_not_called()

    def test_missing_password_cannot_be_replaced_during_guard(self):
        self.guard.state = 'armed'
        self.guard.access.configured = False
        self.assertFalse(self.guard.authorize('停止守护'))
        self.guard.ensure_password.assert_not_called()

    def test_queued_stop_cannot_rearm_after_automatic_state_change(self):
        self.guard.state = 'stopping'
        self.guard.closing = False
        self.guard.control_events = Queue()
        self.guard.control_events.put('toggle')
        self.guard.arm = Mock()
        self.guard.tick()
        callback = self.guard.root.after_idle.call_args.args[0]
        self.assertEqual(callback, self.guard.request_stop)
        self.guard.state = 'idle'
        callback()
        self.guard.arm.assert_not_called()


class VisiblePasswordTests(unittest.TestCase):
    def test_accidental_double_digit_can_be_corrected_in_same_field(self):
        with tempfile.TemporaryDirectory() as folder:
            access = AccessControl(Path(folder) / 'test.json')
            access.set_password('goose3317')
            dialog = object.__new__(app.VerifyDialog)
            dialog.access, dialog.feedback, dialog.value = access, Mock(), Mock()
            dialog.value.get.return_value = ''
            self.assertFalse(dialog.validate())
            self.assertEqual(access.failures, 0)
            for value, valid in [('goose33317', False), ('goose3317', True)]:
                dialog.value.get.return_value = value
                self.assertEqual(dialog.validate(), valid)
            self.assertEqual(access.failures, 0)
