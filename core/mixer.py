"""Audio track mixer (F31): per-track fader, pan, mute and solo, applied in the export mix.

- **Fader** (``Track.gain_db``, -60..+12 dB) and **pan** (``Track.pan``, -1 left .. +1 right, a
  balance control: the far side is turned down, the near side stays at unity) run on each of the
  track's clips after their own effects, fades and volume -- i.e. before ducking and the master
  limiter, so a ducked music bed keeps its fader level.
- **Solo**: while any track is soloed, only soloed tracks are heard (muted ones stay silent even
  if soloed, as in most NLEs).

Qt-free; ``ui/mixer_panel.py`` is the dock.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, Optional, Tuple

GAIN_RANGE = (-60.0, 12.0)
PAN_RANGE = (-1.0, 1.0)


def clean_gain(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return float(min(max(value, GAIN_RANGE[0]), GAIN_RANGE[1]))


def clean_pan(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return float(min(max(value, PAN_RANGE[0]), PAN_RANGE[1]))


def any_solo(tracks: Iterable) -> bool:
    return any(getattr(t, "solo", False) for t in tracks)


def track_audible(track, tracks: Iterable) -> bool:
    """Whether ``track``'s sound reaches the mix (mute, then solo)."""
    if track.muted:
        return False
    return track.solo or not any_solo(tracks)


def pan_gains(pan: float) -> Tuple[float, float]:
    """(left, right) multipliers of the balance control."""
    pan = clean_pan(pan)
    return min(1.0, 1.0 - pan), min(1.0, 1.0 + pan)


def track_filter(track) -> str:
    """Comma-free filter chain for the track's fader and pan ("" when both are neutral)."""
    parts = []
    gain = clean_gain(getattr(track, "gain_db", 0.0))
    if abs(gain) > 1e-3:
        parts.append(f"volume={10 ** (gain / 20.0):.6f}")
    left, right = pan_gains(getattr(track, "pan", 0.0))
    if left < 1.0 - 1e-4 or right < 1.0 - 1e-4:
        parts.append(f"pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c1")
    return ",".join(parts)


def track_meter_gain(track) -> float:
    """The loudest channel's linear gain from fader and pan (for the panel's meter)."""
    return 10 ** (clean_gain(track.gain_db) / 20.0) * max(pan_gains(track.pan))


def apply_track_mix(project, filter_lines, audio_labels, audio_labels_by_track: Dict[str, list]) -> None:
    """Append each non-neutral track's fader/pan to its clips' labels, in place."""
    counter = 0
    for track in project.tracks:
        chain = track_filter(track)
        labels = audio_labels_by_track.get(track.id) or []
        if not chain or not labels:
            continue
        renamed = []
        for label in labels:
            new = f"mix{counter}"
            counter += 1
            filter_lines.append(f"[{label}]{chain}[{new}]")
            audio_labels[audio_labels.index(label)] = new
            renamed.append(new)
        audio_labels_by_track[track.id] = renamed


def track_levels(project, t: float, level_at) -> Dict[str, Tuple[float, float]]:
    """``{track id: (peak, rms)}`` at timeline time ``t`` for the mixer's meters.

    ``level_at(clip, clip_offset) -> (peak, rms) | None`` reads a clip's cached waveform (and its
    own gain); the track's fader/pan is applied here, and an inaudible track reads 0.
    """
    out = {}
    for track in project.tracks:
        peak = rms = 0.0
        if track_audible(track, project.tracks):
            for clip in track.clips:
                if clip.timeline_start <= t < clip.timeline_end:
                    levels: Optional[Tuple[float, float]] = level_at(clip, t - clip.timeline_start)
                    if levels:
                        gain = track_meter_gain(track)
                        peak, rms = max(peak, levels[0] * gain), max(rms, levels[1] * gain)
        out[track.id] = (min(1.0, peak), min(1.0, rms))
    return out
