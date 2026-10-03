"""Audio waveform extraction for clip waveforms and preview level meters.

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
import math
import operator
import os
import subprocess
from typing import List, Optional, Tuple

from core.ffmpeg_utils import find_binary, run_binary

# A low decode sample rate keeps the FFmpeg step fast and the PCM buffer
# small; it's far more resolution than a waveform preview needs.
DECODE_SAMPLE_RATE = 4000
PEAKS_PER_SECOND = 100
_SAMPLES_PER_PEAK = DECODE_SAMPLE_RATE // PEAKS_PER_SECOND

# (peak amplitudes 0..1, RMS amplitudes 0..1, density per second).
WaveformData = Tuple[List[float], List[float], float]


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
    # Audio only (-vn): GPU decoding has nothing to do here, so this never
    # goes through the hardware-decode attempts the video decoders use.
    try:
        result = run_binary(args, timeout=waveform_timeout(duration))
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not result.stdout:
        return None

    peaks, rms = _pcm_bytes_to_levels(result.stdout)
    if not peaks:
        return None

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"peaks_per_second": PEAKS_PER_SECOND, "peaks": peaks, "rms": rms}, f)
    return out_path


def _pcm_bytes_to_peaks(pcm_bytes: bytes) -> List[float]:
    """Backwards-compatible peak-only view of :func:`_pcm_bytes_to_levels`."""
    return _pcm_bytes_to_levels(pcm_bytes)[0]


def _pcm_bytes_to_levels(pcm_bytes: bytes) -> Tuple[List[float], List[float]]:
    """Downsample PCM to one peak and RMS value per bucket."""
    usable_len = (len(pcm_bytes) // 2) * 2
    samples = array.array("h")
    samples.frombytes(pcm_bytes[:usable_len])

    peaks: List[float] = []
    rms_values: List[float] = []
    n = len(samples)
    if n == 0:
        return peaks, rms_values

    bucket = max(1, _SAMPLES_PER_PEAK)
    for start in range(0, n, bucket):
        chunk = samples[start : start + bucket]
        if not chunk:
            continue
        amplitude = max(abs(min(chunk)), abs(max(chunk))) / 32768.0
        peaks.append(round(min(1.0, amplitude), 4))
        # Integer sum of squares (map/mul runs in C; a per-sample float
        # generator made long files noticeably slower to import).
        power = sum(map(operator.mul, chunk, chunk)) / (len(chunk) * 1073741824.0)  # 32768 ** 2
        rms_values.append(round(min(1.0, math.sqrt(power)), 4))
    return peaks, rms_values


def load_waveform(cache_path: str) -> Optional[WaveformData]:
    """Load a previously generated cache file.

    Returns ``(peaks, rms, peaks_per_second)`` (``rms`` falls back to the
    peaks for a cache written before RMS levels were stored), or ``None`` if the file is
    missing or unreadable.
    """
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        peaks = list(data["peaks"])
        rms = list(data.get("rms") or peaks)
        return peaks, rms, float(data["peaks_per_second"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def window_levels(data: WaveformData, source_time: float, window_s: float = 0.12) -> Tuple[float, float]:
    """Approximate ``(peak, rms)`` around ``source_time`` from cached waveform data."""
    peaks, rms_values, density = data
    if not peaks or density <= 0:
        return 0.0, 0.0
    half = max(1, int(window_s * density / 2.0))
    center = int(max(0.0, source_time) * density)
    start = max(0, center - half)
    end = min(len(peaks), center + half + 1)
    if start >= end:
        return 0.0, 0.0
    peak = max(peaks[start:end]) if peaks else 0.0
    rms = sum(rms_values[start:end]) / max(1, end - start)
    return float(peak), float(rms)
