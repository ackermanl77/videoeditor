#!/usr/bin/env python3
"""Convenience wrapper around PyInstaller for building the standalone app.

Usage:
    python build.py

This simply invokes ``pyinstaller build.spec`` after a couple of sanity
checks, so contributors don't need to remember PyInstaller's CLI flags.
The resulting executable is written to ``dist/VideoEditor`` (or
``dist/VideoEditor.exe`` on Windows).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


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
    if result.returncode == 0:
        exe_name = "VideoEditor.exe" if sys.platform.startswith("win") else "VideoEditor"
        print(f"\nBuild finished. Your standalone app is at: dist/{exe_name}")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
