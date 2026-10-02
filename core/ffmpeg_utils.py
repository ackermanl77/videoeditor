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

#: Default timeout (seconds) for :func:`run`/:func:`run_binary`. ffprobe,
#: thumbnail and waveform calls used to have none, so a damaged file or a
#: stalled network share could hang an import (or the export-time probe)
#: forever. Callers with legitimately longer work pass their own ``timeout``.
DEFAULT_TIMEOUT_S = 60.0

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


@lru_cache(maxsize=None)
def nvenc_available() -> bool:
    """Probe whether the located ``ffmpeg`` binary can *actually* encode with NVENC.

    Two steps, both run once and cached:

    1. ``ffmpeg -hide_banner -encoders`` must list ``h264_nvenc`` (a cheap
       check that the encoder is compiled into this build at all).
    2. A tiny real test encode (0.1 s of a 256x256 ``nullsrc`` pattern into
       ``h264_nvenc``, discarded via ``-f null``) must succeed. Step 1 alone
       is not enough: the gyan.dev/BtbN builds ship ``h264_nvenc`` on every
       machine, including ones with no NVIDIA GPU, an AMD/Intel GPU, or a
       driver too old for this ffmpeg's NVENC SDK -- all of which list the
       encoder but fail the moment it's opened. Actually opening an encode
       session is the only reliable check.

    This still isn't a guarantee a later full-length encode will succeed
    (e.g. another process could grab the GPU's limited concurrent NVENC
    session count in between) -- callers that select NVENC should still be
    prepared to fall back to a CPU codec on a GPU-specific failure (see
    ``core.exporter.Exporter._export_single_pass``).

    Cached for the life of the process (``lru_cache``): this doesn't change
    while the app is running, and re-spawning ffmpeg just to re-ask the same
    question on every export isn't worth it. Never raises — any failure
    (ffmpeg not found, probe error, non-zero exit, timeout) is treated as
    "not available" so callers always have a safe CPU fallback path.
    """
    try:
        ffmpeg = find_binary("ffmpeg")
        result = run([ffmpeg, "-hide_banner", "-encoders"], timeout=30)
        output = (result.stdout or "") + (result.stderr or "")
        if "h264_nvenc" not in output:
            return False
        test_encode = run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.1",
                "-c:v", "h264_nvenc", "-f", "null", "-",
            ],
            timeout=30,
        )
        return test_encode.returncode == 0
    except Exception:
        return False


@lru_cache(maxsize=None)
def nvenc_encoder_available(encoder: str) -> bool:
    """Like :func:`nvenc_available`, for one specific NVENC encoder (``hevc_nvenc``, ``av1_nvenc``...).

    A GPU can support H.264 NVENC but not AV1 (anything older than Ada), so
    each encoder gets its own tiny real test encode. Cached; never raises.
    """
    try:
        ffmpeg = find_binary("ffmpeg")
        result = run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.1",
                "-c:v", encoder, "-f", "null", "-",
            ],
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


#: Test-encode pixel format/profile per encoder for :func:`encoder_supports_10bit`.
_TEN_BIT_PROBE_ARGS = {
    "hevc_nvenc": ["-pix_fmt", "p010le", "-profile:v", "main10"],
    "av1_nvenc": ["-pix_fmt", "p010le"],
    "h264_nvenc": ["-pix_fmt", "p010le", "-profile:v", "high10"],
    "libx265": ["-pix_fmt", "yuv420p10le", "-profile:v", "main10", "-x265-params", "log-level=error"],
    "libaom-av1": ["-pix_fmt", "yuv420p10le", "-cpu-used", "8"],
    "prores_ks": ["-pix_fmt", "yuv422p10le"],
    "dnxhd": ["-profile:v", "dnxhr_hqx", "-pix_fmt", "yuv422p10le"],
}


@lru_cache(maxsize=None)
def encoder_supports_10bit(encoder: str) -> bool:
    """Whether ``encoder`` really encodes 10-bit video on this machine (a tiny real test encode).

    NVENC 10-bit needs a Pascal-or-newer GPU for HEVC (and Ada for AV1); a
    system FFmpeg may lack 10-bit x265. The Export dialog only offers 10-bit
    and HDR for codecs that pass. Cached; never raises.
    """
    extra = _TEN_BIT_PROBE_ARGS.get(encoder)
    if extra is None:
        return False
    try:
        ffmpeg = find_binary("ffmpeg")
        result = run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=256x144:d=0.1",
             "-c:v", encoder, *extra, "-f", "null", "-"],
            timeout=60,
        )
        return result.returncode == 0
    except Exception:
        return False


@lru_cache(maxsize=None)
def binary_version(tool: str) -> str:
    """First line of ``<tool> -version`` (e.g. ``ffmpeg version 9.0.1-essentials_build...``), or ``""``."""
    try:
        result = run([find_binary(tool), "-hide_banner", "-version"], timeout=15)
    except Exception:
        return ""
    lines = (result.stdout or "").strip().splitlines()
    return lines[0] if lines else ""


def run(args: List[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess with sensible defaults (no console window on Windows).

    Output is decoded as UTF-8 (FFmpeg/FFprobe always emit UTF-8, e.g. a
    non-ASCII file name echoed back in ffprobe's JSON) with ``errors="replace"``
    -- plain ``text=True`` would use the locale code page (cp1252 on most
    Windows installs), which raised ``UnicodeDecodeError`` in subprocess's
    reader thread for a file like ``Árbol.mp4`` and left ``stdout`` empty.
    """
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT_S)  # raises subprocess.TimeoutExpired
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT_S)
    return subprocess.run(
        args,
        capture_output=True,
        creationflags=creationflags,
        **kwargs,
    )


def popen(args: List[str], **kwargs) -> subprocess.Popen:
    """Start a long-running subprocess with stderr merged into a line-buffered
    UTF-8 text stdout (see :func:`run` for why the encoding is explicit)."""
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        universal_newlines=True,
        creationflags=creationflags,
        **kwargs,
    )
