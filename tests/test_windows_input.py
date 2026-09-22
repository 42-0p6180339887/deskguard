"""Offline policy/ABI tests. Never install hooks or query live input/camera."""

import ctypes
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import windows_input as wi


class FilterTests(unittest.TestCase):
    def test_physical_flags_are_accepted(self):
        for keyboard, flags in ((True, 0), (True, 0x80), (True, 0x21), (False, 0)):
            self.assertTrue(wi.should_count_input(flags, keyboard=keyboard, ignore_injected=True))

    def test_injected_flags_are_excluded(self):
        for keyboard, flags in ((True, 0x10), (True, 0x12), (True, 0x90), (False, 1), (False, 3)):
            self.assertFalse(wi.should_count_input(flags, keyboard=keyboard, ignore_injected=True))

    def test_unfiltered_mode_accepts_injection(self):
        self.assertTrue(wi.should_count_input(0x12, keyboard=True, ignore_injected=False))
        self.assertTrue(wi.should_count_input(3, keyboard=False, ignore_injected=False))

    def test_device_specific_masks_do_not_overlap(self):
        self.assertTrue(wi.should_count_input(1, keyboard=True, ignore_injected=True))
        self.assertTrue(wi.should_count_input(0x10, keyboard=False, ignore_injected=True))


class ABITests(unittest.TestCase):
    def test_callback_parameters_are_pointer_sized(self):
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(wi.LRESULT), pointer_size)
        self.assertEqual(ctypes.sizeof(wi.LPARAM), pointer_size)
        self.assertEqual(ctypes.sizeof(wi.WPARAM), pointer_size)
        self.assertIs(wi.HOOKPROC._restype_, wi.LRESULT)
        self.assertEqual(wi.HOOKPROC._argtypes_, (ctypes.c_int, wi.WPARAM, wi.LPARAM))
        self.assertEqual(wi.LPARAM(-1).value, -1)

    def test_native_structure_layouts(self):
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(wi.DWORD), 4)
        self.assertEqual(ctypes.sizeof(wi.POINT), 8)
        self.assertEqual(wi.KBDLLHOOKSTRUCT.flags.offset, 8)
        self.assertEqual(wi.KBDLLHOOKSTRUCT.time.offset, 12)
        self.assertEqual(wi.KBDLLHOOKSTRUCT.dwExtraInfo.offset, 16)
        self.assertEqual(ctypes.sizeof(wi.KBDLLHOOKSTRUCT), 24 if pointer_size == 8 else 20)
        self.assertEqual(wi.MSLLHOOKSTRUCT.flags.offset, 12)
        self.assertEqual(wi.MSLLHOOKSTRUCT.time.offset, 16)
        self.assertEqual(wi.MSLLHOOKSTRUCT.dwExtraInfo.offset, 24 if pointer_size == 8 else 20)
        self.assertEqual(ctypes.sizeof(wi.MSLLHOOKSTRUCT), 32 if pointer_size == 8 else 24)
        self.assertEqual(ctypes.sizeof(wi.MSG), 48 if pointer_size == 8 else 32)
        self.assertEqual(ctypes.sizeof(wi.LASTINPUTINFO), 8)
        self.assertEqual(wi.LASTINPUTINFO.dwTime.offset, 4)

    def test_constructing_monitor_does_not_start_monitoring(self):
        monitor = wi.InputMonitor()
        self.assertTrue(monitor.ignore_injected)
        self.assertEqual(monitor.snapshot(), 0)
        self.assertIsNone(monitor.error)
        self.assertIsNone(monitor._thread)
        self.assertIsNone(monitor._callbacks)


if __name__ == "__main__":
    unittest.main()
