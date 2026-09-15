"""Low-level helpers for locating and invoking FFmpeg / FFprobe.

Centralising this here means the rest of the codebase never has to worry
about how the binaries are found (bundled next to the executable, on the
system PATH, or via imageio-ffmpeg as a fallback).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from typing import List, Optional

# When PyInstaller bundles the app, sys._MEIPASS points at the extracted
# runtime directory. We look there first so a bundled ffmpeg/ffprobe binary
# (placed next to the executable at build time) is preferred over anything
# on the system PATH, guaranteeing a consistent version at runtime.
def _bundle_dir() -> Optional[str]:
    if hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def _candidate_names(tool: str) -> List[str]:
    if sys.platform.startswith("win"):
        return [f"{tool}.exe"]
    return [tool]


@lru_cache(maxsize=None)
def find_binary(tool: str) -> str:
    """Locate the ``ffmpeg`` or ``ffprobe`` executable.

    Search order:
      1. Next to the running executable / bundle (``bin/`` subfolder or root).
      2. The system ``PATH``.
      3. The ``imageio-ffmpeg`` package's bundled binary (ffmpeg only).

    Raises ``FileNotFoundError`` if nothing is found.
    """
    bundle = _bundle_dir()
    search_dirs = []
    if bundle:
        search_dirs.extend([bundle, os.path.join(bundle, "bin")])

    for directory in search_dirs:
        for name in _candidate_names(tool):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

    on_path = shutil.which(tool)
    if on_path:
        return on_path

    if tool == "ffmpeg":
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass

    raise FileNotFoundError(
        f"Could not locate the '{tool}' executable. Install FFmpeg and ensure "
        f"it is on your PATH, or place '{tool}' next to the application."
    )


def run(args: List[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess with sensible defaults (no console window on Windows)."""
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        creationflags=creationflags,
        **kwargs,
    )


def run_binary(args: List[str], **kwargs) -> subprocess.CompletedProcess:
    """Like :func:`run`, but returns raw ``bytes`` stdout/stderr (``text``
    is deliberately *not* set). Use this for commands whose output is
    binary data — e.g. raw PCM audio piped from ffmpeg — where decoding as
    text would corrupt or fail to decode the bytes.
    """
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    return subprocess.run(
        args,
        capture_output=True,
        creationflags=creationflags,
        **kwargs,
    )


def popen(args: List[str], **kwargs) -> subprocess.Popen:
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        creationflags=creationflags,
        **kwargs,
    )
