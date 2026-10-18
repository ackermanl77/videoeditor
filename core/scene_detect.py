"""Scene-cut detection (F19): where a clip's source changes shot, from FFmpeg's ``scdet``.

``detect_scene_cuts()`` runs ``scdet`` on a 320 px-wide decode of the clip's own trimmed source
range (fast: the shot changes, not the detail, are what matter) and returns the cut times in source
seconds. ``build_scene_cut_command()`` turns them into one undoable edit on the timeline: either a
marker at each cut, or the clip split at each cut -- mapped from source to timeline time through the
clip's own speed, reverse and freeze-frame handling (``Clip.source_time_at``).
"""
from __future__ import annotations

import re
import subprocess
from typing import Callable, List, Optional

from core.clip import Clip, MediaType
from core.commands import AddMarkerCommand, SplitClipCommand
from core.ffmpeg_utils import find_binary
from core.history import Command, CompositeCommand

#: scdet's default threshold is 10 (percent of change); lower finds softer cuts (and more false ones).
DEFAULT_THRESHOLD = 10.0
#: Cuts closer than this to each other, or to the clip's ends, are dropped (flashes, fades).
MIN_SHOT_SECONDS = 0.5
_SCD_TIME = re.compile(r"lavfi\.scd\.time:\s*([0-9.]+)")


class SceneDetectionCancelled(Exception):
    pass


def detect_scene_cuts(
    path: str,
    source_in: float = 0.0,
    source_out: Optional[float] = None,
    threshold: float = DEFAULT_THRESHOLD,
    should_cancel: Optional[Callable[[], bool]] = None,
    ffmpeg: Optional[str] = None,
) -> List[float]:
    """Cut times (source seconds, ascending) between ``source_in`` and ``source_out``."""
    ffmpeg = ffmpeg or find_binary("ffmpeg")
    args = [ffmpeg, "-hide_banner", "-nostats", "-ss", f"{max(0.0, source_in):.3f}"]
    if source_out is not None:
        args += ["-t", f"{max(0.0, source_out - source_in):.3f}"]
    args += ["-i", path, "-an", "-sn", "-dn", "-vf", f"scale=320:-2,scdet=threshold={threshold:g}", "-f", "null", "-"]
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", creationflags=creation)
    times: List[float] = []
    assert process.stderr is not None
    try:
        for line in process.stderr:
            if should_cancel is not None and should_cancel():
                process.kill()
                raise SceneDetectionCancelled()
            match = _SCD_TIME.search(line)
            if match:
                times.append(float(match.group(1)))
    finally:
        process.wait()
    if process.returncode not in (0, None) and not times:
        raise RuntimeError(f"FFmpeg couldn't analyse {path} (exit code {process.returncode})")
    # scdet stamps times relative to the seeked input: back to absolute source seconds.
    cuts = sorted(source_in + t for t in times)
    end = source_out if source_out is not None else float("inf")
    kept: List[float] = []
    for cut in cuts:
        if cut - source_in < MIN_SHOT_SECONDS or end - cut < MIN_SHOT_SECONDS:
            continue
        if kept and cut - kept[-1] < MIN_SHOT_SECONDS:
            continue
        kept.append(cut)
    return kept


def timeline_offset_for_source(clip: Clip, source_time: float) -> Optional[float]:
    """Clip-local timeline seconds at which the clip shows ``source_time`` (None if it never does).
    Bisects ``Clip.source_time_at``, which is monotonic over the playable part (decreasing when the
    clip is reversed), so speed ramps and reverse are mapped exactly as the preview and export see them."""
    if not (clip.source_in <= source_time <= clip.source_out):
        return None
    lo, hi = 0.0, clip.playable_duration
    increasing = not clip.reverse
    for _ in range(60):
        mid = (lo + hi) / 2
        shown = clip.source_time_at(mid)
        if (shown < source_time) == increasing:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def build_scene_cut_command(project, clip_id: str, source_cuts: List[float], mode: str = "split") -> Optional[Command]:
    """One undoable edit for ``source_cuts`` on clip ``clip_id``: ``mode`` "split" splits the clip
    at each cut, "markers" adds a marker at each. None when no cut lands inside the clip."""
    clip = project.find_clip(clip_id)
    if clip is None or clip.media_type not in (MediaType.VIDEO, MediaType.IMAGE):
        return None
    times = []
    for cut in source_cuts:
        offset = timeline_offset_for_source(clip, cut)
        if offset is not None and MIN_SHOT_SECONDS / 2 < offset < clip.playable_duration - MIN_SHOT_SECONDS / 2:
            times.append(clip.timeline_start + offset)
    if not times:
        return None
    if mode == "markers":
        commands: List[Command] = [AddMarkerCommand(project, t, f"Cut {i + 1}") for i, t in enumerate(sorted(times))]
        return CompositeCommand(commands, f"Add {len(commands)} Scene-Cut Marker" + ("s" if len(commands) > 1 else ""))
    # Latest first: each split keeps the original clip id on the left part, so every later
    # (earlier-in-time) split still finds it.
    commands = [SplitClipCommand(project, clip_id, t) for t in sorted(times, reverse=True)]
    return CompositeCommand(commands, f"Split at {len(commands)} Scene Cut" + ("s" if len(commands) > 1 else ""))
