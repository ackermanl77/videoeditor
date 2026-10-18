"""Builds an FFmpeg ``-filter_complex`` graph from a project timeline.

This module is the heart of the export engine. It walks the project's
tracks/clips and produces:

* a list of FFmpeg input arguments (one or more ``-i`` per clip, since the
  same source file may appear multiple times with different trims), and
* a single ``filter_complex`` string that trims, scales, times, fades and
  composites every clip onto one output video stream, and mixes every
  audible clip onto one output audio stream.

Keeping this logic isolated (rather than inline in the exporter) makes it
straightforward to unit test the graph text without invoking FFmpeg, and
easy to extend with new effects later.
"""

from __future__ import annotations

import copy
import math
import os
import tempfile
from dataclasses import dataclass, field
import re
from typing import Dict, List, Optional, Tuple

from core.clip import Clip, MediaType, ReframeMode, TransitionType
from core.effects import ANALYSIS_FILE_PARAM, effect_filters
from core.media_asset import MediaAsset
from core.output_color import REFERENCE_WHITE_NITS, SDR_8BIT, OutputColor
from core.project import Project, ProjectSettings
from core.stabilize import StabilizeJob, make_job
from core.subtitles import build_ass_script, styled_clips_by_script
from core.track import Track, TrackType

# Minimum overlap FFmpeg's xfade is allowed to use for a crossfade — below
# this it's not worth the extra filter graph complexity over a hard cut.
MIN_CROSSFADE_SECONDS = 0.05

# Whether Color Correction/Chroma Key/Denoise/Transition are "active" on a
# clip (i.e. worth emitting a filter for) is decided by `Clip.has_*`
# properties in `core/clip.py`, not duplicated here -- both this module and
# the timeline's per-clip effect-indicator badges
# (`ui/widgets/timeline_items.py::ClipItem`) read the same properties so
# they can't drift apart on what counts as "non-default."

# Maps each transition-capable TransitionType member to the actual FFmpeg
# `xfade` filter's `transition=` name. NONE (and the legacy, never-wired-up
# FADE member) are deliberately excluded -- callers treat "not in this dict"
# the same as "not in TRANSITION-worthy" via `_find_crossfade_partner`'s own
# `!= TransitionType.NONE` check, and default to "fade" if a value somehow
# isn't a key here (defensive; every real transition member is listed).
_XFADE_TRANSITION_NAMES: Dict[TransitionType, str] = {
    TransitionType.CROSSFADE: "fade",
    TransitionType.FADE: "fade",
    TransitionType.WIPE_LEFT: "wipeleft",
    TransitionType.WIPE_RIGHT: "wiperight",
    TransitionType.WIPE_UP: "wipeup",
    TransitionType.WIPE_DOWN: "wipedown",
    TransitionType.SLIDE_LEFT: "slideleft",
    TransitionType.SLIDE_RIGHT: "slideright",
    TransitionType.SLIDE_UP: "slideup",
    TransitionType.SLIDE_DOWN: "slidedown",
    TransitionType.CIRCLE_CROP: "circlecrop",
    TransitionType.DISSOLVE: "dissolve",
}


def _project_with_reframe_override(project: Project, mode: ReframeMode) -> Project:
    """Return a deep copy of ``project`` with every video/image clip forced to ``mode``.

    Used for a one-off export-wide reframe override (e.g. a Shorts/vertical
    preset's "auto-fill vertical frame" option) without persisting the
    change to any clip's own stored ``reframe_mode``.
    """
    cloned = copy.deepcopy(project)
    for track in cloned.tracks:
        for clip in track.clips:
            if clip.media_type in (MediaType.VIDEO, MediaType.IMAGE):
                clip.reframe_mode = mode
    return cloned


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


class _ProbeCache(dict):
    """``path -> has_audio`` cache for :func:`_clip_has_audio`, collecting probe warnings."""

    def __init__(self) -> None:
        super().__init__()
        self.warnings: List[str] = []


def fps_expr(fps: float) -> str:
    """FFmpeg rate text for ``fps``: NTSC rates as exact fractions (``30000/1001``).

    29.97 written as a decimal is not 30000/1001, so a long export drifts by
    a frame every few minutes against the source. Other rates are passed
    through unchanged.
    """
    for numerator in (24000, 30000, 48000, 60000, 120000):
        if abs(fps - numerator / 1001) < 0.005:
            return f"{numerator}/1001"
    return str(fps)


# HDR (PQ/HLG) -> SDR BT.709: linearize with the source's own transfer,
# matrix, primaries and range given explicitly (zscale can't find a path
# from untagged/partially tagged frames), tone-map in float RGB with hable,
# then back to limited-range BT.709 YUV.
_TONEMAP_CHAIN = (
    "zscale=tin={trc}:min={matrix}:pin={prim}:rin={rng}:t=linear:npl=100:m={matrix}:p={prim}"
    ",format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0"
    ",zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)


# Any source -> the HDR target (BT.2020, PQ or HLG, 10-bit): linearize with
# the source's own description, then encode with the target transfer. SDR
# white lands on the reference white (npl=203 cd/m2: 75 % HLG, ~58 % PQ).
_TO_HDR_CHAIN = (
    "zscale=tin={trc}:min={matrix}:pin={prim}:rin={rng}:t=linear:m={matrix}:p={prim}:npl={npl}"
    ",format=gbrpf32le,zscale=p=bt2020:t={target}:m=bt2020nc:r=tv:npl={npl},format=yuv420p10le"
)
_SDR_MATRICES = ("bt709", "bt470bg", "smpte170m")


@dataclass
class _GraphContext:
    """Export-wide settings the per-clip chain builders need."""

    color: OutputColor = SDR_8BIT
    # Motion analyses the stabilized clips need (see core/stabilize.py).
    stabilize_jobs: List[StabilizeJob] = field(default_factory=list)
    # Frame previews: leave stabilization out unless its analysis is already cached.
    stabilize_cached_only: bool = False


def _source_color_filters(asset: Optional[MediaAsset], color: OutputColor = SDR_8BIT) -> str:
    """Comma-prefixed filters converting a source into the export's colour space before it is scaled (or "")."""
    if color.is_hdr:
        if asset is not None and asset.is_hdr and asset.color_transfer == color.transfer:
            return ""  # passthrough: already in the target transfer
        if asset is not None and asset.is_hdr:
            trc, matrix, prim = asset.color_transfer, asset.color_space or "bt2020nc", asset.color_primaries or "bt2020"
        else:
            # SDR (or untagged) source: BT.709, or BT.601 when tagged as such.
            matrix = asset.color_space if asset is not None and asset.color_space in _SDR_MATRICES else "bt709"
            prim = asset.color_primaries if asset is not None and asset.color_primaries in _SDR_MATRICES else "bt709"
            trc = "bt709"
        rng = "pc" if asset is not None and asset.color_range == "pc" else "tv"
        return "," + _TO_HDR_CHAIN.format(
            trc=trc, matrix=matrix, prim=prim, rng=rng, npl=REFERENCE_WHITE_NITS, target=color.transfer
        )
    if asset is None or not asset.is_hdr:
        return ""
    return "," + _TONEMAP_CHAIN.format(
        trc=asset.color_transfer,
        matrix=asset.color_space or "bt2020nc",
        prim=asset.color_primaries or "bt2020",
        rng="pc" if asset.color_range == "pc" else "tv",
    )


def _scale_matrix(asset: Optional[MediaAsset], color: OutputColor = SDR_8BIT) -> str:
    """Extra ``scale`` options converting an SD (BT.601) source's YUV matrix to BT.709 (or "")."""
    if asset is not None and asset.is_bt601 and not asset.is_hdr and not color.is_hdr:
        return ":in_color_matrix=bt601:out_color_matrix=bt709"
    return ""


def _with_bit_depth(filter_complex: str, color: OutputColor) -> str:
    """The graph with its 8-bit working formats swapped for 10-bit ones.

    Every chain is written with ``yuva420p``/``yuv420p`` (the default 8-bit
    graph); a 10-bit export uses the same graph with ``yuva420p10le``/
    ``yuv420p10le`` and 10-bit overlays, so no step quietly truncates to
    8 bits. Done in one place so no builder can be missed.
    """
    if not color.is_10bit:
        return filter_complex
    out = re.sub(r"format=yuva420p(?![0-9])", "format=yuva420p10le", filter_complex)
    out = re.sub(r"format=yuv420p(?![0-9])", "format=yuv420p10le", out)
    return out.replace("overlay=x=", "overlay=format=yuv420p10:x=")


def _needs_full_decode(clip: Clip) -> bool:
    """Whether a clip must be decoded from its own start even inside a trimmed export window.

    Stabilization replays a motion analysis frame by frame from the clip's
    first frame, so a stabilized clip can't be seeked into.
    """
    return clip.has_stabilization


def _stabilize_filters(clip: Clip, start: float, length: float, ctx: _GraphContext) -> str:
    """Comma-prefixed ``vidstabtransform`` for a stream of ``length`` source seconds read from ``start``.

    Records the matching analysis job (core/stabilize.py). With
    ``ctx.stabilize_cached_only`` (frame previews) an effect whose analysis
    isn't cached yet is left out instead.
    """
    out = ""
    for effect in clip.effects:
        if effect.kind != "stabilize" or not effect.is_renderable():
            continue
        params = effect.resolved_params()
        job = make_job(clip.source_path, start, length, params["shakiness"], params["accuracy"])
        if ctx.stabilize_cached_only and not job.is_done:
            continue
        ctx.stabilize_jobs.append(job)
        out += "," + effect.filter(extra={ANALYSIS_FILE_PARAM: job.result_path})
    return out


def _fit_or_fill(
    clip: Clip,
    width: int,
    height: int,
    pad: bool,
    asset: Optional[MediaAsset] = None,
    color: OutputColor = SDR_8BIT,
    t_offset: float = 0.0,
) -> str:
    """Comma-prefixed scaling of a clip onto the canvas, ending in ``yuva420p``.

    Shared by the normal and the speed-ramp chain (they used to carry two
    copies of this). FILL scales to cover the canvas and crops around the
    crop focus; FIT scales to fit, padded to the full canvas when ``pad``.
    "pre" effects (no alpha-capable pixel format, see core/effects.py) run
    here, before the frame gains its alpha channel.
    """
    matrix = _scale_matrix(asset, color)
    pre = effect_filters(clip.effects, "pre", t_offset, aspect=_source_aspect(asset, width, height))
    if clip.reframe_mode == ReframeMode.FILL:
        return (
            f",scale={width}:{height}:force_original_aspect_ratio=increase{matrix}{pre},format=yuva420p"
            f",crop={width}:{height}:'(iw-{width})*{clip.crop_focus_x:.4f}'"
            f":'(ih-{height})*{clip.crop_focus_y:.4f}'"
        )
    out = f",scale={width}:{height}:force_original_aspect_ratio=decrease{matrix}{pre},format=yuva420p"
    if pad:
        out += f",pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    return out


def _source_aspect(asset: Optional[MediaAsset], width: int, height: int) -> float:
    """The width/height of a clip's frame before it's fitted to the canvas (masks, F16)."""
    if asset is not None and asset.width and asset.height:
        return asset.width / asset.height
    return width / height if height else 16 / 9


def _color_filters(clip: Clip, t_offset: float = 0.0, aspect: float = 16 / 9) -> str:
    """Comma-prefixed per-pixel colour work: the clip's "post" effects (keys, colour correction, looks).

    Runs on a stream whose clock is clip-local time (minus ``t_offset`` for
    a speed ramp's later segments), so animated parameters follow the clip.
    ``aspect`` is the frame's width/height there (for masked effects).
    """
    return effect_filters(clip.effects, "post", t_offset, aspect=aspect)


def _post_aspect(clip: Clip, width: int, height: int, pad: bool, asset: Optional[MediaAsset]) -> float:
    """The frame's aspect after _fit_or_fill: the canvas when filled or padded, else the source's."""
    if clip.reframe_mode == ReframeMode.FILL or pad:
        return width / height if height else 16 / 9
    return _source_aspect(asset, width, height)


def _afade(kind: str, start: float, duration: float, curve: str = "") -> str:
    """One ``afade``; ``curve="qsin"`` is the equal-power curve used for crossfades.

    A linear (default ``tri``) crossfade of two uncorrelated sources dips
    about 3 dB in the middle; quarter-sine in/out keeps the summed power flat.
    """
    out = f"afade=t={kind}:st={start:.3f}:d={duration:.3f}" if kind == "out" else f"afade=t=in:st=0:d={duration:.3f}"
    return out + (f":curve={curve}" if curve else "")


def _volume_filter(clip: Clip) -> str:
    """Comma-prefixed gain: a per-frame envelope for 2+ volume keyframes, else the scalar volume.

    The envelope uses clip-local time: every audio chain re-stamps its
    samples to start at the clip's own start before this step.
    """
    if clip.has_volume_automation:
        return f",volume=eval=frame:volume='{_piecewise_linear_expr(clip.volume_keyframes, 't')}'"
    if abs(clip.volume - 1.0) > 1e-6:
        return f",volume={clip.volume:.3f}"
    return ""


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


def _write_text_file(content: str, suffix: str = ".txt") -> str:
    """Write a text overlay's content to a temp UTF-8 file for ``drawtext=textfile=``.

    Passing the text through a file (with ``expansion=none``) instead of
    inline ``text='...'`` sidesteps drawtext's own escaping rules entirely:
    the previous inline approach backslash-escaped ``'``/``%``/``:`` inside
    a single-quoted value, where a backslash is literal and a ``'`` always
    ends the quote -- so e.g. "Don't stop" or "50% at 10:30" made FFmpeg
    reject the whole export. ``expansion=none`` also stops drawtext treating
    ``%{...}`` sequences in the user's text as expansions. Written with
    ``newline=""`` so a multi-line overlay's line breaks reach drawtext as
    plain LF (a Windows CRLF would render the CR as a glyph). The caller
    owns the returned file's lifetime -- see :meth:`GraphResult.cleanup`.
    """
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="ve_text_")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return path


def _adelay_filter(seconds: float) -> str:
    """``adelay`` shifting *every* channel of a stream by ``seconds``.

    ``all=1`` applies the one delay to all channels -- the previous
    ``adelay=<ms>|<ms>`` form only delayed the first two, so a 5.1 (or any
    3+ channel) source had its remaining channels play early, out of sync.
    """
    return f"adelay=delays={int(round(seconds * 1000))}:all=1"


def _piecewise_linear_expr(keyframes: List[Tuple[float, float]], t_var: str = "t") -> str:
    """Build an FFmpeg expression for piecewise-linear interpolation between keyframes.

    ``keyframes`` is a list of ``(time, value)`` pairs (need not be
    pre-sorted). Holds the first/last value outside the keyframed time
    range. ``t_var`` is the FFmpeg expression text to substitute for "the
    current time" — a plain ``"t"``/``"T"`` (whichever the target filter
    calls it) or a derived expression like ``"(t-2.5)"`` when the filter's
    own time origin doesn't match the keyframes' (see callers).
    """
    kfs = sorted(keyframes, key=lambda k: k[0])
    if len(kfs) == 1:
        return f"{kfs[0][1]:.6f}"
    expr = f"{kfs[-1][1]:.6f}"  # at/after the last keyframe: hold its value
    for i in range(len(kfs) - 1, 0, -1):
        t0, v0 = kfs[i - 1]
        t1, v1 = kfs[i]
        segment = f"({v0:.6f}+({v1:.6f}-{v0:.6f})*({t_var}-{t0:.6f})/({t1:.6f}-{t0:.6f}))"
        expr = f"if(lt({t_var},{t1:.6f}),{segment},{expr})"
    return f"if(lt({t_var},{kfs[0][0]:.6f}),{kfs[0][1]:.6f},{expr})"  # before the first: hold its value


def _position_expr(clip: Clip, origin: float = 0.0) -> Tuple[str, str]:
    """Return (x_expr, y_expr) for the overlay compositing step's x/y formulas.

    A plain float string for a non-keyframed clip (identical to before
    position keyframing existed). For 2+ position keyframes, a
    piecewise-linear expression over *output* time — the overlay step runs
    after each layer has been shifted to its place on the output timeline
    (``clip.timeline_start - origin``, where ``origin`` is the start of the
    export window, 0 for a full export), so ``t`` there is output time, not
    the clip-local time keyframes are specified in;
    ``(t-(timeline_start-origin))`` converts back to clip-local time.

    In FILL reframe mode, Transform's position (static or keyframed) is
    ignored — the cropped frame already exactly covers the canvas, so
    positioning it anywhere but centered would just clip parts of an
    already-full frame. Centered (0.5, 0.5) is the correct, only sensible
    placement there.
    """
    if clip.reframe_mode == ReframeMode.FILL:
        return "0.5", "0.5"
    if len(clip.position_keyframes) < 2:
        return f"{clip.transform_x:.4f}", f"{clip.transform_y:.4f}"
    # Signed format: inside a trimmed export window a clip can start before
    # the window (a negative output-time start), giving "(t+1.500000)".
    t_local = f"(t{-(clip.timeline_start - origin):+.6f})"
    x_keyframes = [(t, x) for t, x, _y in clip.position_keyframes]
    y_keyframes = [(t, y) for t, _x, y in clip.position_keyframes]
    return _piecewise_linear_expr(x_keyframes, t_local), _piecewise_linear_expr(y_keyframes, t_local)


def _atempo_chain(speed: float) -> List[str]:
    """FFmpeg's ``atempo`` only accepts 0.5-2.0; chain filters for wider ranges."""
    if speed <= 0:
        return []
    filters = []
    remaining = speed
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) > 1e-6:
        filters.append(f"atempo={remaining:.6f}")
    return filters


@dataclass
class InputSpec:
    """One FFmpeg ``-i`` input plus the args that must precede it."""

    pre_args: List[str]
    path: str


@dataclass
class GraphResult:
    input_specs: List[InputSpec]
    filter_complex: str
    video_out_label: str
    audio_out_label: str
    has_audio: bool
    # Temp files the filter graph references by path (currently: one UTF-8
    # text file per text overlay, see _write_text_file) and so must outlive
    # every FFmpeg run using this graph. Whoever builds the graph owns them
    # and must call cleanup() when done -- core/exporter.py does so in a
    # `finally` around the whole export (every pass/fallback attempt).
    temp_files: List[str] = field(default_factory=list)
    # Whether any clip actually contributes audio (False when [aout] is
    # only the silent anullsrc fallback) -- loudness normalization skips a
    # silent mix, whose measured loudness is -inf.
    has_audible_clips: bool = False
    # Non-fatal problems found while planning (e.g. a source that could not
    # be probed, so its audio was left out). Reported with the export result.
    warnings: List[str] = field(default_factory=list)
    # Motion analyses the export must run before rendering (stabilized clips,
    # see core/stabilize.py); their result files are referenced by the graph.
    stabilize_jobs: List[StabilizeJob] = field(default_factory=list)

    def cleanup(self) -> None:
        """Delete every temp file this graph created. Safe to call repeatedly."""
        for path in self.temp_files:
            try:
                os.remove(path)
            except OSError:
                pass
        self.temp_files = []


# Master-bus peak ceiling: alimiter's limit=0.891 is -1 dBFS. amix sums its
# inputs with normalize=0 (so adding a track never quietly lowers the
# others), which let two ordinary tracks reach +3.7 dBFS and clip on decode.
# level=false keeps alimiter from auto-raising the level; latency=true
# compensates its look-ahead delay so audio stays in sync with the picture.
MASTER_LIMITER = "alimiter=limit=0.891:level=false:latency=true"


def _audio_format_filter(sample_rate: int) -> str:
    """Per-clip normalization to the project's sample rate and a stereo layout.

    Every clip reaches ``amix`` in the same format, instead of leaving a mix
    of 44.1 kHz music, 48 kHz camera audio, mono voice-over and 5.1 to
    amix's own format negotiation.
    """
    return f"aresample={sample_rate},aformat=sample_rates={sample_rate}:channel_layouts=stereo"


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


def _video_placement(clip_start: float, head: float, window: _Window, seeked: bool) -> str:
    """Filters (no leading comma, possibly empty) moving a clip-local video stream into output time.

    ``head > 0`` means the clip starts before the export window. A *seeked*
    stream (``_register_video_input(..., seek=True)``) already starts at
    clip-local time ``head`` (see ``_build_video_clip_chain``), so it only
    needs re-basing to 0; an unseeked one (speed-ramp segments, crossfade
    pairs) is trimmed to it first.
    """
    if head > 0:
        if seeked:
            return "setpts=PTS-STARTPTS"
        return f"trim=start={head:.6f},setpts=PTS-STARTPTS"
    offset = clip_start - window.start
    return _timeline_shift(offset) if offset > 0 else ""


def _audio_placement(clip_start: float, head: float, window: _Window, seeked: bool) -> str:
    """Audio counterpart of :func:`_video_placement` (``adelay`` instead of ``setpts``)."""
    if head > 0:
        if seeked:
            return "asetpts=PTS-STARTPTS"
        return f"atrim=start={head:.6f},asetpts=PTS-STARTPTS"
    offset = clip_start - window.start
    return _adelay_filter(offset) if offset > 0 else ""


@dataclass
class _InputSlice:
    """What one registered clip input actually covers (see ``_register_video_input``)."""

    index: int
    head: float  # clip-local seconds skipped by seeking the input (0 if not seeked)
    source_length: float  # source seconds the trim filter keeps


def _register_video_input(
    clip: Clip,
    fps: float,
    input_specs: List[InputSpec],
    window: Optional[_Window] = None,
) -> _InputSlice:
    """Add clip's ``-i`` input (looped for a still image, seeked for video).

    With a ``window``, the input is seeked to the first source frame inside
    it and bounded to the last one; without one (crossfade pairs, which trim
    after merging instead), it covers the whole clip. Either way ``-t``
    bounds how much the decoder reads -- it used to decode each source to
    its end even though the graph kept only the clip's own trim.
    """
    idx = len(input_specs)
    head, source_in, source_length = _clip_source_slice(clip, window, fps)
    if clip.media_type == MediaType.IMAGE:
        pre_args = ["-loop", "1", "-framerate", str(fps), "-t", f"{source_length:.3f}"]
    else:
        pre_args = [
            "-ss", f"{max(0.0, source_in):.3f}",
            "-t", f"{source_length + INPUT_DURATION_MARGIN:.3f}",
        ]
    input_specs.append(InputSpec(pre_args=pre_args, path=clip.source_path))
    return _InputSlice(index=idx, head=head, source_length=source_length)


def _clip_source_slice(clip: Clip, window: Optional[_Window], fps: float) -> Tuple[float, float, float]:
    """``(head, source_start, source_length)``: the part of a constant-speed clip's source a window needs.

    ``head`` is the clip-local time of the slice's first frame. It equals
    the window's own head except when the window starts inside the clip's
    freeze-frame tail: the slice then backs up to the clip's last frame (one
    frame's worth of source), which the freeze (``tpad``) holds, and the
    caller trims to the window afterwards (see :func:`_slice_is_exact`). A
    reversed clip plays ``source_out`` -> ``source_in``, so its slice is
    mirrored: clip-local ``[h, e]`` reads source ``[out - e*speed, out - h*speed]``.
    """
    speed = clip.speed if clip.speed > 0 else 1.0
    head = window.head(clip.timeline_start) if window is not None else 0.0
    local_end = window.local_end(clip.timeline_start, clip.duration) if window is not None else clip.duration
    if head <= 0 and local_end >= clip.duration:
        return 0.0, clip.source_in, clip.source_length
    playable = clip.playable_duration
    end = min(local_end, playable)
    head = min(head, playable)
    if end - head <= 1e-9:  # the window starts in the freeze-frame tail
        head = max(0.0, playable - 1.0 / (fps if fps > 0 else 30.0))
        end = playable
    length = max(0.0, (end - head) * speed)
    if clip.reverse:
        # A reversed clip's frames sit exactly on ``source_out - k/fps``, so
        # this start is a frame time; the input's 3-decimal ``-ss`` could round
        # it *up* past that frame and drop it. Round down instead; the
        # ``trim`` duration counts from the first frame kept, so ``length``
        # stays as it is.
        start = clip.source_out - end * speed
        return head, math.floor(start * 1000.0 + 1e-6) / 1000.0, length
    return head, clip.source_in + head * speed, length


def _slice_is_exact(clip: Clip, window: _Window, slice_head: float) -> bool:
    """Whether a seeked slice starts exactly at the window's start (else it is trimmed afterwards)."""
    return abs(slice_head - window.head(clip.timeline_start)) <= 1e-9


def _build_video_clip_chain(
    idx: int,
    clip: Clip,
    width: int,
    height: int,
    skip_fade_in: bool = False,
    skip_fade_out: bool = False,
    apply_transform: bool = True,
    head: float = 0.0,
    source_length: Optional[float] = None,
    asset: Optional[MediaAsset] = None,
    ctx: Optional[_GraphContext] = None,
) -> str:
    """Build a clip's local-time (pre-placement) video filter chain, ending unbracketed.

    Shared by both standalone clips and crossfade pairs — a crossfade needs
    each side's chain up to (but not including) the timeline-placement
    step, so the two can be combined with ``xfade`` first and shifted into
    place only once, as a single merged layer.

    ``head``/``source_length`` describe a seeked input (see
    ``_register_video_input``): the input starts ``head`` clip-local seconds
    in and holds ``source_length`` source seconds. Its frames are re-stamped
    to start at clip-local time ``head`` so every clip-local filter below
    (fades, the opacity-keyframe ``geq``) sees the same times it would for
    the whole clip.

    ``apply_transform=False`` (used only for crossfade-pair sides) skips
    Transform and instead pads the fitted frame to the full canvas size —
    ``xfade`` requires both its inputs to be the same size, and Transform's
    overlay-based positioning (see ``build_filter_graph``) deliberately
    allows a clip's frame to end up smaller (or larger, when zoomed in)
    than the canvas, which ``xfade`` can't reconcile between two different
    clips. Transform is a documented no-op on a clip while it's crossfading.
    """
    ctx = ctx or _GraphContext()
    length = clip.source_length if source_length is None else source_length
    if clip.media_type == MediaType.IMAGE:
        chain = f"[{idx}:v]setpts=PTS-STARTPTS"
    else:
        chain = f"[{idx}:v]trim=duration={length:.3f},setpts=PTS-STARTPTS"
        # Stabilization runs on the source frames as decoded (stabilized
        # clips are never seeked into, see _needs_full_decode).
        if head <= 0:
            chain += _stabilize_filters(clip, clip.source_in, length, ctx)
        if clip.reverse:
            chain += ",reverse"

    speed = clip.speed if clip.speed > 0 else 1.0
    if abs(speed - 1.0) > 1e-6:
        chain += f",setpts=PTS/{speed:.6f}"
    if head > 0:
        chain += f",setpts=PTS+{head:.6f}/TB"

    # HDR sources are tone-mapped to SDR BT.709 first; then fit/fill
    # (FILL covers the canvas and crops around the crop focus, see
    # docs/workflows/shorts-export/workflow.md; FIT is padded to the full
    # canvas only when not Transform-positioned), ending in yuva420p so alpha
    # exists for every later step (keying, fades, rotate).
    chain += _source_color_filters(asset, ctx.color)
    chain += _fit_or_fill(clip, width, height, pad=not apply_transform, asset=asset, color=ctx.color)
    chain += _color_filters(clip, aspect=_post_aspect(clip, width, height, not apply_transform, asset))

    if apply_transform and clip.reframe_mode != ReframeMode.FILL:
        if abs(clip.transform_scale - 1.0) > 1e-6:
            chain += f",scale=iw*{clip.transform_scale:.4f}:ih*{clip.transform_scale:.4f}"
        if abs(clip.transform_rotation) > 1e-6:
            radians = math.radians(clip.transform_rotation)
            chain += f",rotate={radians:.6f}:c=none:ow=rotw({radians:.6f}):oh=roth({radians:.6f})"

    chain += _video_fade_and_opacity_suffix(clip, skip_fade_in, skip_fade_out)
    return chain


def _video_fade_and_opacity_suffix(clip: Clip, skip_fade_in: bool = False, skip_fade_out: bool = False) -> str:
    """The trailing fade-in/out + opacity portion of a clip's video chain.

    Factored out so a speed-ramped clip's chain (built entirely differently
    — multiple concatenated segments, see ``_build_speed_ramp_video_chain``)
    can still apply these the exact same way, once, to its final merged
    output rather than per-segment.
    """
    suffix = ""
    clip_len = clip.duration
    # The freeze frame comes first, so fades and opacity animation reach
    # into the held frame exactly as they would on footage.
    if clip.freeze_frame_duration > 0:
        suffix += f",tpad=stop_mode=clone:stop_duration={clip.freeze_frame_duration:.3f}"
    if clip.fade_in > 0 and not skip_fade_in:
        suffix += f",fade=t=in:st=0:d={clip.fade_in:.3f}:alpha=1"
    if clip.fade_out > 0 and not skip_fade_out:
        fade_start = max(0.0, clip_len - clip.fade_out)
        suffix += f",fade=t=out:st={fade_start:.3f}:d={clip.fade_out:.3f}:alpha=1"

    if len(clip.opacity_keyframes) >= 2:
        # colorchannelmixer's options are plain floats, not per-frame
        # expressions — geq is the filter that actually supports a
        # continuously time-varying value here. lum/cb/cr are re-sampled
        # from the current frame unchanged (geq would otherwise zero a
        # plane its expression doesn't mention); only alpha is recomputed,
        # multiplying whatever alpha already exists (matching
        # colorchannelmixer=aa=X's own "aa * existing alpha" semantics) so
        # this still composes correctly with fades/rotate's transparency.
        opacity_expr = _piecewise_linear_expr(clip.opacity_keyframes, "T")
        suffix += f",geq=lum='lum(X,Y)':cb='cb(X,Y)':cr='cr(X,Y)':a='alpha(X,Y)*({opacity_expr})'"
    elif clip.opacity < 1.0:
        suffix += f",colorchannelmixer=aa={clip.opacity:.3f}"

    return suffix


def _register_speed_ramp_inputs(clip: Clip, input_specs: List[InputSpec]) -> List[Tuple[int, float, float, float]]:
    """Register one seeked ``-i`` per speed segment of ``clip``.

    Returns each segment's ``(input_index, source_in, source_out, speed)``.
    Split out of ``_build_speed_ramp_video_chain`` so a clip on a *hidden*
    video track (no picture, audio still plays) can get the same per-segment
    inputs for ``_maybe_add_speed_ramped_audio`` without building video
    chains nothing would consume (an unconsumed filter output makes FFmpeg
    refuse to run; an unused input stream is harmless).
    """
    segment_specs: List[Tuple[int, float, float, float]] = []
    for seg_in, seg_out, seg_speed in clip.speed_segments:
        idx = len(input_specs)
        pre_args = ["-ss", f"{max(0.0, seg_in):.3f}", "-t", f"{(seg_out - seg_in) + INPUT_DURATION_MARGIN:.3f}"]
        input_specs.append(InputSpec(pre_args=pre_args, path=clip.source_path))
        segment_specs.append((idx, seg_in, seg_out, seg_speed))
    return segment_specs


def _build_speed_ramp_video_chain(
    clip: Clip,
    width: int,
    height: int,
    clip_counter: int,
    filter_lines: List[str],
    input_specs: List[InputSpec],
    asset: Optional[MediaAsset] = None,
    ctx: Optional[_GraphContext] = None,
) -> Tuple[str, List[Tuple[int, float, float, float]]]:
    """Build a speed-ramped clip's video chain: N segments, each its own
    ``-i``/trim/setpts at that segment's speed, concatenated together.

    Returns ``(video_label, segment_specs)`` — the final label (unbracketed
    fade/opacity from ``_video_fade_and_opacity_suffix`` still needs to be
    appended by the caller, same as a normal clip) and each segment's
    ``(input_index, source_in, source_out, speed)``, so
    ``_maybe_add_speed_ramped_audio`` can reuse the exact same ``-i``
    registrations for that segment's audio rather than opening the file
    again — the same one-input-serves-both-streams principle
    ``_maybe_add_audio`` already relies on for a normal clip.

    Chroma key and color correction apply per segment (pure per-pixel color
    operations, size-neutral). Transform's scale/rotation does not apply
    here at all — ``concat`` requires every segment to already be the same
    fixed canvas size, which independently scaling/rotating each segment
    can't guarantee stays consistent (this mirrors the same trade-off
    already made for a crossfading clip, and for the same reason).
    """
    ctx = ctx or _GraphContext()
    seg_labels: List[str] = []
    segment_specs = _register_speed_ramp_inputs(clip, input_specs)
    t_offset = 0.0  # clip-local time of this segment's first frame (animated effects)
    ordered_specs = list(reversed(segment_specs)) if clip.reverse else list(segment_specs)
    for i, (idx, seg_in, seg_out, seg_speed) in enumerate(ordered_specs):
        seg_chain = f"[{idx}:v]trim=duration={(seg_out - seg_in):.3f},setpts=PTS-STARTPTS"
        seg_chain += _stabilize_filters(clip, seg_in, seg_out - seg_in, ctx)
        if clip.reverse:
            seg_chain += ",reverse"
        if abs(seg_speed - 1.0) > 1e-6:
            seg_chain += f",setpts=PTS/{seg_speed:.6f}"
        seg_chain += _source_color_filters(asset, ctx.color)
        seg_chain += _fit_or_fill(clip, width, height, pad=True, asset=asset, color=ctx.color, t_offset=t_offset)
        seg_chain += _color_filters(clip, t_offset, aspect=_post_aspect(clip, width, height, True, asset))
        t_offset += (seg_out - seg_in) / seg_speed

        label = f"sr{clip_counter}v{i}"
        filter_lines.append(f"{seg_chain}[{label}]")
        seg_labels.append(label)

    if len(seg_labels) == 1:
        return seg_labels[0], segment_specs

    joined = "".join(f"[{lbl}]" for lbl in seg_labels)
    merged_label = f"sr{clip_counter}v"
    filter_lines.append(f"{joined}concat=n={len(seg_labels)}:v=1:a=0[{merged_label}]")
    return merged_label, segment_specs


def _maybe_add_speed_ramped_audio(
    project: Project,
    clip: Clip,
    segment_specs: List[Tuple[int, float, float, float]],
    counter: int,
    filter_lines: List[str],
    audio_labels: List[str],
    probe_cache: Dict[str, bool],
    window: _Window = _Window(),
    sample_rate: int = 48000,
) -> None:
    """Speed-ramped-clip counterpart to ``_maybe_add_audio``.

    Builds one ``atempo``-adjusted audio segment per video segment (reusing
    each segment's already-registered ``-i`` input, see
    ``_build_speed_ramp_video_chain``), concatenates them, then applies
    fade/volume/placement once to the whole result — identical
    end-to-end behavior to ``_build_audio_chain``, just fed by N segments
    instead of one continuous stream. Segment inputs aren't seeked to an
    export window, so a clip straddling the window's start is trimmed after
    the fades instead (``_audio_placement(..., seeked=False)``).
    """
    if clip.is_silent:
        return
    if not _clip_has_audio(project, clip, probe_cache):
        return

    seg_labels = []
    ordered_specs = list(reversed(segment_specs)) if clip.reverse else list(segment_specs)
    for i, (idx, seg_in, seg_out, seg_speed) in enumerate(ordered_specs):
        seg_chain = f"[{idx}:a]atrim=duration={(seg_out - seg_in):.3f},asetpts=PTS-STARTPTS"
        if clip.reverse:
            seg_chain += ",areverse"
        if abs(seg_speed - 1.0) > 1e-6:
            for f in _atempo_chain(seg_speed):
                seg_chain += f",{f}"
        label = f"sr{counter}a{i}"
        filter_lines.append(f"{seg_chain}[{label}]")
        seg_labels.append(label)

    if len(seg_labels) > 1:
        joined = "".join(f"[{lbl}]" for lbl in seg_labels)
        base_label = f"sr{counter}a"
        filter_lines.append(f"{joined}concat=n={len(seg_labels)}:v=0:a=1[{base_label}]")
    else:
        base_label = seg_labels[0]

    tail = f",{_audio_format_filter(sample_rate)}"
    if clip.has_denoise:
        tail += f",afftdn=nr={clip.denoise_strength:.3f}"
    # The freeze-frame tail is silent (the audio simply ends there), so the
    # fade-out ends where the sound does instead of inside the silence.
    clip_len = clip.playable_duration
    if clip.fade_in > 0:
        tail += "," + _afade("in", 0.0, clip.fade_in)
    if clip.fade_out > 0:
        tail += "," + _afade("out", max(0.0, clip_len - clip.fade_out), clip.fade_out)
    tail += _volume_filter(clip)
    placement = _audio_placement(clip.timeline_start, window.head(clip.timeline_start), window, seeked=False)
    if placement:
        tail += f",{placement}"

    final_label = f"sr{counter}af"
    filter_lines.append(f"[{base_label}]{tail.lstrip(',')}[{final_label}]")
    audio_labels.append(final_label)


def _crossfade_duration(clip: Clip, next_clip: Clip) -> float:
    """Clamp a requested crossfade to what both clips can actually provide."""
    return min(clip.transition_duration, clip.duration, next_clip.duration)


def _find_crossfade_partner(clips: List[Clip], i: int) -> Optional[int]:
    """Return the index of ``clips[i]``'s crossfade partner, if a valid one follows it.

    Valid means: ``clips[i]`` requests a transition out (any
    ``TransitionType`` other than ``NONE`` -- not just the original
    ``CROSSFADE`` member; see ``_XFADE_TRANSITION_NAMES`` for the full set
    of ``xfade``-backed transitions this now covers), the next clip is a
    video/image clip touching its tail (no gap — clips can't overlap per
    ``Track.has_overlap``), and the clamped crossfade duration is meaningful.
    Only one pair is ever formed per clip — chaining three or more
    consecutive transitions on the same track isn't supported. Neither side
    may be speed-ramped: a transition already can't reconcile Transform's
    variable output size between two clips (see
    ``_build_video_clip_chain``'s docstring), and a speed-ramped clip
    doesn't produce a single chain to feed ``xfade`` in the first place.
    """
    clip = clips[i]
    if not clip.has_transition:
        return None
    if len(clip.speed_keyframes) >= 2:
        return None
    if i + 1 >= len(clips):
        return None
    nxt = clips[i + 1]
    if nxt.media_type not in (MediaType.VIDEO, MediaType.IMAGE):
        return None
    if len(nxt.speed_keyframes) >= 2:
        return None
    if abs(nxt.timeline_start - clip.timeline_end) > 0.02:
        return None
    if _crossfade_duration(clip, nxt) <= MIN_CROSSFADE_SECONDS:
        return None
    return i + 1


def transition_issue(clips: List[Clip], i: int) -> Optional[str]:
    """Why ``clips[i]``'s requested transition out won't be rendered, or
    None when it isn't requested or does join (F14: the timeline marks such
    a clip). ``clips`` is the track's clips in timeline order, as passed to
    :func:`_find_crossfade_partner` / :func:`_find_audio_crossfade_partner`,
    whose rules this spells out."""
    clip = clips[i]
    if not clip.has_transition:
        return None
    audio = clip.media_type == MediaType.AUDIO
    if not audio and len(clip.speed_keyframes) >= 2:
        return "this clip has a speed ramp"
    if i + 1 >= len(clips):
        return "no clip follows it on this track"
    nxt = clips[i + 1]
    if audio and nxt.media_type != MediaType.AUDIO:
        return "the next clip isn't audio"
    if not audio and nxt.media_type not in (MediaType.VIDEO, MediaType.IMAGE):
        return "the next clip isn't a video or an image"
    if not audio and len(nxt.speed_keyframes) >= 2:
        return "the next clip has a speed ramp"
    if abs(nxt.timeline_start - clip.timeline_end) > 0.02:
        return "there's a gap before the next clip"
    if _crossfade_duration(clip, nxt) <= MIN_CROSSFADE_SECONDS:
        return "the clips are too short for it"
    if i in _transition_partners(clips, audio):
        return "it already ends the previous clip's transition (transitions can't be chained)"
    return None


def _transition_partners(clips: List[Clip], audio: bool) -> set:
    """Indices taken as the second half of a pair, by the renderer's own
    left-to-right pass: a clip that ends one transition can't start another."""
    finder = _find_audio_crossfade_partner if audio else _find_crossfade_partner
    taken: set = set()
    for index in range(len(clips)):
        if index in taken:
            continue
        partner = finder(clips, index)
        if partner is not None:
            taken.add(partner)
    return taken


def track_transition_issues(track: Track) -> Dict[str, str]:
    """{clip id: reason} for every clip on `track` whose transition out
    won't be rendered, judged on the same clip list the renderer uses (an
    audio track's audio clips only)."""
    clips = track.sorted_clips()
    if track.track_type == TrackType.AUDIO:
        clips = [c for c in clips if c.media_type == MediaType.AUDIO]
    issues = {}
    for i, clip in enumerate(clips):
        reason = transition_issue(clips, i)
        if reason:
            issues[clip.id] = reason
    return issues


def _find_audio_crossfade_partner(clips: List[Clip], i: int) -> Optional[int]:
    """Audio-track counterpart of :func:`_find_crossfade_partner`.

    Two adjacent audio clips (no gap) where the first has a transition out
    crossfade with an equal-power curve over the transition duration. As
    with video transitions, the second clip starts ``d`` seconds early so the
    two overlap; nothing after it on the track moves.
    """
    clip = clips[i]
    if not clip.has_transition or i + 1 >= len(clips):
        return None
    nxt = clips[i + 1]
    if nxt.media_type != MediaType.AUDIO:
        return None
    if abs(nxt.timeline_start - clip.timeline_end) > 0.02:
        return None
    if _crossfade_duration(clip, nxt) <= MIN_CROSSFADE_SECONDS:
        return None
    return i + 1


def _apply_audio_ducking(
    project: Project,
    filter_lines: List[str],
    audio_labels: List[str],
    audio_labels_by_track: Dict[str, List[str]],
) -> None:
    """Route each ducked track's audio through ``sidechaincompress`` against its trigger.

    A track whose ``duck_trigger_track_id`` names another track (e.g. a
    background-music track ducking under a narration/dialogue track) has
    its own mixed audio compressed using the trigger track's mixed audio as
    the sidechain "detector" input -- so it automatically gets quieter
    whenever the trigger track is audible. Implemented via FFmpeg's
    ``sidechaincompress=threshold:ratio:attack:release`` filter, which takes
    exactly two inputs (``[main][sidechain]``) and emits one compressed
    output for ``main`` only -- the trigger's own audio is *not* consumed or
    silenced by this: its labels are left untouched in ``audio_labels``, so
    it continues to play normally, independently of being used as a
    detector here.

    Mutates ``audio_labels`` in place: the ducked track's individual clip
    labels are removed and replaced with the single compressed output
    label, so the final ``amix`` (in ``build_filter_graph``) picks up the
    ducked result instead of double-counting the pre-compression labels.
    Silently does nothing for a track with no ``duck_trigger_track_id``, an
    unresolvable/self-referencing trigger, or either side with no audible
    audio at all (nothing to duck, or nothing to duck against).
    """
    counter = 0
    for track in project.tracks:
        trigger_id = track.duck_trigger_track_id
        if not trigger_id or trigger_id == track.id:
            continue
        main_labels = audio_labels_by_track.get(track.id) or []
        trigger_labels = audio_labels_by_track.get(trigger_id) or []
        if not main_labels or not trigger_labels:
            continue

        if len(main_labels) > 1:
            main_label = f"duckmain{counter}"
            joined = "".join(f"[{lbl}]" for lbl in main_labels)
            filter_lines.append(
                f"{joined}amix=inputs={len(main_labels)}:duration=longest:dropout_transition=0:normalize=0[{main_label}]"
            )
        else:
            main_label = main_labels[0]

        if len(trigger_labels) > 1:
            trigger_label = f"ducktrig{counter}"
            joined = "".join(f"[{lbl}]" for lbl in trigger_labels)
            filter_lines.append(
                f"{joined}amix=inputs={len(trigger_labels)}:duration=longest:dropout_transition=0:normalize=0[{trigger_label}]"
            )
        else:
            trigger_label = trigger_labels[0]

        out_label = f"duckout{counter}"
        filter_lines.append(
            f"[{main_label}][{trigger_label}]sidechaincompress=threshold={track.duck_threshold:.4f}"
            f":ratio={track.duck_ratio:.2f}:attack={track.duck_attack:.2f}:release={track.duck_release:.2f}[{out_label}]"
        )

        for lbl in main_labels:
            if lbl in audio_labels:
                audio_labels.remove(lbl)
        audio_labels.append(out_label)
        counter += 1


def build_filter_graph(
    project: Project,
    settings: ProjectSettings,
    reframe_override: Optional[ReframeMode] = None,
    trim_range: Optional[Tuple[float, Optional[float]]] = None,
    audio_only: bool = False,
    video_only: bool = False,
    output_color: OutputColor = SDR_8BIT,
    stabilize_cached_only: bool = False,
) -> GraphResult:
    """Construct the full FFmpeg input list + filter graph for ``project``.

    ``output_color`` picks the working format and colour space: 8-bit SDR
    BT.709 (default), 10-bit SDR, or 10-bit HDR PQ/HLG passthrough (see
    core/output_color.py). ``stabilize_cached_only`` leaves out stabilization
    whose motion analysis isn't cached yet (frame previews); an export
    instead runs ``GraphResult.stabilize_jobs`` first.

    ``audio_only`` builds the same audio mix with no picture layers or text
    (the video output is just the black base) -- used for the loudness
    measurement pass, which would otherwise pay for a full video render.
    ``video_only`` is the reverse (the audio output is silence), used for
    rendering single preview frames (core/frame_render.py).

    ``reframe_override``, when given, forces every video/image clip's
    reframe mode for this export only (see ``_project_with_reframe_override``)
    — used by the "auto-fill vertical frame" Shorts option so it doesn't have
    to be set per-clip. ``trim_range`` (start, end), when given, renders only
    that span of the timeline (end defaults to the project's total duration
    when ``None``) — used for the Shorts duration cap and general
    export-range trimming. The graph is built for the window itself (see
    :class:`_Window`) rather than rendering the whole timeline and trimming
    the result, so the cost of a range export follows the range's length.
    """

    if reframe_override is not None:
        project = _project_with_reframe_override(project, reframe_override)

    duration = max(project.total_duration, 0.1)
    width, height, fps = settings.width, settings.height, settings.fps
    sample_rate = settings.sample_rate or 48000

    window = _Window()
    out_duration = duration
    if trim_range is not None:
        trim_start, trim_end = trim_range
        trim_start = max(0.0, trim_start)
        trim_end = duration if trim_end is None else min(trim_end, duration)
        if trim_end > trim_start:
            window = _Window(trim_start, trim_end)
            out_duration = trim_end - trim_start
    is_windowed = window.end != math.inf

    input_specs: List[InputSpec] = []
    filter_lines: List[str] = []

    # (track_order, start, label, (pos_x_expr, pos_y_expr))
    video_layer_labels: List[Tuple[int, float, str, Tuple[str, str]]] = []
    audio_labels: List[str] = []
    # Mirrors audio_labels, but grouped by the Track.id each label's clip
    # lives on -- needed only for audio ducking (_apply_audio_ducking),
    # which has to isolate one track's own mixed audio from another's.
    audio_labels_by_track: Dict[str, List[str]] = {}
    text_clips: List[Clip] = []
    temp_files: List[str] = []

    # Base canvas: solid black, as long as the rendered output. Every layer
    # is composited onto it with eof_action=pass, so it also bounds the
    # video's length (a clip running past a trim window's end is cut here).
    # Tagged with the target colour space: drawtext converts text colours
    # with the frame's own matrix (untagged it assumed BT.601, so text in a
    # BT.709 export came out slightly off-colour).
    ctx = _GraphContext(color=output_color, stabilize_cached_only=stabilize_cached_only)
    filter_lines.append(
        f"color=c=black:s={width}x{height}:d={out_duration:.3f}:rate={fps_expr(fps)},{output_color.setparams()}[base]"
    )

    video_tracks = [t for t in project.tracks if t.track_type == TrackType.VIDEO]
    audio_tracks = [t for t in project.tracks if t.track_type == TrackType.AUDIO]

    clip_counter = 0
    probe_cache = _ProbeCache()

    # ---- Video tracks (bottom to top = list order) ----
    # A track's `hidden` flag removes its *picture* (video layers and text
    # overlays) but keeps its audio playing; `muted` removes its audio but
    # keeps the picture -- the same split every NLE uses for the eye/speaker
    # toggles. (Hidden previously skipped the whole track, silently dropping
    # its audio too, and `muted` was ignored entirely for a video track's own
    # embedded audio.)
    for track_order, track in enumerate(video_tracks):
        render_video = not track.hidden and not audio_only
        render_audio = not track.muted and not video_only
        if not render_video and not render_audio:
            continue
        sorted_clips = track.sorted_clips()
        consumed_as_partner = set()

        for i, clip in enumerate(sorted_clips):
            if i in consumed_as_partner:
                continue
            if clip.media_type == MediaType.TEXT:
                if render_video and window.overlaps(clip.timeline_start, clip.timeline_end):
                    text_clips.append(clip)
                continue
            if clip.media_type not in (MediaType.VIDEO, MediaType.IMAGE):
                continue

            partner_i = _find_crossfade_partner(sorted_clips, i)

            if partner_i is None and not window.overlaps(clip.timeline_start, clip.timeline_end):
                clip_counter += 1  # outside the export window: not in the graph at all
                continue

            if partner_i is None and len(clip.speed_keyframes) >= 2:
                # Speed ramp: N segments concatenated, rather than one -i +
                # one chain. Transform's scale/rotation doesn't apply here
                # (see _build_speed_ramp_video_chain's docstring); position
                # (static or keyframed) still does, since that's resolved
                # separately at the compositing/overlay step below.
                head = window.head(clip.timeline_start)
                if render_video:
                    body_label, segment_specs = _build_speed_ramp_video_chain(
                        clip, width, height, clip_counter, filter_lines, input_specs,
                        asset=project.asset_for_clip(clip), ctx=ctx,
                    )
                    suffix = _video_fade_and_opacity_suffix(clip).lstrip(",")
                    placement = _video_placement(clip.timeline_start, head, window, seeked=False)
                    tail = ",".join(part for part in (suffix, placement) if part)
                    if tail:
                        placed_label = f"v{clip_counter}"
                        filter_lines.append(f"[{body_label}]{tail}[{placed_label}]")
                        body_label = placed_label
                    video_layer_labels.append(
                        (track_order, clip.timeline_start, body_label, _position_expr(clip, window.start))
                    )
                elif _clip_is_audible(project, clip, probe_cache):
                    segment_specs = _register_speed_ramp_inputs(clip, input_specs)
                else:
                    segment_specs = []
                if render_audio and segment_specs:
                    _before = len(audio_labels)
                    _maybe_add_speed_ramped_audio(
                        project, clip, segment_specs, clip_counter, filter_lines, audio_labels, probe_cache, window,
                        sample_rate,
                    )
                    audio_labels_by_track.setdefault(track.id, []).extend(audio_labels[_before:])
                clip_counter += 1
            elif partner_i is None:
                source: Optional[_InputSlice] = None
                # A stabilized clip is read from its own start and trimmed to
                # the window afterwards; every other clip is seeked to it.
                full = _needs_full_decode(clip)
                input_window = None if full else window
                if render_video:
                    source = _register_video_input(clip, fps, input_specs, input_window)
                    chain = _build_video_clip_chain(
                        source.index, clip, width, height, head=source.head, source_length=source.source_length,
                        asset=project.asset_for_clip(clip), ctx=ctx,
                    )
                    exact = not full and _slice_is_exact(clip, window, source.head)
                    placement = _video_placement(
                        clip.timeline_start, source.head if exact else window.head(clip.timeline_start), window,
                        seeked=exact,
                    )
                    if placement:
                        chain += f",{placement}"
                    label = f"v{clip_counter}"
                    filter_lines.append(f"{chain}[{label}]")
                    video_layer_labels.append(
                        (track_order, clip.timeline_start, label, _position_expr(clip, window.start))
                    )
                if render_audio and _clip_is_audible(project, clip, probe_cache):
                    if source is None:
                        source = _register_video_input(clip, fps, input_specs, input_window)
                    _before = len(audio_labels)
                    exact = not full and _slice_is_exact(clip, window, source.head)
                    _maybe_add_audio(
                        project, clip, source.index, filter_lines, audio_labels, clip_counter, probe_cache,
                        window=window, head=source.head if exact else window.head(clip.timeline_start),
                        source_length=None if full else source.source_length, seeked=exact,
                        sample_rate=sample_rate, input_head=None if full else source.head,
                    )
                    audio_labels_by_track.setdefault(track.id, []).extend(audio_labels[_before:])
                clip_counter += 1
            else:
                # Crossfade pair: build each side's chain up to (not including)
                # its own fade in/out — the xfade transition replaces those —
                # then merge and shift into position as a single layer.
                partner = sorted_clips[partner_i]
                d = _crossfade_duration(clip, partner)
                offset = max(0.0, clip.duration - d)
                consumed_as_partner.add(partner_i)
                pair_end = clip.timeline_start + offset + partner.duration
                if not window.overlaps(clip.timeline_start, pair_end):
                    clip_counter += 2  # whole pair outside the export window
                    continue
                # The merged layer is trimmed to the window after xfade (the
                # pair's inputs aren't seeked), so a window starting mid-pair
                # still decodes the pair from its start -- bounded by the
                # pair's own length, not the timeline's.
                head = window.head(clip.timeline_start)

                idx_a: Optional[int] = None
                idx_b: Optional[int] = None
                if render_video:
                    # xfade requires constant-frame-rate inputs ("inputs needs to
                    # be a constant frame rate" is a hard failure otherwise) —
                    # neither trim/setpts nor the final -r output flag guarantees
                    # that on their own, so force it explicitly on both sides
                    # feeding into it. Standalone clips don't need this: overlay
                    # has no such requirement, and the exporter's -r already
                    # fixes the final output rate.
                    idx_a = _register_video_input(clip, fps, input_specs).index
                    chain_a = (
                        _build_video_clip_chain(
                            idx_a, clip, width, height, skip_fade_out=True, apply_transform=False,
                            asset=project.asset_for_clip(clip), ctx=ctx,
                        )
                        + f",fps={fps_expr(fps)}"
                    )
                    idx_b = _register_video_input(partner, fps, input_specs).index
                    chain_b = (
                        _build_video_clip_chain(
                            idx_b, partner, width, height, skip_fade_in=True, apply_transform=False,
                            asset=project.asset_for_clip(partner), ctx=ctx,
                        )
                        + f",fps={fps_expr(fps)}"
                    )

                    label_a = f"xf{clip_counter}a"
                    label_b = f"xf{clip_counter}b"
                    filter_lines.append(f"{chain_a}[{label_a}]")
                    filter_lines.append(f"{chain_b}[{label_b}]")

                    merged_label = f"xf{clip_counter}"
                    xfade_name = _XFADE_TRANSITION_NAMES.get(clip.transition_out, "fade")
                    filter_lines.append(
                        f"[{label_a}][{label_b}]xfade=transition={xfade_name}:duration={d:.3f}:offset={offset:.3f}[{merged_label}]"
                    )

                    final_label = merged_label
                    placement = _video_placement(clip.timeline_start, head, window, seeked=False)
                    if placement:
                        final_label = f"v{clip_counter}"
                        filter_lines.append(f"[{merged_label}]{placement}[{final_label}]")
                    # Transform/position-keyframes don't apply mid-crossfade (see
                    # _build_video_clip_chain's docstring) — the merged output is
                    # always full-canvas, so center it same as the old behavior.
                    video_layer_labels.append((track_order, clip.timeline_start, final_label, ("0.5", "0.5")))

                # Audio follows the video crossfade (see _crossfade_audio_clips):
                # B's audio starts where xfade actually starts B's picture, and
                # the two sides' audio crossfade over the same d-second overlap.
                audio_a, audio_b = _crossfade_audio_clips(clip, partner, d, offset)
                _before = len(audio_labels)
                if render_audio and _clip_is_audible(project, audio_a, probe_cache):
                    if idx_a is None:
                        idx_a = _register_video_input(clip, fps, input_specs).index
                    _maybe_add_audio(
                        project, audio_a, idx_a, filter_lines, audio_labels, clip_counter, probe_cache,
                        window=window, head=window.head(audio_a.timeline_start), seeked=False,
                        sample_rate=sample_rate, curve_out="qsin",
                    )
                clip_counter += 1
                if render_audio and _clip_is_audible(project, audio_b, probe_cache):
                    if idx_b is None:
                        idx_b = _register_video_input(partner, fps, input_specs).index
                    _maybe_add_audio(
                        project, audio_b, idx_b, filter_lines, audio_labels, clip_counter, probe_cache,
                        window=window, head=window.head(audio_b.timeline_start), seeked=False,
                        sample_rate=sample_rate, curve_in="qsin",
                    )
                clip_counter += 1
                audio_labels_by_track.setdefault(track.id, []).extend(audio_labels[_before:])

    # ---- Composite video layers onto the base canvas, bottom to top ----
    # Position is expressed relative to the *canvas* (main_w/main_h) and the
    # layer's own (possibly Transform-scaled/rotated, so not necessarily
    # canvas-sized) frame (w/h), centering it at (transform_x, transform_y).
    # For a layer at the default (0.5, 0.5) with no Transform applied, this
    # expression evaluates to exactly 0:0 — identical to the old hardcoded
    # overlay=0:0, so an unedited clip's composited position is unchanged.
    current = "base"
    for i, (_track_order, _start, label, (pos_x_expr, pos_y_expr)) in enumerate(video_layer_labels):
        out_label = f"comp{i}"
        x_expr = f"(main_w*({pos_x_expr}))-(w/2)"
        y_expr = f"(main_h*({pos_y_expr}))-(h/2)"
        filter_lines.append(
            f"[{current}][{label}]overlay=x='{x_expr}':y='{y_expr}':shortest=0:eof_action=pass[{out_label}]"
        )
        current = out_label

    # ---- Text overlays (drawn on top of the composited video) ----
    # Styled subtitle lines (imported from an ASS/SSA script with its
    # styling) are drawn by libass after them, one `ass` pass per script;
    # a line whose script is gone falls back to drawtext.
    styled_by_script = styled_clips_by_script(project, text_clips)
    styled_ids = {clip.id for clips in styled_by_script.values() for clip in clips}
    for i, clip in enumerate(text_clips):
        style = clip.text_style
        if style is None or clip.id in styled_ids:
            continue
        out_label = f"text{i}"
        text_path = _write_text_file(style.content)
        temp_files.append(text_path)
        filter_lines.append(
            f"[{current}]{_drawtext_filter(style, text_path, clip, window, height, output_color)}[{out_label}]"
        )
        current = out_label

    styled_warnings: List[str] = []
    if styled_by_script and output_color.is_hdr:
        # libass writes SDR colour values; in a PQ/HLG frame they mean far brighter light than
        # drawtext's captions, which are mapped to the 203 cd/m2 reference white.
        styled_warnings.append(
            "Styled subtitles are drawn with SDR colour values in this HDR export, so they will look much "
            "brighter than the rest of the picture; export them in SDR, embed them in an MKV, or import them "
            "as plain text."
        )
    for i, (script_id, clips) in enumerate(styled_by_script.items()):
        script = build_ass_script(
            project.subtitle_scripts[script_id], clips, window.start, None if math.isinf(window.end) else window.end
        )
        ass_path = _write_text_file(script, suffix=".ass")
        temp_files.append(ass_path)
        out_label = f"ass{i}"
        filter_lines.append(
            f"[{current}]ass=filename={_escape_filter_value(ass_path.replace(os.sep, '/'))}[{out_label}]"
        )
        current = out_label

    video_out_label = current

    # ---- Audio tracks ----
    for track in audio_tracks:
        if track.muted or video_only:
            continue
        audio_clips = [c for c in track.sorted_clips() if c.media_type == MediaType.AUDIO]
        crossfade_sides: Dict[int, Tuple[Clip, str, str]] = {}  # index -> (timing stand-in, curve_in, curve_out)
        for i in range(len(audio_clips)):
            partner_i = _find_audio_crossfade_partner(audio_clips, i)
            if partner_i is None or i in crossfade_sides:
                continue
            first, second = audio_clips[i], audio_clips[partner_i]
            d = _crossfade_duration(first, second)
            side_a, side_b = _crossfade_audio_clips(first, second, d, max(0.0, first.duration - d))
            curve_in_a = crossfade_sides.get(i, (None, "", ""))[1]
            crossfade_sides[i] = (side_a, curve_in_a, "qsin")
            crossfade_sides[partner_i] = (side_b, "qsin", "")
        for i, original in enumerate(audio_clips):
            clip, curve_in, curve_out = crossfade_sides.get(i, (original, "", ""))
            if clip.is_silent:
                continue
            if not window.overlaps(clip.timeline_start, clip.timeline_end):
                continue
            if i in crossfade_sides:
                # A crossfade side starts earlier than its own timeline_start (or
                # ends in a fade it didn't have): read it whole, trim to the window after.
                idx = len(input_specs)
                pre_args = ["-ss", f"{max(0.0, clip.source_in):.3f}",
                            "-t", f"{clip.source_length + INPUT_DURATION_MARGIN:.3f}"]
                input_specs.append(InputSpec(pre_args=pre_args, path=clip.source_path))
                label = _build_audio_chain(
                    idx, clip, filter_lines, clip_counter, window=window,
                    head=window.head(clip.timeline_start), seeked=False, sample_rate=sample_rate,
                    curve_in=curve_in, curve_out=curve_out,
                )
                audio_labels.append(label)
                audio_labels_by_track.setdefault(track.id, []).append(label)
                clip_counter += 1
                continue
            if len(clip.speed_keyframes) >= 2:
                # Speed ramp: one atempo'd segment per speed segment, as for a
                # video clip's own audio (without crossfade, like the video
                # path). A single constant-speed chain would play the whole
                # source at clip.speed and overrun the clip's ramped length.
                _before = len(audio_labels)
                _maybe_add_speed_ramped_audio(
                    project, clip, _register_speed_ramp_inputs(clip, input_specs), clip_counter,
                    filter_lines, audio_labels, probe_cache, window, sample_rate,
                )
                audio_labels_by_track.setdefault(track.id, []).extend(audio_labels[_before:])
                clip_counter += 1
                continue
            slice_head, source_in, source_length = _clip_source_slice(clip, window, fps)
            exact = _slice_is_exact(clip, window, slice_head)
            idx = len(input_specs)
            pre_args = ["-ss", f"{max(0.0, source_in):.3f}", "-t", f"{source_length + INPUT_DURATION_MARGIN:.3f}"]
            input_specs.append(InputSpec(pre_args=pre_args, path=clip.source_path))
            label = _build_audio_chain(
                idx, clip, filter_lines, clip_counter,
                window=window, head=slice_head if exact else window.head(clip.timeline_start),
                source_length=source_length, seeked=exact, sample_rate=sample_rate, input_head=slice_head,
            )
            audio_labels.append(label)
            audio_labels_by_track.setdefault(track.id, []).append(label)
            clip_counter += 1

    # ---- Audio ducking (sidechain compression) ----
    # Any track with duck_trigger_track_id set gets its own mixed audio
    # routed through sidechaincompress, keyed off its trigger track's mixed
    # audio -- e.g. background music automatically quieting under narration.
    # Mutates audio_labels in place (see _apply_audio_ducking's docstring).
    _apply_audio_ducking(project, filter_lines, audio_labels, audio_labels_by_track)

    has_audio = bool(audio_labels)
    has_audible_clips = has_audio
    if has_audio:
        joined = "".join(f"[{lbl}]" for lbl in audio_labels)
        # A clip running past a trim window's end (crossfade/speed-ramp
        # audio isn't cut at the window's end) mustn't outlast the video.
        bound = f",atrim=duration={out_duration:.3f}" if is_windowed else ""
        filter_lines.append(
            f"{joined}amix=inputs={len(audio_labels)}:duration=longest:dropout_transition=0:normalize=0"
            f",{MASTER_LIMITER}{bound}[aout]"
        )
        audio_out_label = "aout"
    else:
        filter_lines.append(f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}:d={out_duration:.3f}[aout]")
        audio_out_label = "aout"
        has_audio = True  # we always emit a (possibly silent) audio track

    filter_complex = _with_bit_depth(";".join(filter_lines), output_color)

    return GraphResult(
        input_specs=input_specs,
        filter_complex=filter_complex,
        video_out_label=video_out_label,
        audio_out_label=audio_out_label,
        has_audio=has_audio,
        temp_files=temp_files,
        has_audible_clips=has_audible_clips,
        warnings=list(probe_cache.warnings) + styled_warnings,
        stabilize_jobs=list(ctx.stabilize_jobs),
    )


# drawtext text_align flag per TextStyle.alignment.
_TEXT_ALIGN_FLAGS = {"left": "L", "center": "C", "right": "R"}

# TextStyle sizes (font size, outline width, shadow offset, box padding) are
# "pixels at 1080p": scaled by output height / REFERENCE_TEXT_HEIGHT so a
# title covers the same share of the frame at 720p, 1080p, 4K or vertical.
REFERENCE_TEXT_HEIGHT = 1080


def _scaled_text_px(value: float, height: int, minimum: int = 0) -> int:
    return max(minimum, int(round(value * height / REFERENCE_TEXT_HEIGHT)))


def _drawtext_filter(
    style, text_path: str, clip: Clip, window: _Window, height: int, color: OutputColor = SDR_8BIT
) -> str:
    """One text overlay's ``drawtext`` filter (unbracketed), enabled over the clip's span of output time.

    The box position (``position_x``/``position_y``) keeps its long-standing
    meaning: 0 = flush left/top, 1 = flush right/bottom of the frame.
    ``alignment`` aligns lines *within* the text block (``text_align``), so
    single-line titles in existing projects don't move.
    """
    from core.fonts import resolve_font_file

    x_expr = f"(w-text_w)*{style.position_x:.3f}"
    y_expr = f"(h-text_h)*{style.position_y:.3f}"
    start = clip.timeline_start - window.start
    end = clip.timeline_end - window.start
    parts = [f"drawtext=textfile={_escape_filter_value(text_path.replace(os.sep, '/'))}:expansion=none"]
    font_file = resolve_font_file(style.font_family, style.bold, style.italic)
    if font_file:
        parts.append(f":fontfile={_escape_filter_value(font_file.replace(os.sep, '/'))}")
    parts.append(f":fontcolor={color.text_color(style.color)}:fontsize={_scaled_text_px(style.font_size, height, 1)}")
    parts.append(f":bordercolor={color.text_color(style.outline_color)}:borderw={_scaled_text_px(style.outline_width, height)}")
    parts.append(f":text_align={_TEXT_ALIGN_FLAGS.get(style.alignment, 'C')}")
    if style.shadow_enabled:
        offset = _scaled_text_px(style.shadow_offset, height)
        parts.append(f":shadowcolor={color.text_color(style.shadow_color)}:shadowx={offset}:shadowy={offset}")
    if style.box_enabled:
        opacity = max(0.0, min(1.0, style.box_opacity))
        parts.append(
            f":box=1:boxcolor={color.text_color(style.box_color)}@{opacity:.2f}:boxborderw={_scaled_text_px(style.box_padding, height)}"
        )
    parts.append(f":x={x_expr}:y={y_expr}")
    parts.append(f":enable='between(t,{start:.3f},{end:.3f})'")
    return "".join(parts)


def _maybe_add_audio(
    project: Project,
    clip: Clip,
    input_index: int,
    filter_lines: List[str],
    audio_labels: List[str],
    counter: int,
    probe_cache: Dict[str, bool],
    window: "_Window" = _Window(),
    head: float = 0.0,
    source_length: Optional[float] = None,
    seeked: bool = True,
    sample_rate: int = 48000,
    curve_in: str = "",
    curve_out: str = "",
    input_head: Optional[float] = None,
) -> None:
    """Append clip's audio chain to ``audio_labels`` if it's actually audible.

    Shared by every place a video/image clip's own audio needs handling
    (standalone clips and both sides of a crossfade pair) so the has-audio /
    muted / volume checks can't drift out of sync between them. The window
    arguments are passed through to :func:`_build_audio_chain`.
    """
    if not _clip_is_audible(project, clip, probe_cache):
        return
    audio_labels.append(
        _build_audio_chain(
            input_index, clip, filter_lines, counter,
            window=window, head=head, source_length=source_length, seeked=seeked,
            sample_rate=sample_rate, curve_in=curve_in, curve_out=curve_out, input_head=input_head,
        )
    )


def _clip_is_audible(project: Project, clip: Clip, probe_cache: Dict[str, bool]) -> bool:
    """Whether a video/image clip's own embedded audio would be mixed at all.

    The clip-level half of the check (a track's own ``muted`` flag is the
    caller's concern). Shared by ``_maybe_add_audio`` and the hidden-track
    path in ``build_filter_graph``, which registers a clip's ``-i`` input
    purely for its audio and so needs to know up front whether to bother.
    """
    if clip.media_type == MediaType.IMAGE:
        return False
    if clip.is_silent:
        return False
    return _clip_has_audio(project, clip, probe_cache)


def _crossfade_audio_clips(clip: Clip, partner: Clip, d: float, offset: float) -> Tuple[Clip, Clip]:
    """Audio-timing stand-ins for a crossfade pair's two sides.

    ``xfade`` starts the partner's (B's) first frame ``offset`` seconds into
    the merged layer -- i.e. at ``A.timeline_start + A.duration - d``, which
    is ``d`` seconds *before* ``B.timeline_start``. B's audio used to stay at
    ``B.timeline_start`` and so lagged its own picture by ``d`` for the whole
    of clip B. The returned copy of B starts where its video actually does,
    and the two sides' audio crossfade over the same ``d``-second overlap
    (A fades out, B fades in), replacing each side's own fade there exactly
    like ``skip_fade_out``/``skip_fade_in`` do on the video side.

    Shallow copies: only scalar timing/fade fields are changed, and the
    originals (the real model objects) are never mutated.
    """
    audio_a = copy.copy(clip)
    audio_a.fade_out = d
    audio_b = copy.copy(partner)
    audio_b.timeline_start = clip.timeline_start + offset
    audio_b.fade_in = d
    return audio_a, audio_b


def _build_audio_chain(
    input_index: int,
    clip: Clip,
    filter_lines: List[str],
    counter: int,
    window: _Window = _Window(),
    head: float = 0.0,
    source_length: Optional[float] = None,
    seeked: bool = True,
    sample_rate: int = 48000,
    curve_in: str = "",
    curve_out: str = "",
    input_head: Optional[float] = None,
) -> str:
    """A clip's audio chain, placed on the output timeline, ending in ``[a<counter>]``.

    ``curve_in``/``curve_out`` set the fade curve of a crossfade side's
    fade (``qsin``, equal power); ordinary fades stay linear.

    ``seeked=True`` means the input was already seeked ``head`` clip-local
    seconds past the clip's start (and holds ``source_length`` source
    seconds); its samples are re-stamped to clip-local time so the fades
    below land where they would for the whole clip. ``seeked=False`` inputs
    start at the clip's own start and are trimmed by ``head`` afterwards.

    ``input_head`` (clip-local time of the input's first sample) is given
    for a seeked slice that does *not* start at the window (one starting in
    the freeze-frame tail, see ``_clip_source_slice``): its samples are
    stamped at ``input_head`` and the unseeked trim to ``head`` follows.
    """
    label = f"a{counter}"
    length = clip.source_length if source_length is None else source_length
    stamp = head if seeked else (input_head or 0.0)
    chain = f"[{input_index}:a]atrim=duration={length:.3f},asetpts=PTS-STARTPTS,{_audio_format_filter(sample_rate)}"
    if clip.reverse:
        chain += ",areverse"

    speed = clip.speed if clip.speed > 0 else 1.0
    if abs(speed - 1.0) > 1e-6:
        for f in _atempo_chain(speed):
            chain += f",{f}"

    # Noise reduction, applied before fade/volume so it works on the clip's
    # unmodified audio level rather than an already-faded/attenuated signal.
    if clip.has_denoise:
        chain += f",afftdn=nr={clip.denoise_strength:.3f}"

    if stamp > 0:
        chain += f",asetpts=PTS+{stamp:.6f}/TB"

    # Fade in/out apply to a clip's audio too, not just its video alpha —
    # otherwise the Properties panel's Fade sliders silently do nothing for
    # an audio-only clip (and don't duck a video clip's own audio either).
    # The freeze-frame tail is silent (the audio just ends), so the fade-out
    # ends where the sound does rather than inside that silence.
    clip_len = clip.playable_duration
    if clip.fade_in > 0:
        chain += "," + _afade("in", 0.0, clip.fade_in, curve_in)
    if clip.fade_out > 0:
        chain += "," + _afade("out", max(0.0, clip_len - clip.fade_out), clip.fade_out, curve_out)

    chain += _volume_filter(clip)

    placement = _audio_placement(clip.timeline_start, head, window, seeked)
    if placement:
        chain += f",{placement}"

    filter_lines.append(f"{chain}[{label}]")
    return label
