"""Shared pieces of the export filter graph (R9: split out of core/filter_graph.py, which
re-exports every name): the graph context and timeline windows, input specs, filter-value
escaping, keyframe expressions and crossfade lengths."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from core.clip import Clip
from core.output_color import SDR_8BIT, OutputColor
from core.project import Project
from core.stabilize import StabilizeJob


# Minimum overlap FFmpeg's xfade is allowed to use for a crossfade — below
# this it's not worth the extra filter graph complexity over a hard cut.
MIN_CROSSFADE_SECONDS = 0.05


def _clip_has_audio(project: Project, clip: Clip, probe_cache: Dict[str, bool]) -> bool:
    """Determine whether ``clip``'s source actually has an audio stream.

    Prefers the media-pool asset's probed metadata. If the clip's source
    path isn't in the pool (e.g. the asset was removed, or the project was
    hand-edited), falls back to probing the file directly rather than
    assuming ``True`` — mapping a non-existent ``[N:a]`` stream would make
    FFmpeg fail the entire export.
    """
    asset = project.asset_for_clip(clip)
    if asset is not None:
        return asset.has_audio
    if clip.source_path in probe_cache:
        return probe_cache[clip.source_path]
    has_audio = False
    try:
        from core.media_probe import probe_file

        has_audio = probe_file(clip.source_path).has_audio
    except Exception as exc:  # noqa: BLE001 - never let a missing/corrupt file abort export planning
        has_audio = False
        warnings = getattr(probe_cache, "warnings", None)
        if warnings is not None:
            warnings.append(f"Could not probe {clip.source_path} ({exc}); its audio is left out.")
    probe_cache[clip.source_path] = has_audio
    return has_audio


@dataclass
class _GraphContext:
    """Export-wide settings the per-clip chain builders need."""

    color: OutputColor = SDR_8BIT
    # Motion analyses the stabilized clips need (see core/stabilize.py).
    stabilize_jobs: List[StabilizeJob] = field(default_factory=list)
    # Frame previews: leave stabilization out unless its analysis is already cached.
    stabilize_cached_only: bool = False


def _escape_filter_value(value: str) -> str:
    """Escape an arbitrary string for use as one filter option's value inside ``-filter_complex``.

    FFmpeg parses a filtergraph description at two levels, each with its own
    escaping (see "Notes on filtergraph escaping" in ffmpeg-filters(1)):

    1. the option value itself (``key=value`` inside one filter's argument
       list), where backslash, ``'`` and ``:`` are special; then
    2. the whole filtergraph string, where backslash, ``'``, ``[``, ``]``,
       ``,`` and ``;`` are special.

    Escaping for level 1 first and then level 2 lets any string survive
    both parsers unchanged. Used for a text overlay's ``textfile=`` path,
    which comes from ``tempfile`` and so can contain a drive-letter colon,
    spaces, or even an apostrophe in a Windows user name.
    """
    level1 = "".join("\\" + ch if ch in "\\':" else ch for ch in value)
    return "".join("\\" + ch if ch in "\\'[],;" else ch for ch in level1)


def _eased_fraction(u: str, easing: str) -> str:
    """FFmpeg expression text for core.clip.ease() of the fraction expression ``u``."""
    if easing == "ease_in":
        return f"({u}*{u})"
    if easing == "ease_out":
        return f"({u}*(2-{u}))"
    if easing == "ease_in_out":
        return f"({u}*{u}*(3-2*{u}))"
    return u


def _piecewise_linear_expr(keyframes: List[Tuple[float, float]], t_var: str = "t", easing: str = "linear") -> str:
    """Build an FFmpeg expression for piecewise-linear interpolation between keyframes.

    ``keyframes`` is a list of ``(time, value)`` pairs (need not be
    pre-sorted). Holds the first/last value outside the keyframed time
    range. ``t_var`` is the FFmpeg expression text to substitute for "the
    current time" — a plain ``"t"``/``"T"`` (whichever the target filter
    calls it) or a derived expression like ``"(t-2.5)"`` when the filter's
    own time origin doesn't match the keyframes' (see callers).

    ``easing`` (F25, core.clip.EASINGS) shapes every gap: the fraction of the
    gap goes through ease() -- quadratic in/out, smoothstep in-out -- and
    "hold" keeps each keyframe's value until the next one.
    """
    kfs = sorted(keyframes, key=lambda k: k[0])
    if len(kfs) == 1:
        return f"{kfs[0][1]:.6f}"
    expr = f"{kfs[-1][1]:.6f}"  # at/after the last keyframe: hold its value
    for i in range(len(kfs) - 1, 0, -1):
        t0, v0 = kfs[i - 1]
        t1, v1 = kfs[i]
        if easing == "hold":
            segment = f"{v0:.6f}"
        else:
            u = f"(({t_var}-{t0:.6f})/({t1:.6f}-{t0:.6f}))"
            segment = f"({v0:.6f}+({v1:.6f}-{v0:.6f})*{_eased_fraction(u, easing)})"
        expr = f"if(lt({t_var},{t1:.6f}),{segment},{expr})"
    return f"if(lt({t_var},{kfs[0][0]:.6f}),{kfs[0][1]:.6f},{expr})"  # before the first: hold its value


@dataclass
class InputSpec:
    """One FFmpeg ``-i`` input plus the args that must precede it."""

    pre_args: List[str]
    path: str


# Extra seconds of source each seeked input may decode past what its trim
# filter keeps (``-t`` on the input). Only a safety margin: the exact cut is
# still made by the ``trim``/``atrim`` filters, so frames/samples are never
# lost to it; it just stops the decoder shortly after the trim point instead
# of reading the rest of a (possibly hour-long) source file.
INPUT_DURATION_MARGIN = 0.5


@dataclass(frozen=True)
class _Window:
    """The span of project time being rendered: ``[start, end)``.

    A full export is ``[0, inf)``. A trimmed export (Trim Range, Shorts cap)
    renders only its own window: clips outside it are left out of the graph
    entirely, clips straddling its start are seeked (or trimmed) to it, and
    every layer is placed relative to ``start`` -- so the last 5 s of a long
    timeline cost about as much as 5 s, not as much as the whole timeline.
    """

    start: float = 0.0
    end: float = math.inf

    def overlaps(self, t0: float, t1: float) -> bool:
        return t1 > self.start + 1e-6 and t0 < self.end - 1e-6

    def head(self, clip_start: float) -> float:
        """Clip-local seconds cut from the front of a clip starting at ``clip_start``."""
        return max(0.0, self.start - clip_start)

    def local_end(self, clip_start: float, clip_duration: float) -> float:
        """Clip-local time at which the window ends (at most the clip's own duration)."""
        return min(clip_duration, self.end - clip_start)


def _timeline_shift(offset: float) -> str:
    """Delay a clip-local stream (first frame at PTS 0) to ``offset`` seconds of output time.

    ``setpts`` only relabels the clip's own frames. The previous
    ``tpad=start_duration=<offset>:color=black@0.0`` generated ``offset * fps``
    transparent full-canvas frames per clip, each of which ``overlay`` then
    alpha-blended over the canvas -- cost proportional to the *sum* of every
    clip's start time, quadratic in clip count for a sequential edit.
    ``overlay`` (``eof_action=pass``) passes the main input through untouched
    until the shifted layer's first frame arrives, so the result is frame-for-
    frame identical (tests/integration verifies this with ``framemd5``).
    """
    return f"setpts=PTS+{offset:.3f}/TB"


def _crossfade_duration(clip: Clip, next_clip: Clip) -> float:
    """Clamp a requested crossfade to what both clips can actually provide."""
    return min(clip.transition_duration, clip.duration, next_clip.duration)
