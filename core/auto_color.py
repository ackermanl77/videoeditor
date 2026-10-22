"""Auto color (F36): one click measures a frame and sets per-channel levels.

A small RGB frame of the clip (FFmpeg, ``rgb24`` at SAMPLE_WIDTH px) gives each
channel's dark and bright percentiles. Stretching every channel from its own
dark point to its own bright point sets the black and white points and, because
each channel moves separately, neutralises a colour cast at the same time -- the
"auto levels / auto colour" of most editors. The result is a ``color_levels``
effect (core.effects), so it can be tweaked or removed like any other.

Pure Python (the app has no NumPy): a 160 px wide frame is ~14k pixels.
"""

from __future__ import annotations

import subprocess
from typing import Dict, Optional, Sequence

SAMPLE_WIDTH = 160
LOW_PERCENTILE = 0.005
HIGH_PERCENTILE = 0.995
#: Never stretch a channel by more than this (a nearly flat frame would explode).
MIN_RANGE = 0.25


def frame_args(ffmpeg: str, source: str, source_time: float) -> list:
    return [ffmpeg, "-hide_banner", "-v", "error", "-ss", f"{max(0.0, source_time):.3f}", "-i", source,
            "-frames:v", "1", "-vf", f"scale={SAMPLE_WIDTH}:-2", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]


def _percentile(sorted_values: Sequence[int], fraction: float) -> float:
    index = min(len(sorted_values) - 1, max(0, int(round(fraction * (len(sorted_values) - 1)))))
    return sorted_values[index] / 255.0


def levels_from_rgb(data: bytes) -> Optional[Dict[str, float]]:
    """``color_levels`` parameters from raw rgb24 bytes, or None for an empty frame."""
    if len(data) < 3:
        return None
    channels = [sorted(data[offset::3]) for offset in range(3)]
    params: Dict[str, float] = {}
    for name, values in zip("rgb", channels):
        low, high = _percentile(values, LOW_PERCENTILE), _percentile(values, HIGH_PERCENTILE)
        if high - low < MIN_RANGE:  # a flat channel: widen symmetrically instead of stretching it to noise
            mid = (high + low) / 2
            low, high = max(0.0, mid - MIN_RANGE / 2), min(1.0, mid + MIN_RANGE / 2)
        params[f"{name}_low"] = round(low, 4)
        params[f"{name}_high"] = round(high, 4)
    return params


def measure_levels(ffmpeg: str, source: str, source_time: float, runner=subprocess.run) -> Optional[Dict[str, float]]:
    """The levels for the frame at ``source_time`` of ``source``; None if FFmpeg can't read it."""
    run = runner(frame_args(ffmpeg, source, source_time), capture_output=True, timeout=30,
                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if run.returncode != 0:
        return None
    return levels_from_rgb(run.stdout)
