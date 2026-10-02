#!/usr/bin/env python3
"""Convenience wrapper around PyInstaller for building the standalone app.

Usage:
    python build.py [--keep-build] [--installer]

This simply invokes ``pyinstaller build.spec`` after a couple of sanity
checks, so contributors don't need to remember PyInstaller's CLI flags.
The result is a folder, ``dist/VideoEditor/`` (``VideoEditor.exe`` plus its
``_internal/`` folder on Windows). ``--installer`` then compiles
``installer/VideoEditor.iss`` with Inno Setup 6 (Windows only) into
``dist/installer/VideoEditor-<version>-setup.exe``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def find_iscc() -> str | None:
    """Inno Setup's compiler: on PATH, or in its default install folders."""
    found = shutil.which("iscc")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        for folder in ("Inno Setup 6", os.path.join("Programs", "Inno Setup 6")):
            candidate = os.path.join(base, folder, "ISCC.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


def build_installer() -> int:
    iscc = find_iscc()
    if iscc is None:
        print("Inno Setup 6 (ISCC.exe) was not found. Install it from https://jrsoftware.org/isinfo.php")
        return 1
    sys.path.insert(0, ROOT)
    from core import __version__

    cmd = [iscc, f"/DAppVersion={__version__}", os.path.join("installer", "VideoEditor.iss")]
    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode == 0:
        print(f"\nInstaller: dist/installer/VideoEditor-{__version__}-setup.exe")
    return result.returncode


def main() -> int:
    if shutil.which("pyinstaller") is None:
        try:
            import PyInstaller  # noqa: F401
        except ImportError:
            print("PyInstaller is not installed. Install it with:")
            print("  pip install pyinstaller")
            return 1

    bin_dir = os.path.join(ROOT, "bin")
    if not os.path.isdir(bin_dir) or not os.listdir(bin_dir):
        print(
            "Note: no bundled FFmpeg binaries found in ./bin/. The built app "
            "will rely on FFmpeg being installed on the end user's PATH.\n"
            "To bundle FFmpeg instead, copy ffmpeg/ffprobe (or ffmpeg.exe/"
            "ffprobe.exe on Windows) into ./bin/ before building.\n"
        )

    cmd = [sys.executable, "-m", "PyInstaller", "build.spec", "--noconfirm"]
    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode
    # build/ is PyInstaller's intermediate work folder (~160 MB), not part of the app. Remove it
    # after a successful build -- a failed one keeps it for debugging -- unless --keep-build is
    # passed, which trades the disk space for a faster next rebuild.
    if "--keep-build" not in sys.argv[1:]:
        shutil.rmtree(os.path.join(ROOT, "build"), ignore_errors=True)
    exe_name = "VideoEditor.exe" if sys.platform.startswith("win") else "VideoEditor"
    print(f"\nBuild finished. Your app is at: dist/VideoEditor/{exe_name} (ship the whole folder)")
    if "--installer" in sys.argv[1:]:
        return build_installer()
    return 0


if __name__ == "__main__":
    sys.exit(main())
