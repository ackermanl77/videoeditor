"""Live EBU R128 loudness for the preview (F15's last piece).

Each source file is measured once by FFmpeg's own ``ebur128`` filter (the
same K-weighting and gating the Export dialog's ``loudnorm`` measurement
uses), which logs the momentary (400 ms) and short-term (3 s) loudness every
100 ms. Those two series are cached as a small JSON file next to the
waveforms, keyed like them by path + modification time, so the preview's
meter only looks values up while playing -- no decoding on the UI thread.

What the meter shows is the clip under the playhead (like the peak/RMS
meters): its source loudness shifted by the clip's gain in dB (volume and
volume automation), and an *integrated* value since playback started,
computed from the momentary blocks with R128's gates (absolute -70 LUFS,
relative -10 LU). A mix of several audible tracks isn't summed -- the
Export dialog's meter measures the real mix.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from typing import Dict, List, Optional, Tuple

from core.ffmpeg_utils import find_binary, run_binary

BLOCKS_PER_SECOND = 10  # ebur128's frame log interval (100 ms)
SILENCE_LUFS = -70.0  # R128's absolute gate; anything quieter shows as silence
RELATIVE_GATE_LU = 10.0

# (momentary LUFS, short-term LUFS, blocks per second); index i covers the
# window ending at (i + 1) / BLOCKS_PER_SECOND seconds of the source.
LoudnessData = Tuple[List[float], List[float], float]

_FRAME_LINE = re.compile(r"\bt:\s*([\d.]+)\s+TARGET:.*?\bM:\s*(-?[\d.]+|-?inf|nan)\s+S:\s*(-?[\d.]+|-?inf|nan)")


def loudness_cache_path(path: str, cache_dir: str) -> str:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    key = hashlib.sha1(f"loudness:{path}:{mtime}".encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, f"{key}.loudness.json")


def loudness_timeout(duration: float) -> float:
    """ebur128 decodes at the source's own sample rate: 60 s plus 1 s per 10 s of audio."""
    return 60.0 + max(0.0, duration or 0.0) / 10.0


def _value(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        return SILENCE_LUFS - 50
    return value if math.isfinite(value) else SILENCE_LUFS - 50


def parse_frame_log(log: str) -> Tuple[List[float], List[float]]:
    """Momentary and short-term series from ebur128's ``framelog=verbose`` lines, one per 100 ms."""
    momentary: List[float] = []
    short_term: List[float] = []
    for match in _FRAME_LINE.finditer(log):
        momentary.append(round(_value(match.group(2)), 2))
        short_term.append(round(_value(match.group(3)), 2))
    return momentary, short_term


def generate_loudness(path: str, cache_dir: str, duration: float = 0.0) -> Optional[str]:
    """Measure ``path``'s audio and cache it; the cache file's path, or None (no FFmpeg,
    no audio, decode error). Safe to call for files without audio."""
    os.makedirs(cache_dir, exist_ok=True)
    out_path = loudness_cache_path(path, cache_dir)
    if os.path.exists(out_path):
        return out_path
    try:
        ffmpeg = find_binary("ffmpeg")
    except FileNotFoundError:
        return None
    args = [
        ffmpeg, "-hide_banner", "-nostats", "-v", "verbose",
        "-i", path, "-vn", "-map", "0:a:0?",
        "-af", "ebur128=framelog=verbose", "-f", "null", "-",
    ]
    try:
        result = run_binary(args, timeout=loudness_timeout(duration))
    except Exception:  # noqa: BLE001 -- a missing meter isn't fatal (timeout, killed, ...)
        return None
    log = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else str(result.stderr)
    momentary, short_term = parse_frame_log(log)
    if result.returncode != 0 or not momentary:
        return None
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"momentary": momentary, "short_term": short_term, "blocks_per_second": BLOCKS_PER_SECOND}, f)
    os.replace(tmp, out_path)
    return out_path


def load_loudness(cache_path: str) -> Optional[LoudnessData]:
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        momentary = [float(v) for v in data["momentary"]]
        short_term = [float(v) for v in data["short_term"]]
        return momentary, short_term, float(data["blocks_per_second"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def gain_db(gain: float) -> float:
    return 20.0 * math.log10(gain) if gain > 0 else -math.inf


def loudness_at(data: LoudnessData, source_time: float, gain: float = 1.0) -> Tuple[Optional[float], Optional[float]]:
    """``(momentary, short_term)`` LUFS at ``source_time`` with the clip's ``gain``
    applied, None for silence (below the absolute gate) or past the end."""
    momentary, short_term, density = data
    index = int(max(0.0, source_time) * density + 1e-6) - 1  # the window ending at or before source_time
    index = max(0, index)
    if not momentary or index >= len(momentary):
        return None, None
    shift = gain_db(gain)

    def shifted(value: float) -> Optional[float]:
        level = value + shift
        return level if level > SILENCE_LUFS else None

    return shifted(momentary[index]), shifted(short_term[index] if index < len(short_term) else -math.inf)


class IntegratedLoudness:
    """R128 integrated loudness of what has played since :meth:`reset`.

    Fed one momentary value per 100 ms of program time (``add`` keys it by
    the timeline block, so a UI tick faster than 100 ms can't count a block
    twice), gated like ``ebur128``: blocks at or below -70 LUFS are dropped,
    then those more than 10 LU below the mean of the rest."""

    def __init__(self) -> None:
        self._blocks: Dict[int, float] = {}

    def reset(self) -> None:
        self._blocks.clear()

    def add(self, timeline_time: float, momentary: Optional[float]) -> None:
        block = int(timeline_time * BLOCKS_PER_SECOND)
        self._blocks[block] = momentary if momentary is not None else -math.inf

    def value(self) -> Optional[float]:
        gated = [m for m in self._blocks.values() if m > SILENCE_LUFS]
        if not gated:
            return None
        relative = _mean_lufs(gated) - RELATIVE_GATE_LU
        kept = [m for m in gated if m > relative]
        return _mean_lufs(kept) if kept else None


def _mean_lufs(values: List[float]) -> float:
    """Energy mean of loudness values (the -0.691 dB offset cancels out)."""
    return 10.0 * math.log10(sum(10.0 ** (v / 10.0) for v in values) / len(values))


def format_lufs(value: Optional[float]) -> str:
    return "  -∞ " if value is None else f"{value:5.1f}"
