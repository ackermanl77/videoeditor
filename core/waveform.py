"""Audio waveform peak extraction, for drawing amplitude previews on clips.

The approach: decode a media file's audio to low-sample-rate mono PCM with
FFmpeg, then downsample it to a small number of "peaks" per second (the
max absolute amplitude within each time bucket). The result is cached to a
tiny JSON file next to the thumbnail cache, keyed by a hash of the source
path + modification time, so re-importing the same file is instant.

Kept dependency-free (no numpy) — the stdlib ``array`` module is fast
enough for this at the sample rate we decode to.
"""

from __future__ import annotations

import array
import hashlib
import json
import os
import subprocess
from typing import List, Optional, Tuple

from core.ffmpeg_utils import find_binary, run_binary

# A low decode sample rate keeps the FFmpeg step fast and the PCM buffer
# small; it's far more resolution than a waveform preview needs.
DECODE_SAMPLE_RATE = 4000
PEAKS_PER_SECOND = 25
_SAMPLES_PER_PEAK = DECODE_SAMPLE_RATE // PEAKS_PER_SECOND


def _cache_path(path: str, cache_dir: str) -> str:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    key = hashlib.sha1(f"waveform:{path}:{mtime}".encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, f"{key}.waveform.json")


def waveform_timeout(duration: float) -> float:
    """Decode budget: 60 s plus 1 s per 20 s of audio (decoding to 4 kHz mono runs far faster)."""
    return 60.0 + max(0.0, duration or 0.0) / 20.0


def generate_waveform(path: str, cache_dir: str, duration: float = 0.0) -> Optional[str]:
    """Extract and cache peak amplitude data for ``path``'s audio track.

    Returns the cache file's path on success, or ``None`` if FFmpeg is
    unavailable, the file has no readable audio, or decoding fails. Safe
    to call for files that turn out to have no audio — it just returns
    ``None`` rather than raising, so callers don't need to pre-check.
    """
    os.makedirs(cache_dir, exist_ok=True)
    out_path = _cache_path(path, cache_dir)
    if os.path.exists(out_path):
        return out_path

    try:
        ffmpeg = find_binary("ffmpeg")
    except FileNotFoundError:
        return None

    args = [
        ffmpeg,
        "-v", "error",
        "-i", path,
        "-vn",
        "-ac", "1",
        "-ar", str(DECODE_SAMPLE_RATE),
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "pipe:1",
    ]
    try:
        result = run_binary(args, timeout=waveform_timeout(duration))
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not result.stdout:
        return None

    peaks = _pcm_bytes_to_peaks(result.stdout)
    if not peaks:
        return None

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"peaks_per_second": PEAKS_PER_SECOND, "peaks": peaks}, f)
    return out_path


def _pcm_bytes_to_peaks(pcm_bytes: bytes) -> List[float]:
    """Downsample raw little-endian 16-bit PCM to one peak per bucket."""
    usable_len = (len(pcm_bytes) // 2) * 2
    samples = array.array("h")
    samples.frombytes(pcm_bytes[:usable_len])

    peaks: List[float] = []
    n = len(samples)
    if n == 0:
        return peaks

    bucket = max(1, _SAMPLES_PER_PEAK)
    for start in range(0, n, bucket):
        chunk = samples[start : start + bucket]
        if not chunk:
            continue
        amplitude = max(abs(min(chunk)), abs(max(chunk))) / 32768.0
        peaks.append(round(min(1.0, amplitude), 4))
    return peaks


def load_waveform(cache_path: str) -> Optional[Tuple[List[float], float]]:
    """Load a previously generated cache file.

    Returns ``(peaks, peaks_per_second)``, or ``None`` if the file is
    missing or unreadable.
    """
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data["peaks"], float(data["peaks_per_second"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
