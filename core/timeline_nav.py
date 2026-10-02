"""Frame-accurate timeline navigation helpers (timecode, frame steps, edit/marker points).

Pure functions over the model, so the keyboard transport in ``ui/`` (frame
step, previous/next edit point or marker) is testable without Qt.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional

from core.clip import Clip
from core.project import Project

_EPS = 1e-6


def _fps(fps: float) -> float:
    return fps if fps and fps > 0 else 30.0


def format_timecode(seconds: float, fps: float) -> str:
    """``HH:MM:SS:FF`` (non-drop-frame) for ``seconds`` at ``fps``."""
    rate = _fps(fps)
    nominal = int(round(rate))
    total_frames = int(math.floor(max(0.0, seconds) * rate + _EPS))
    frames = total_frames % nominal if nominal else 0
    whole_seconds = total_frames // nominal if nominal else int(seconds)
    h, rem = divmod(whole_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}:{frames:02d}"


def snap_to_frame(seconds: float, fps: float) -> float:
    rate = _fps(fps)
    return max(0.0, round(seconds * rate) / rate)


def step_frames(seconds: float, fps: float, frames: int) -> float:
    """The time ``frames`` frames after (negative: before) the frame containing ``seconds``."""
    rate = _fps(fps)
    current = math.floor(max(0.0, seconds) * rate + _EPS)
    return max(0.0, (current + frames) / rate)


def edit_points(project: Project) -> List[float]:
    """Every clip start/end on every track, plus 0, sorted and de-duplicated."""
    points = {0.0}
    for track in project.tracks:
        for clip in track.clips:
            points.add(round(clip.timeline_start, 6))
            points.add(round(clip.timeline_end, 6))
    return sorted(points)


def marker_points(project: Project) -> List[float]:
    return sorted(m.time for m in project.markers)


def clip_keyframe_points(clip: Clip) -> List[float]:
    """Timeline times of every keyframe on ``clip`` (opacity, position, volume and speed).

    Opacity/position/volume keyframes are clip-local timeline seconds; speed
    keyframes are source seconds and are mapped through the clip's speed
    segments. Only animated properties (2+ keyframes) count.
    """
    times = set()
    for keyframes in (clip.opacity_keyframes, clip.position_keyframes, clip.volume_keyframes):
        if len(keyframes) >= 2:
            times.update(clip.timeline_start + k[0] for k in keyframes)
    if len(clip.speed_keyframes) >= 2:
        times.update(clip.timeline_start + clip.timeline_offset_at(k[0]) for k in clip.speed_keyframes)
    return sorted(round(t, 6) for t in times if clip.timeline_start - _EPS <= t <= clip.timeline_end + _EPS)


def adjacent_point(points: Iterable[float], t: float, direction: int) -> Optional[float]:
    """The nearest point strictly after (``direction > 0``) or before ``t``, or None."""
    if direction > 0:
        later = [p for p in points if p > t + _EPS]
        return min(later) if later else None
    earlier = [p for p in points if p < t - _EPS]
    return max(earlier) if earlier else None
