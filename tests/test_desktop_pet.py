"""Native pet lifecycle tests without opening windows or starting camera input."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from desktop_pet import DesktopPet


class DesktopPetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "assets").mkdir()
        (root / "_app").mkdir()
        for name in ("deskguard-pet.png", "deskguard-pet-step.png"):
            (root / "assets" / name).write_bytes(b"fixture")
        (root / "_app" / "PetHost.exe").write_bytes(b"fixture")
        self.pet = DesktopPet(None, root / "assets" / "deskguard-pet.png")

    def test_configuration_is_inert_until_explicitly_shown(self):
        with patch("desktop_pet.subprocess.Popen") as launch:
            self.pet.configure(size="small", speed="slow", gentle=True)
            launch.assert_not_called()
            self.assertFalse(self.pet.visible)

    def test_show_passes_preferences_and_close_releases_control_pipe(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("pet", 0.15), 0]
        self.pet.configure(size="medium", speed="brisk", gentle=True)
        with patch("desktop_pet.subprocess.Popen", return_value=process) as launch:
            self.pet.set_visible(True)
            self.pet.set_visible(True)
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[0][-3:], ["medium", "brisk", "gentle"])
            self.pet.close()
        process.stdin.close.assert_called_once()
        process.terminate.assert_not_called()
        self.assertFalse(self.pet.visible)

    def test_failed_start_closes_pipe_and_reports_failure(self):
        process = Mock()
        process.wait.return_value = 1
        with patch("desktop_pet.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "启动失败"):
                self.pet.set_visible(True)
        process.stdin.close.assert_called_once()
        self.assertFalse(self.pet.visible)


if __name__ == "__main__":
    unittest.main()
