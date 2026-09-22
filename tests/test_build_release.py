import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import build_release as builder


class ReleaseBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / "source"
        self.python = root / "python"
        self.output = root / "release"
        self.source.mkdir()
        self.python.mkdir()
        for name in builder.APP_MODULES:
            (self.source / name).write_text("# application source\n", encoding="utf-8")
        for name in builder.DOCUMENTS:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("License or documentation content", encoding="utf-8")
        for name in builder.ASSETS:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"public app icon")
        for name in builder.RUNTIME_FILES:
            (self.python / name).write_bytes(b"runtime file")
        pe = bytearray(134)
        pe[:2] = b"MZ"
        struct.pack_into("<I", pe, 60, 128)
        pe[128:134] = b"PE\0\0\x64\x86"
        (self.python / "python.exe").write_bytes(pe)
        for name in builder.RUNTIME_TREES:
            (self.python / name).mkdir()
        (self.python / "DLLs" / "_tkinter.pyd").write_bytes(b"tkinter")
        for name in ("tcl8.6", "tk8.6"):
            (self.python / "tcl" / name).mkdir()
            (self.python / "tcl" / name / "license.terms").write_text("Keep these upstream terms", encoding="utf-8")
        (self.python / "Lib" / "os.py").write_text("# stdlib", encoding="utf-8")

    @staticmethod
    def compile_stub(source, package):
        (package / "DeskGuard.exe").write_bytes(b"compiled launcher")
        (package / "_app" / "CameraHost.exe").write_bytes(b"compiled camera helper")
        return [Path("DeskGuard.exe"), Path("_app/CameraHost.exe")]

    def build(self):
        with patch.object(builder, "_validate_host"), patch.object(builder, "_compile_native", side_effect=self.compile_stub):
            return builder.build_release(self.output, self.python, self.source)

    def test_release_includes_licenses_and_only_clean_payload(self):
        # A used source tree and an installed Python may have unrelated/private data.
        (self.source / "Photos").mkdir()
        (self.source / "Photos" / "private.jpg").write_bytes(b"private")
        (self.source / "settings.json").write_text("private settings", encoding="utf-8")
        (self.source / "CameraHost.exe").write_bytes(b"stale untrusted binary")
        (self.source / "assets" / "private.png").write_bytes(b"private image")
        for relative in ("Lib/site-packages/extra.py", "Lib/__pycache__/os.pyc", "Lib/test/test_os.py", "Lib/Photos/private.jpg", "Lib/settings.json"):
            path = self.python / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"not for release")
        result = self.build()
        with zipfile.ZipFile(result["archive"]) as archive:
            names = set(archive.namelist())
            self.assertIn("DeskGuard/DeskGuard.exe", names)
            self.assertIn("DeskGuard/_app/app.py", names)
            self.assertIn("DeskGuard/_runtime/Lib/os.py", names)
            self.assertIn("DeskGuard/LICENSE", names)
            self.assertIn("DeskGuard/THIRD_PARTY_NOTICES.md", names)
            for name in builder.ASSETS:
                self.assertEqual(archive.read("DeskGuard/" + name), b"public app icon")
            self.assertIn("DeskGuard/_runtime/LICENSE.txt", names)
            self.assertIn("DeskGuard/_runtime/tcl/tcl8.6/license.terms", names)
            self.assertEqual(archive.read("DeskGuard/_app/CameraHost.exe"), b"compiled camera helper")
            self.assertFalse(any("private" in name or "site-packages" in name or "__pycache__" in name or "settings.json" in name for name in names))
        checksum = hashlib.sha256(result["archive"].read_bytes()).hexdigest()
        self.assertEqual(result["checksums"].read_text(), checksum + "  DeskGuard-Windows.zip\n")
        self.assertEqual((self.source / "CameraHost.exe").read_bytes(), b"stale untrusted binary")

    def test_existing_installation_is_never_merged_or_overwritten(self):
        photo = self.output / "DeskGuard" / "Photos" / "keep.jpg"
        photo.parent.mkdir(parents=True)
        photo.write_bytes(b"keep my photo")
        with self.assertRaisesRegex(builder.BuildError, "already exists"):
            self.build()
        self.assertEqual(photo.read_bytes(), b"keep my photo")
        self.assertFalse((self.output / "DeskGuard-Windows.zip").exists())

    def test_failed_compile_publishes_nothing_and_preserves_other_output_files(self):
        self.output.mkdir()
        sentinel = self.output / "notes.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with patch.object(builder, "_validate_host"), patch.object(builder, "_compile_native", side_effect=builder.BuildError("compile failed")):
            with self.assertRaisesRegex(builder.BuildError, "compile failed"):
                builder.build_release(self.output, self.python, self.source)
        self.assertEqual(list(self.output.iterdir()), [sentinel])

    def test_archive_uses_manifest_and_rejects_private_paths(self):
        package = self.output / "DeskGuard"
        (package / "Photos").mkdir(parents=True)
        (package / "Photos" / "private.jpg").write_bytes(b"private")
        (package / "DeskGuard.exe").write_bytes(b"launcher")
        archive = self.output / "clean.zip"
        builder._create_archive(package, [Path("DeskGuard.exe")], archive)
        with zipfile.ZipFile(archive) as zipped:
            self.assertEqual(zipped.namelist(), ["DeskGuard/DeskGuard.exe"])
        with self.assertRaisesRegex(builder.BuildError, "unexpected archive entry"):
            builder._create_archive(package, [Path("Photos/private.jpg")], self.output / "refused.zip")
        with self.assertRaisesRegex(builder.BuildError, "unexpected archive entry"):
            builder._create_archive(package, [Path("assets/private.png")], self.output / "refused-asset.zip")


if __name__ == "__main__":
    unittest.main()
