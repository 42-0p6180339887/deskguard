import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from guard_core import (
    TriggerGate,
    check_storage,
    directory_size_bytes,
    save_jpeg,
    validate_output_dir,
)


JPEG = b"\xff\xd8test-encoded-image\xff\xd9"


class TriggerGateTests(unittest.TestCase):
    def test_first_changed_tick_is_immediate_and_idle_never_repeats(self):
        gate = TriggerGate(5)
        gate.reset(100, 10)
        self.assertFalse(gate.due(100, 11))
        self.assertTrue(gate.due(101, 11.1))
        for moment in (11.2, 16.1, 1000):
            self.assertFalse(gate.due(101, moment))

    def test_inputs_during_cooldown_coalesce_into_one_pending_capture(self):
        gate = TriggerGate(5)
        gate.reset(100, 10)
        self.assertTrue(gate.due(101, 11))
        self.assertFalse(gate.due(102, 12))
        self.assertFalse(gate.due(103, 14))
        self.assertFalse(gate.due(103, 15.999))
        self.assertTrue(gate.due(103, 16))
        self.assertFalse(gate.due(103, 21))
        self.assertTrue(gate.due(104, 22))

    def test_dword_wrap_and_backward_tick_still_count_as_input(self):
        gate = TriggerGate(1)
        gate.reset(0xFFFFFFFF, 10)
        self.assertTrue(gate.due(0, 10.1))
        self.assertTrue(gate.due(0xFFFFFF00, 11.2))

    def test_reset_clears_pending_and_starts_with_new_baseline(self):
        gate = TriggerGate(5)
        gate.reset(100, 10)
        self.assertTrue(gate.due(101, 11))
        self.assertFalse(gate.due(102, 12))
        gate.reset(200, 13)
        self.assertFalse(gate.due(200, 100))
        self.assertTrue(gate.due(201, 100.1))

    def test_invalid_interval_and_uninitialized_gate(self):
        for interval in (-1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                TriggerGate(interval)
        with self.assertRaises(RuntimeError):
            TriggerGate(1).due(100, 10)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.output_dir = Path(self.temp_dir.name) / "照片 记录"

    def test_validate_creates_unicode_directory_and_removes_its_probe(self):
        result = validate_output_dir(self.output_dir)
        self.assertEqual(result, self.output_dir.resolve())
        self.assertTrue(result.is_dir())
        self.assertEqual(list(result.iterdir()), [])

    def test_validate_rejects_file_in_place_of_directory(self):
        self.output_dir.write_text("keep me", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "保存目录"):
            validate_output_dir(self.output_dir)
        self.assertEqual(self.output_dir.read_text(encoding="utf-8"), "keep me")

    def test_unique_atomic_saves_have_metadata_only_and_no_temp_files(self):
        validate_output_dir(self.output_dir)
        first = save_jpeg(self.output_dir, JPEG, "输入活动")
        second = save_jpeg(self.output_dir, JPEG, "手动测试")
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), JPEG)
        self.assertEqual(second.read_bytes(), JPEG)
        self.assertRegex(first.parent.name, r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(first.name, r"^\d{2}-\d{2}-\d{2}-\d{3}-[a-f0-9]{32}\.jpg$")
        events = [json.loads(line) for line in (self.output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(events), 2)
        self.assertEqual(set(events[0]), {"time", "reason", "file"})
        self.assertEqual(self.output_dir / events[0]["file"], first)
        self.assertFalse(list(self.output_dir.rglob("*.tmp")))
        self.assertEqual(directory_size_bytes(self.output_dir), len(JPEG) * 2)

    def test_corrupt_data_is_rejected_before_any_folder_or_file_created(self):
        for data in (b"", b"plain text", b"\xff\xd8truncated", b"\xff\xd9"):
            with self.assertRaises(ValueError):
                save_jpeg(self.output_dir, data, "input")
        self.assertFalse(self.output_dir.exists())

    def test_failed_atomic_replace_leaves_no_photo_or_temp(self):
        with patch("guard_core.os.replace", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(RuntimeError, "照片保存失败"):
                save_jpeg(self.output_dir, JPEG, "input")
        self.assertFalse(list(self.output_dir.rglob("*.jpg")))
        self.assertFalse(list(self.output_dir.rglob("*.tmp")))
        self.assertFalse((self.output_dir / "events.jsonl").exists())

    def test_log_failure_keeps_and_returns_saved_photo(self):
        validate_output_dir(self.output_dir)
        (self.output_dir / "events.jsonl").mkdir()
        with self.assertLogs("guard_core", level="WARNING") as log:
            result = save_jpeg(self.output_dir, JPEG, "input")
        self.assertEqual(result.read_bytes(), JPEG)
        self.assertIn("照片已保存", log.output[0])

    def test_size_counts_only_jpeg_files(self):
        validate_output_dir(self.output_dir)
        (self.output_dir / "one.JPG").write_bytes(b"123")
        (self.output_dir / "two.jpeg").write_bytes(b"12345")
        (self.output_dir / "events.jsonl").write_bytes(b"ignored log")
        (self.output_dir / "partial.jpg.tmp").write_bytes(b"ignored partial")
        self.assertEqual(directory_size_bytes(self.output_dir), 8)

    def test_cap_checks_incoming_size_and_never_deletes_photos(self):
        validate_output_dir(self.output_dir)
        photo = save_jpeg(self.output_dir, JPEG, "input")
        space = type("Space", (), {"free": 1000})()
        with patch("guard_core.shutil.disk_usage", return_value=space):
            check_storage(self.output_dir, len(JPEG) + 10, 10, min_free_bytes=10)
            with self.assertRaisesRegex(RuntimeError, "存储上限"):
                check_storage(self.output_dir, len(JPEG) + 10, 11, min_free_bytes=10)
        self.assertEqual(photo.read_bytes(), JPEG)

    def test_free_space_reserves_incoming_photo_size(self):
        validate_output_dir(self.output_dir)
        space = type("Space", (), {"free": 100})()
        with patch("guard_core.shutil.disk_usage", return_value=space):
            check_storage(self.output_dir, 1000, 20, min_free_bytes=80)
            with self.assertRaisesRegex(RuntimeError, "磁盘可用空间不足"):
                check_storage(self.output_dir, 1000, 21, min_free_bytes=80)


if __name__ == "__main__":
    unittest.main()
