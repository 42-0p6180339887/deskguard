import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import app
import update_launch


class UpdateLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="deskguard update ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.resources = self.root / "updates"
        self.resources.mkdir()
        for name in ("AppUpdates.ps1", "app-update.json"):
            (self.resources / name).write_text("test component", encoding="utf-8")

    def test_launch_passes_absolute_packaged_paths_as_separate_arguments(self):
        powershell = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        with patch.object(update_launch, "_system_powershell", return_value=powershell), \
             patch.object(update_launch.subprocess, "Popen") as process:
            update_launch.launch_updater(self.root)
        args, options = process.call_args
        command = args[0]
        self.assertEqual(command[0], str(powershell))
        self.assertEqual(command[command.index("-File") + 1], str((self.resources / "AppUpdates.ps1").resolve()))
        self.assertEqual(command[command.index("-Config") + 1], str((self.resources / "app-update.json").resolve()))
        self.assertEqual(options["cwd"], str(self.root.resolve()))
        self.assertFalse(options["shell"])

    def test_incomplete_package_never_falls_back_to_a_working_directory_script(self):
        (self.resources / "AppUpdates.ps1").unlink()
        (self.root / "AppUpdates.ps1").write_text("untrusted alternate", encoding="utf-8")
        with patch.object(update_launch.subprocess, "Popen") as process:
            with self.assertRaises(OSError):
                update_launch.launch_updater(self.root)
            process.assert_not_called()

    def test_symbolic_link_to_outside_script_is_rejected(self):
        external = self.root / "outside.ps1"
        external.write_text("outside", encoding="utf-8")
        script = self.resources / "AppUpdates.ps1"
        script.unlink()
        try:
            script.symlink_to(external)
        except OSError:
            self.skipTest("Creating a symbolic link is unavailable on this Windows account")
        with patch.object(update_launch.subprocess, "Popen") as process:
            with self.assertRaises(OSError):
                update_launch.launch_updater(self.root)
            process.assert_not_called()

    def test_active_protection_is_never_stopped_or_updated_implicitly(self):
        guard = app.GuardApp.__new__(app.GuardApp)
        guard.root = Mock()
        guard.state = "armed"
        guard.stop = Mock()
        with patch.object(app, "launch_updater") as launch, patch.object(app.messagebox, "showinfo") as notice:
            guard.check_updates()
        launch.assert_not_called()
        guard.stop.assert_not_called()
        notice.assert_called_once()

    def test_idle_gui_action_launches_packaged_updater(self):
        guard = app.GuardApp.__new__(app.GuardApp)
        guard.root = Mock()
        guard.state = "idle"
        with patch.object(app, "launch_updater") as launch:
            guard.check_updates()
        launch.assert_called_once_with(app.APP_DIR)

    def test_transition_metadata_matches_program_version(self):
        source = Path(__file__).resolve().parents[1]
        version = (source / "VERSION").read_text(encoding="utf-8").strip()
        config = json.loads((source / "updates" / "app-update.json").read_text(encoding="utf-8"))
        self.assertEqual(config["versionName"], version)
        major, minor, patch = map(int, version.split("."))
        self.assertEqual(config["versionCode"], major * 10000 + minor * 100 + patch)
        self.assertEqual(config["appId"], "deskguard")


if __name__ == "__main__":
    unittest.main()
