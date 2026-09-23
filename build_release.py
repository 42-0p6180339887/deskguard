"""Build a Windows x64 portable release using local Python 3.12 and Windows tools.

No packages are downloaded. The application and camera helper are never run.
Existing release folders/archives are refused rather than merged or overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile


APP_MODULES = (
    "app.py", "guard_core.py", "windows_input.py", "windows_tray.py", "camera_worker.py", "desktop_pet.py",
)
RUNTIME_FILES = (
    "python.exe", "pythonw.exe", "python3.dll", "python312.dll",
    "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt",
)
RUNTIME_TREES = ("Lib", "DLLs", "tcl")
DOCUMENTS = (
    "LICENSE", "THIRD_PARTY_NOTICES.md", "README.md", "CHANGELOG.md",
    "docs/USER_GUIDE.zh-CN.md", "docs/VALIDATION.zh-CN.md",
    "docs/DEVELOPMENT.md", "VERSION",
)
ASSETS = ("assets/deskguard.ico", "assets/deskguard.png", "assets/deskguard-pet.png",
          "assets/deskguard-pet-step.png")
_EXCLUDED_DIRS = {
    "site-packages", "__pycache__", "test", "tests", "idlelib", "ensurepip",
    "venv", "turtledemo", "photos", "logs", ".git",
}
_EXCLUDED_FILES = {"settings.json", "events.jsonl"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".jpg", ".jpeg"}


class BuildError(RuntimeError):
    """A release cannot be built without changing the supplied inputs."""


def _validate_host() -> None:
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise BuildError("Build on 64-bit Windows using 64-bit Python 3.12.")
    if sys.version_info[:2] != (3, 12):
        raise BuildError("This launcher configuration requires Python 3.12.")


def _regular_file(path: Path) -> None:
    if _is_link(path) or not path.is_file():
        raise BuildError(f"Required input is missing or is a symbolic link: {path}")


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _validate_python_root(python_root: Path) -> None:
    for name in RUNTIME_FILES:
        _regular_file(python_root / name)
    for name in RUNTIME_TREES:
        directory = python_root / name
        if _is_link(directory) or not directory.is_dir():
            raise BuildError(f"Python runtime directory is missing or linked: {directory}")
    _regular_file(python_root / "DLLs" / "_tkinter.pyd")
    for name in ("tcl8.6", "tk8.6"):
        if not (python_root / "tcl" / name).is_dir():
            raise BuildError("Python must include Tcl/Tk 8.6 (install the tkinter feature).")
    # Check the selected runtime without executing it or importing site packages.
    with (python_root / "python.exe").open("rb") as handle:
        header = handle.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise BuildError("Selected python.exe is not a Windows executable.")
        pe_offset = struct.unpack_from("<I", header, 60)[0]
        handle.seek(pe_offset)
        pe_header = handle.read(6)
    if pe_header != b"PE\0\0\x64\x86":
        raise BuildError("Selected Python runtime must be Windows x64 (AMD64).")


def _copy_file(source: Path, target: Path) -> None:
    _regular_file(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _runtime_relative_allowed(relative: Path) -> bool:
    if any(part.lower() in _EXCLUDED_DIRS for part in relative.parts):
        return False
    return relative.name.lower() not in _EXCLUDED_FILES and relative.suffix.lower() not in _EXCLUDED_SUFFIXES


def _copy_runtime(python_root: Path, package_dir: Path) -> list[Path]:
    manifest: list[Path] = []
    for name in RUNTIME_FILES:
        relative = Path("_runtime") / name
        _copy_file(python_root / name, package_dir / relative)
        manifest.append(relative)
    for tree in RUNTIME_TREES:
        root = python_root / tree
        for directory, subdirs, filenames in os.walk(root, followlinks=False):
            current = Path(directory)
            subdirs[:] = sorted(
                name for name in subdirs
                if name.lower() not in _EXCLUDED_DIRS and not _is_link(current / name)
            )
            for name in sorted(filenames):
                source = current / name
                runtime_relative = source.relative_to(python_root)
                if _is_link(source) or not _runtime_relative_allowed(runtime_relative):
                    continue
                relative = Path("_runtime") / runtime_relative
                _copy_file(source, package_dir / relative)
                manifest.append(relative)
    return manifest


def _compile_native(source_dir: Path, package_dir: Path) -> list[Path]:
    windows = Path(os.environ.get("WINDIR", os.environ.get("SystemRoot", r"C:\Windows")))
    framework = windows / "Microsoft.NET" / "Framework64" / "v4.0.30319"
    metadata = windows / "System32" / "WinMetadata"
    compiler = framework / "csc.exe"
    _regular_file(compiler)
    for name in ("CameraHost.cs", "Launcher.cs", "PetHost.cs"):
        _regular_file(source_dir / name)
    icon = source_dir / "assets" / "deskguard.ico"
    _regular_file(icon)
    _regular_file(source_dir / "VERSION")
    version = (source_dir / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or any(int(part) > 65534 for part in version.split(".")):
        raise BuildError("VERSION must contain a three-part numeric version (each part 0..65534).")
    camera_refs = ["System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll"]
    local_refs = [framework / name for name in (
        "System.Runtime.dll", "System.Threading.Tasks.dll", "System.Runtime.InteropServices.WindowsRuntime.dll",
    )]
    local_refs += [metadata / name for name in (
        "Windows.Foundation.winmd", "Windows.Devices.winmd", "Windows.Media.winmd", "Windows.Storage.winmd",
    )]
    for reference in local_refs:
        _regular_file(reference)
    camera_refs += [str(reference) for reference in local_refs]
    camera_path = Path("_app") / "CameraHost.exe"
    (package_dir / "_app").mkdir(parents=True, exist_ok=True)
    commands = [
        [str(compiler), "/nologo", "/target:exe", "/platform:x64", "/optimize+",
         "/out:" + str(package_dir / camera_path)]
        + ["/r:" + reference for reference in camera_refs] + [str(source_dir / "CameraHost.cs")],
        [str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/optimize+",
         "/r:System.Windows.Forms.dll", "/out:" + str(package_dir / "DeskGuard.exe"),
         "/win32icon:" + str(icon),
         str(source_dir / "Launcher.cs")],
        [str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/optimize+",
         "/r:System.Drawing.dll", "/r:System.Windows.Forms.dll",
         "/out:" + str(package_dir / "_app" / "PetHost.exe"),
         str(source_dir / "PetHost.cs")],
    ]
    # Derive executable metadata from the same VERSION shipped in the package.
    # Keep generated source outside the payload and remove it after compilation.
    with tempfile.TemporaryDirectory(prefix="deskguard-version-") as temporary:
        assembly_info = Path(temporary) / "AssemblyInfo.cs"
        assembly_info.write_text(
            f'[assembly: System.Reflection.AssemblyVersion("{version}.0")]\n'
            f'[assembly: System.Reflection.AssemblyFileVersion("{version}.0")]\n'
            f'[assembly: System.Reflection.AssemblyInformationalVersion("{version}")]\n'
            '[assembly: System.Reflection.AssemblyProduct("DeskGuard")]\n',
            encoding="utf-8",
        )
        for command in commands:
            try:
                subprocess.run(command + [str(assembly_info)], check=True, capture_output=True, text=True, errors="replace")
            except subprocess.CalledProcessError as exc:
                raise BuildError("Native compilation failed:\n" + exc.stdout + exc.stderr) from exc
    return [camera_path, Path("DeskGuard.exe"), Path("_app") / "PetHost.exe"]


def _payload_path_allowed(relative: Path) -> bool:
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        return False
    if relative.as_posix() in (*DOCUMENTS, *ASSETS) or relative == Path("DeskGuard.exe"):
        return True
    if relative.parts[0] == "_app":
        return len(relative.parts) == 2 and relative.name in (*APP_MODULES, "CameraHost.exe", "PetHost.exe")
    if relative.parts[0] == "_runtime":
        if len(relative.parts) < 2:
            return False
        runtime_relative = Path(*relative.parts[1:])
        if len(relative.parts) == 2:
            return relative.name in RUNTIME_FILES
        return relative.parts[1] in RUNTIME_TREES and _runtime_relative_allowed(runtime_relative)
    return False


def _create_archive(package_dir: Path, manifest: list[Path], archive_path: Path) -> None:
    """Archive only recorded build inputs, never a recursive scan of a used app."""
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for relative in sorted(set(manifest)):
            if not _payload_path_allowed(relative):
                raise BuildError(f"Refusing unexpected archive entry: {relative}")
            source = package_dir / relative
            _regular_file(source)
            archive.write(source, "DeskGuard/" + relative.as_posix())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_release(output_dir: Path, python_root: Path, source_dir: Path | None = None) -> dict[str, Path]:
    """Create a fresh release and return folder/archive/checksums paths."""
    _validate_host()
    source_dir = Path(source_dir or Path(__file__).resolve().parent).resolve()
    python_root = Path(python_root).resolve()
    output_dir = Path(output_dir).resolve()
    _validate_python_root(python_root)
    _regular_file(source_dir / "LICENSE")
    artifacts = {
        "folder": output_dir / "DeskGuard",
        "archive": output_dir / "DeskGuard-Windows.zip",
        "checksums": output_dir / "SHA256SUMS.txt",
    }
    for destination in artifacts.values():
        if destination.exists() or destination.is_symlink():
            raise BuildError(f"Output already exists; choose a fresh --output directory: {destination}")
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".deskguard-build-", dir=output_dir) as temporary:
        staging = Path(temporary)
        package_dir = staging / "DeskGuard"
        package_dir.mkdir()
        manifest = _copy_runtime(python_root, package_dir)
        for name in APP_MODULES:
            relative = Path("_app") / name
            _copy_file(source_dir / name, package_dir / relative)
            manifest.append(relative)
        for name in ASSETS:
            relative = Path(name)
            _copy_file(source_dir / relative, package_dir / relative)
            manifest.append(relative)
        manifest.extend(_compile_native(source_dir, package_dir))
        for name in DOCUMENTS:
            if (source_dir / name).exists():
                relative = Path(name)
                _copy_file(source_dir / relative, package_dir / relative)
                manifest.append(relative)
        archive_path = staging / artifacts["archive"].name
        _create_archive(package_dir, manifest, archive_path)
        checksums = staging / artifacts["checksums"].name
        checksums.write_text(_sha256(archive_path) + "  " + archive_path.name + "\n", encoding="ascii")
        # All copying/compilation succeeds before publishing. Never merge into an
        # existing installation, which might contain the user's photos/settings.
        for destination in artifacts.values():
            if destination.exists() or destination.is_symlink():
                raise BuildError(f"Output appeared during build; refusing to overwrite: {destination}")
        package_dir.rename(artifacts["folder"])
        archive_path.rename(artifacts["archive"])
        checksums.rename(artifacts["checksums"])
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "dist")
    parser.add_argument("--python-root", type=Path, default=Path(sys.base_prefix))
    args = parser.parse_args()
    try:
        artifacts = build_release(args.output, args.python_root)
    except (BuildError, OSError) as exc:
        parser.exit(1, f"Build failed: {exc}\n")
    for label, path in artifacts.items():
        print(f"{label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
