"""Motion tracking (F18): follow an object in one video clip and make another clip move with it.

The tracker is FFmpeg's own ``find_rect`` filter, so no new dependency: the object -- a box the user
draws on a frame of the tracked clip -- is cut out as a small grey PGM template, and ``find_rect``
looks for it in every frame of a 480 px-wide, 15 fps decode of the clip's trimmed source range,
printing each frame's best match (``lavfi.rect.x/y/w/h`` and a ``score``, 0 = exact) through the
``metadata`` filter. It matches the template as it was drawn: an object that turns, grows or is
covered is lost for those frames (they are skipped and the keyframes interpolate across them).

The matches (source seconds + centre as a fraction of the tracked frame) are then mapped onto the
canvas through the tracked clip's fit and Transform (position, scale; Fill mode and rotation are
refused), turned into clip-local times of the follower through the tracked clip's own speed,
reverse and freeze handling, thinned, and written as the follower's position keyframes -- one undo
step. By default the follower keeps the offset it had from the object at the first tracked frame,
so a logo placed beside a face stays beside it.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from core.clip import Clip, MediaType, ReframeMode, interpolate_keyframes
from core.ffmpeg_utils import find_binary

TRACK_WIDTH = 480
TRACK_FPS = 15
#: find_rect's best-match score: 0 is a perfect match; above this the frame is treated as lost.
DEFAULT_THRESHOLD = 0.3
#: Keyframes on a straight stretch closer than this (fraction of the canvas) to the line are dropped.
SIMPLIFY_TOLERANCE = 0.002
MIN_TEMPLATE_PIXELS = 8

_FRAME = re.compile(r"pts_time:\s*([0-9.]+)")
_RECT = re.compile(r"lavfi\.rect\.(x|y|w|h|score)=([0-9.eE+-]+)")


class TrackingCancelled(Exception):
    pass


@dataclass(frozen=True)
class TrackPoint:
    source_time: float  # absolute source seconds
    x: float  # object centre, fraction of the tracked frame's width
    y: float
    score: float


def track_size(source_width: int, source_height: int) -> Tuple[int, int]:
    """The tracking decode's frame size (what ``scale=TRACK_WIDTH:-2`` gives)."""
    height = max(2, int(round(TRACK_WIDTH * source_height / max(1, source_width) / 2.0)) * 2)
    return TRACK_WIDTH, height


def template_box_pixels(box: Sequence[float], frame_size: Tuple[int, int]) -> Tuple[int, int, int, int]:
    """``(x, y, w, h)`` in tracking-frame pixels of a box given as fractions ``(x, y, w, h)`` of the frame."""
    fw, fh = frame_size
    x0 = min(max(0.0, box[0]), 1.0) * fw
    y0 = min(max(0.0, box[1]), 1.0) * fh
    x1 = min(max(0.0, box[0] + box[2]), 1.0) * fw
    y1 = min(max(0.0, box[1] + box[3]), 1.0) * fh
    w, h = int(round(x1 - x0)), int(round(y1 - y0))
    if w < MIN_TEMPLATE_PIXELS or h < MIN_TEMPLATE_PIXELS:
        raise ValueError("Draw a bigger box around the object.")
    return int(round(x0)), int(round(y0)), w, h


def _filter_path(path: str) -> str:
    from core.filter_graph import _escape_filter_value

    return _escape_filter_value(path.replace(os.sep, "/"))


def template_args(ffmpeg: str, source: str, source_time: float, crop: Tuple[int, int, int, int],
                  out_path: str) -> List[str]:
    x, y, w, h = crop
    return [ffmpeg, "-hide_banner", "-v", "error", "-y", "-ss", f"{max(0.0, source_time):.3f}", "-i", source,
            "-frames:v", "1", "-vf", f"scale={TRACK_WIDTH}:-2,crop={w}:{h}:{x}:{y},format=gray", out_path]


def track_args(ffmpeg: str, source: str, source_in: float, source_out: float, template: str,
               threshold: float = DEFAULT_THRESHOLD) -> List[str]:
    vf = (f"fps={TRACK_FPS},scale={TRACK_WIDTH}:-2,"
          f"find_rect=object={_filter_path(template)}:threshold={threshold:g},metadata=print")
    return [ffmpeg, "-hide_banner", "-nostats", "-ss", f"{max(0.0, source_in):.3f}",
            "-t", f"{max(0.0, source_out - source_in):.3f}", "-i", source, "-an", "-sn", "-dn",
            "-vf", vf, "-f", "null", "-"]


def parse_track_log(lines, source_in: float, frame_size: Tuple[int, int]) -> List[TrackPoint]:
    """TrackPoints from the ``metadata=print`` log (a frame line, then its lavfi.rect.* lines)."""
    fw, fh = frame_size
    points: List[TrackPoint] = []
    time: Optional[float] = None
    rect: dict = {}

    def flush():
        if time is not None and {"x", "y", "w", "h"} <= rect.keys():
            points.append(TrackPoint(source_in + time, (rect["x"] + rect["w"] / 2) / fw,
                                     (rect["y"] + rect["h"] / 2) / fh, rect.get("score", 0.0)))

    for line in lines:
        frame = _FRAME.search(line)
        if frame:
            flush()
            time, rect = float(frame.group(1)), {}
            continue
        match = _RECT.search(line)
        if match:
            rect[match.group(1)] = float(match.group(2))
    flush()
    return points


def track_object(source: str, source_in: float, source_out: float, template_time: float,
                 box: Sequence[float], source_size: Tuple[int, int], threshold: float = DEFAULT_THRESHOLD,
                 should_cancel: Optional[Callable[[], bool]] = None, ffmpeg: Optional[str] = None) -> List[TrackPoint]:
    """Track the object inside ``box`` (fractions of the frame at ``template_time``, source seconds)
    through ``[source_in, source_out]``. Returns the frames where it was found."""
    ffmpeg = ffmpeg or find_binary("ffmpeg")
    frame_size = track_size(*source_size)
    crop = template_box_pixels(box, frame_size)
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    handle, template = tempfile.mkstemp(prefix="ve_track_", suffix=".pgm")
    os.close(handle)
    try:
        made = subprocess.run(template_args(ffmpeg, source, template_time, crop, template), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", creationflags=creation)
        if made.returncode != 0 or os.path.getsize(template) == 0:
            raise RuntimeError(f"FFmpeg couldn't read the frame to track: {made.stderr.strip()[-300:]}")
        process = subprocess.Popen(track_args(ffmpeg, source, source_in, source_out, template, threshold),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                   errors="replace", creationflags=creation)
        assert process.stderr is not None
        lines: List[str] = []
        try:
            for line in process.stderr:
                if should_cancel is not None and should_cancel():
                    process.kill()
                    raise TrackingCancelled()
                lines.append(line)
        finally:
            process.wait()
        points = parse_track_log(lines, source_in, frame_size)
        if process.returncode not in (0, None) and not points:
            raise RuntimeError(f"FFmpeg couldn't track the object (exit code {process.returncode}).")
        return points
    finally:
        try:
            os.remove(template)
        except OSError:
            pass


# -- onto the canvas and the follower -------------------------------------------------------------
def trackable_problem(clip: Clip) -> Optional[str]:
    """Why ``clip`` can't be tracked (None if it can)."""
    if clip.media_type != MediaType.VIDEO:
        return "only a video clip can be tracked"
    if clip.reframe_mode == ReframeMode.FILL:
        return "the tracked clip uses Fill Frame"
    if abs(clip.transform_rotation) > 1e-6:
        return "the tracked clip is rotated"
    return None


def frame_to_canvas(clip: Clip, local_time: float, nx: float, ny: float, source_size: Tuple[int, int],
                    canvas_size: Tuple[int, int]) -> Tuple[float, float]:
    """A point of the tracked clip's frame (fractions) as a canvas position (fractions), through its
    fit (letterbox/pillarbox) and Transform position/scale at clip-local timeline ``local_time``."""
    sw, sh = source_size
    cw, ch = canvas_size
    src_aspect, canvas_aspect = sw / max(1, sh), cw / max(1, ch)
    fit_w, fit_h = (1.0, canvas_aspect / src_aspect) if src_aspect >= canvas_aspect else (src_aspect / canvas_aspect, 1.0)
    if len(clip.position_keyframes) >= 2:
        cx, cy = interpolate_keyframes(clip.position_keyframes, local_time, clip.easing_for("position"))
    else:
        cx, cy = clip.transform_x, clip.transform_y
    scale = clip.transform_scale if clip.transform_scale > 0 else 1.0
    return cx + (nx - 0.5) * fit_w * scale, cy + (ny - 0.5) * fit_h * scale


def _simplify(keyframes: List[Tuple[float, float, float]], tolerance: float) -> List[Tuple[float, float, float]]:
    """Drop keyframes that lie (within ``tolerance``) on the straight path between their neighbours."""
    if len(keyframes) <= 2:
        return keyframes
    kept = [keyframes[0]]
    for i in range(1, len(keyframes) - 1):
        (t0, x0, y0), (t1, x1, y1), (t2, x2, y2) = kept[-1], keyframes[i], keyframes[i + 1]
        frac = (t1 - t0) / (t2 - t0) if t2 > t0 else 0.0
        if abs(x0 + (x2 - x0) * frac - x1) > tolerance or abs(y0 + (y2 - y0) * frac - y1) > tolerance:
            kept.append(keyframes[i])
    kept.append(keyframes[-1])
    return kept


def follow_keyframes(points: Sequence[TrackPoint], tracked: Clip, follower: Clip, source_size: Tuple[int, int],
                     canvas_size: Tuple[int, int], keep_offset: bool = True,
                     tolerance: float = SIMPLIFY_TOLERANCE) -> List[Tuple[float, float, float]]:
    """The follower's position keyframes ``(clip-local seconds, x, y)`` that make it move with the
    tracked object; only the part of the track that overlaps the follower's time on the timeline."""
    from core.scene_detect import timeline_offset_for_source

    samples: List[Tuple[float, float, float]] = []
    for point in points:
        local = timeline_offset_for_source(tracked, point.source_time)
        if local is None:
            continue
        x, y = frame_to_canvas(tracked, local, point.x, point.y, source_size, canvas_size)
        follower_t = tracked.timeline_start + local - follower.timeline_start
        if -1e-6 <= follower_t <= follower.duration + 1e-6:
            samples.append((max(0.0, follower_t), x, y))
    samples.sort()
    if not samples:
        return []
    if keep_offset:
        t0 = samples[0][0]
        if len(follower.position_keyframes) >= 2:
            fx, fy = interpolate_keyframes(follower.position_keyframes, t0)
        else:
            fx, fy = follower.transform_x, follower.transform_y
        dx, dy = fx - samples[0][1], fy - samples[0][2]
        samples = [(t, x + dx, y + dy) for t, x, y in samples]
    samples = [(round(t, 4), round(x, 5), round(y, 5)) for t, x, y in samples]
    deduped = [samples[0]] + [s for prev, s in zip(samples, samples[1:]) if s[0] - prev[0] > 1e-6]
    return _simplify(deduped, tolerance) if len(deduped) >= 2 else deduped
