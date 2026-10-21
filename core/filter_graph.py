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
from typing import Dict, List, Optional, Tuple

from core.clip import Clip, MediaType, ReframeMode, TransitionType
from core.mixer import apply_track_mix, track_audible
from core.output_color import SDR_8BIT, OutputColor
from core.project import Project, ProjectSettings
from core.stabilize import StabilizeJob
from core.subtitles import build_ass_script, styled_clips_by_script
from core.track import Track, TrackType
from core.filter_graph_base import (  # noqa: F401 -- moved here from this module; same names
    MIN_CROSSFADE_SECONDS,
    _clip_has_audio,
    _GraphContext,
    _escape_filter_value,
    _eased_fraction,
    _piecewise_linear_expr,
    InputSpec,
    INPUT_DURATION_MARGIN,
    _Window,
    _timeline_shift,
    _crossfade_duration,
)
from core.filter_graph_video import (  # noqa: F401 -- moved here from this module; same names
    _TONEMAP_CHAIN,
    _TO_HDR_CHAIN,
    _SDR_MATRICES,
    _source_color_filters,
    _scale_matrix,
    _with_bit_depth,
    _needs_full_decode,
    _stabilize_filters,
    _fit_or_fill,
    _source_aspect,
    _color_filters,
    _post_aspect,
    _position_expr,
    _video_placement,
    _InputSlice,
    _register_video_input,
    _clip_source_slice,
    _slice_is_exact,
    _build_video_clip_chain,
    _video_fade_and_opacity_suffix,
    _register_speed_ramp_inputs,
    _build_speed_ramp_video_chain,
)
from core.filter_graph_audio import (  # noqa: F401 -- moved here from this module; same names
    _afade,
    _volume_filter,
    _adelay_filter,
    _atempo_chain,
    MASTER_LIMITER,
    _audio_format_filter,
    _audio_placement,
    _maybe_add_speed_ramped_audio,
    _apply_audio_ducking,
    _maybe_add_audio,
    _clip_is_audible,
    _crossfade_audio_clips,
    _build_audio_chain,
)
from core.filter_graph_text import (  # noqa: F401 -- moved here from this module; same names
    _TEXT_ALIGN_FLAGS,
    REFERENCE_TEXT_HEIGHT,
    _scaled_text_px,
    _drawtext_filter,
)


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


@dataclass
class _GraphBuild:
    """Everything build_filter_graph()'s steps share while they add to the graph (R9): the
    project and output settings, the export window, and the lists each step appends to. The
    steps run in order -- video tracks, compositing, text overlays, audio tracks, ducking and
    the master mix -- and `clip_counter` numbers every clip's labels across all of them."""

    project: Project
    width: int
    height: int
    fps: float
    sample_rate: int
    window: _Window
    ctx: _GraphContext
    output_color: OutputColor
    audio_only: bool
    video_only: bool
    probe_cache: _ProbeCache = field(default_factory=_ProbeCache)
    input_specs: List[InputSpec] = field(default_factory=list)
    filter_lines: List[str] = field(default_factory=list)
    # (track_order, start, label, (pos_x_expr, pos_y_expr))
    video_layer_labels: List[Tuple[int, float, str, Tuple[str, str]]] = field(default_factory=list)
    audio_labels: List[str] = field(default_factory=list)
    # Mirrors audio_labels, but grouped by the Track.id each label's clip
    # lives on -- needed only for audio ducking (_apply_audio_ducking),
    # which has to isolate one track's own mixed audio from another's.
    audio_labels_by_track: Dict[str, List[str]] = field(default_factory=dict)
    text_clips: List[Clip] = field(default_factory=list)
    temp_files: List[str] = field(default_factory=list)
    clip_counter: int = 0

    def track_audio(self, track: Track, before: int) -> None:
        """Files the audio labels added since `before` under `track` (for ducking)."""
        self.audio_labels_by_track.setdefault(track.id, []).extend(self.audio_labels[before:])


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

    The steps (R9): _add_video_track() per video track, _composite_video_layers(),
    _add_text_overlays(), _add_audio_track() per audio track, then _mix_audio().
    """

    if reframe_override is not None:
        project = _project_with_reframe_override(project, reframe_override)

    duration = max(project.total_duration, 0.1)

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

    b = _GraphBuild(
        project=project, width=settings.width, height=settings.height, fps=settings.fps,
        sample_rate=settings.sample_rate or 48000, window=window,
        ctx=_GraphContext(color=output_color, stabilize_cached_only=stabilize_cached_only),
        output_color=output_color, audio_only=audio_only, video_only=video_only,
    )

    # Base canvas: solid black, as long as the rendered output. Every layer
    # is composited onto it with eof_action=pass, so it also bounds the
    # video's length (a clip running past a trim window's end is cut here).
    # Tagged with the target colour space: drawtext converts text colours
    # with the frame's own matrix (untagged it assumed BT.601, so text in a
    # BT.709 export came out slightly off-colour).
    b.filter_lines.append(
        f"color=c=black:s={b.width}x{b.height}:d={out_duration:.3f}:rate={fps_expr(b.fps)},{output_color.setparams()}[base]"
    )

    video_tracks = [t for t in project.tracks if t.track_type == TrackType.VIDEO]
    audio_tracks = [t for t in project.tracks if t.track_type == TrackType.AUDIO]

    # ---- Video tracks (bottom to top = list order) ----
    for track_order, track in enumerate(video_tracks):
        _add_video_track(b, track_order, track)

    current = _composite_video_layers(b)
    video_out_label, styled_warnings = _add_text_overlays(b, current)

    # ---- Audio tracks ----
    for track in audio_tracks:
        if not track_audible(track, project.tracks) or video_only:
            continue
        _add_audio_track(b, track)

    audio_out_label, has_audio, has_audible_clips = _mix_audio(b, out_duration, is_windowed)

    filter_complex = _with_bit_depth(";".join(b.filter_lines), output_color)

    return GraphResult(
        input_specs=b.input_specs,
        filter_complex=filter_complex,
        video_out_label=video_out_label,
        audio_out_label=audio_out_label,
        has_audio=has_audio,
        temp_files=b.temp_files,
        has_audible_clips=has_audible_clips,
        warnings=list(b.probe_cache.warnings) + styled_warnings,
        stabilize_jobs=list(b.ctx.stabilize_jobs),
    )


def _add_video_track(b: _GraphBuild, track_order: int, track: Track) -> None:
    """One video track's clips: text clips are collected for the overlay step; video and image
    clips become picture layers (and their embedded audio), one at a time or as crossfade pairs.

    A track's `hidden` flag removes its *picture* (video layers and text
    overlays) but keeps its audio playing; `muted` removes its audio but
    keeps the picture -- the same split every NLE uses for the eye/speaker
    toggles. (Hidden previously skipped the whole track, silently dropping
    its audio too, and `muted` was ignored entirely for a video track's own
    embedded audio.)"""
    render_video = not track.hidden and not b.audio_only
    render_audio = track_audible(track, b.project.tracks) and not b.video_only
    if not render_video and not render_audio:
        return
    sorted_clips = track.sorted_clips()
    consumed_as_partner = set()

    for i, clip in enumerate(sorted_clips):
        if i in consumed_as_partner:
            continue
        if clip.media_type == MediaType.TEXT:
            if render_video and b.window.overlaps(clip.timeline_start, clip.timeline_end):
                b.text_clips.append(clip)
            continue
        if clip.media_type not in (MediaType.VIDEO, MediaType.IMAGE):
            continue

        partner_i = _find_crossfade_partner(sorted_clips, i)

        if partner_i is None and not b.window.overlaps(clip.timeline_start, clip.timeline_end):
            b.clip_counter += 1  # outside the export window: not in the graph at all
            continue

        if partner_i is None and len(clip.speed_keyframes) >= 2:
            _add_speed_ramp_clip(b, track_order, track, clip, render_video, render_audio)
        elif partner_i is None:
            _add_single_clip(b, track_order, track, clip, render_video, render_audio)
        else:
            consumed_as_partner.add(partner_i)
            _add_crossfade_pair(b, track_order, track, clip, sorted_clips[partner_i], render_video, render_audio)


def _add_speed_ramp_clip(b: _GraphBuild, track_order: int, track: Track, clip: Clip, render_video: bool,
                         render_audio: bool) -> None:
    """Speed ramp: N segments concatenated, rather than one -i + one chain.
    Transform's scale/rotation doesn't apply here (see
    _build_speed_ramp_video_chain's docstring); position (static or
    keyframed) still does, since that's resolved separately at the
    compositing/overlay step."""
    head = b.window.head(clip.timeline_start)
    if render_video:
        body_label, segment_specs = _build_speed_ramp_video_chain(
            clip, b.width, b.height, b.clip_counter, b.filter_lines, b.input_specs,
            asset=b.project.asset_for_clip(clip), ctx=b.ctx,
        )
        suffix = _video_fade_and_opacity_suffix(clip).lstrip(",")
        placement = _video_placement(clip.timeline_start, head, b.window, seeked=False)
        tail = ",".join(part for part in (suffix, placement) if part)
        if tail:
            placed_label = f"v{b.clip_counter}"
            b.filter_lines.append(f"[{body_label}]{tail}[{placed_label}]")
            body_label = placed_label
        b.video_layer_labels.append(
            (track_order, clip.timeline_start, body_label, _position_expr(clip, b.window.start))
        )
    elif _clip_is_audible(b.project, clip, b.probe_cache):
        segment_specs = _register_speed_ramp_inputs(clip, b.input_specs)
    else:
        segment_specs = []
    if render_audio and segment_specs:
        _before = len(b.audio_labels)
        _maybe_add_speed_ramped_audio(
            b.project, clip, segment_specs, b.clip_counter, b.filter_lines, b.audio_labels, b.probe_cache, b.window,
            b.sample_rate,
        )
        b.track_audio(track, _before)
    b.clip_counter += 1


def _add_single_clip(b: _GraphBuild, track_order: int, track: Track, clip: Clip, render_video: bool,
                     render_audio: bool) -> None:
    """A clip with no crossfade: one input, its video chain and (when audible) its audio."""
    window = b.window
    source: Optional[_InputSlice] = None
    # A stabilized clip is read from its own start and trimmed to
    # the window afterwards; every other clip is seeked to it.
    full = _needs_full_decode(clip)
    input_window = None if full else window
    if render_video:
        source = _register_video_input(clip, b.fps, b.input_specs, input_window)
        chain = _build_video_clip_chain(
            source.index, clip, b.width, b.height, head=source.head, source_length=source.source_length,
            asset=b.project.asset_for_clip(clip), ctx=b.ctx,
        )
        exact = not full and _slice_is_exact(clip, window, source.head)
        placement = _video_placement(
            clip.timeline_start, source.head if exact else window.head(clip.timeline_start), window,
            seeked=exact,
        )
        if placement:
            chain += f",{placement}"
        label = f"v{b.clip_counter}"
        b.filter_lines.append(f"{chain}[{label}]")
        b.video_layer_labels.append(
            (track_order, clip.timeline_start, label, _position_expr(clip, window.start))
        )
    if render_audio and _clip_is_audible(b.project, clip, b.probe_cache):
        if source is None:
            source = _register_video_input(clip, b.fps, b.input_specs, input_window)
        _before = len(b.audio_labels)
        exact = not full and _slice_is_exact(clip, window, source.head)
        _maybe_add_audio(
            b.project, clip, source.index, b.filter_lines, b.audio_labels, b.clip_counter, b.probe_cache,
            window=window, head=source.head if exact else window.head(clip.timeline_start),
            source_length=None if full else source.source_length, seeked=exact,
            sample_rate=b.sample_rate, input_head=None if full else source.head,
        )
        b.track_audio(track, _before)
    b.clip_counter += 1


def _add_crossfade_pair(b: _GraphBuild, track_order: int, track: Track, clip: Clip, partner: Clip,
                        render_video: bool, render_audio: bool) -> None:
    """Crossfade pair: build each side's chain up to (not including)
    its own fade in/out — the xfade transition replaces those —
    then merge and shift into position as a single layer."""
    window = b.window
    d = _crossfade_duration(clip, partner)
    offset = max(0.0, clip.duration - d)
    pair_end = clip.timeline_start + offset + partner.duration
    if not window.overlaps(clip.timeline_start, pair_end):
        b.clip_counter += 2  # whole pair outside the export window
        return
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
        idx_a = _register_video_input(clip, b.fps, b.input_specs).index
        chain_a = (
            _build_video_clip_chain(
                idx_a, clip, b.width, b.height, skip_fade_out=True, apply_transform=False,
                asset=b.project.asset_for_clip(clip), ctx=b.ctx,
            )
            + f",fps={fps_expr(b.fps)}"
        )
        idx_b = _register_video_input(partner, b.fps, b.input_specs).index
        chain_b = (
            _build_video_clip_chain(
                idx_b, partner, b.width, b.height, skip_fade_in=True, apply_transform=False,
                asset=b.project.asset_for_clip(partner), ctx=b.ctx,
            )
            + f",fps={fps_expr(b.fps)}"
        )

        label_a = f"xf{b.clip_counter}a"
        label_b = f"xf{b.clip_counter}b"
        b.filter_lines.append(f"{chain_a}[{label_a}]")
        b.filter_lines.append(f"{chain_b}[{label_b}]")

        merged_label = f"xf{b.clip_counter}"
        xfade_name = _XFADE_TRANSITION_NAMES.get(clip.transition_out, "fade")
        b.filter_lines.append(
            f"[{label_a}][{label_b}]xfade=transition={xfade_name}:duration={d:.3f}:offset={offset:.3f}[{merged_label}]"
        )

        final_label = merged_label
        placement = _video_placement(clip.timeline_start, head, window, seeked=False)
        if placement:
            final_label = f"v{b.clip_counter}"
            b.filter_lines.append(f"[{merged_label}]{placement}[{final_label}]")
        # Transform/position-keyframes don't apply mid-crossfade (see
        # _build_video_clip_chain's docstring) — the merged output is
        # always full-canvas, so center it same as the old behavior.
        b.video_layer_labels.append((track_order, clip.timeline_start, final_label, ("0.5", "0.5")))

    # Audio follows the video crossfade (see _crossfade_audio_clips):
    # B's audio starts where xfade actually starts B's picture, and
    # the two sides' audio crossfade over the same d-second overlap.
    audio_a, audio_b = _crossfade_audio_clips(clip, partner, d, offset)
    _before = len(b.audio_labels)
    if render_audio and _clip_is_audible(b.project, audio_a, b.probe_cache):
        if idx_a is None:
            idx_a = _register_video_input(clip, b.fps, b.input_specs).index
        _maybe_add_audio(
            b.project, audio_a, idx_a, b.filter_lines, b.audio_labels, b.clip_counter, b.probe_cache,
            window=window, head=window.head(audio_a.timeline_start), seeked=False,
            sample_rate=b.sample_rate, curve_out="qsin",
        )
    b.clip_counter += 1
    if render_audio and _clip_is_audible(b.project, audio_b, b.probe_cache):
        if idx_b is None:
            idx_b = _register_video_input(partner, b.fps, b.input_specs).index
        _maybe_add_audio(
            b.project, audio_b, idx_b, b.filter_lines, b.audio_labels, b.clip_counter, b.probe_cache,
            window=window, head=window.head(audio_b.timeline_start), seeked=False,
            sample_rate=b.sample_rate, curve_in="qsin",
        )
    b.clip_counter += 1
    b.track_audio(track, _before)


def _composite_video_layers(b: _GraphBuild) -> str:
    """Composite video layers onto the base canvas, bottom to top; returns the last label.

    Position is expressed relative to the *canvas* (main_w/main_h) and the
    layer's own (possibly Transform-scaled/rotated, so not necessarily
    canvas-sized) frame (w/h), centering it at (transform_x, transform_y).
    For a layer at the default (0.5, 0.5) with no Transform applied, this
    expression evaluates to exactly 0:0 — identical to the old hardcoded
    overlay=0:0, so an unedited clip's composited position is unchanged."""
    current = "base"
    for i, (_track_order, _start, label, (pos_x_expr, pos_y_expr)) in enumerate(b.video_layer_labels):
        out_label = f"comp{i}"
        x_expr = f"(main_w*({pos_x_expr}))-(w/2)"
        y_expr = f"(main_h*({pos_y_expr}))-(h/2)"
        b.filter_lines.append(
            f"[{current}][{label}]overlay=x='{x_expr}':y='{y_expr}':shortest=0:eof_action=pass[{out_label}]"
        )
        current = out_label
    return current


def _add_text_overlays(b: _GraphBuild, current: str) -> Tuple[str, List[str]]:
    """Text overlays (drawn on top of the composited video); returns the video output label and
    any warnings. Styled subtitle lines (imported from an ASS/SSA script with
    its styling) are drawn by libass after them, one `ass` pass per script;
    a line whose script is gone falls back to drawtext."""
    project, window, output_color = b.project, b.window, b.output_color
    styled_by_script = styled_clips_by_script(project, b.text_clips)
    styled_ids = {clip.id for clips in styled_by_script.values() for clip in clips}
    for i, clip in enumerate(b.text_clips):
        style = clip.text_style
        if style is None or clip.id in styled_ids:
            continue
        out_label = f"text{i}"
        text_path = _write_text_file(style.content)
        b.temp_files.append(text_path)
        b.filter_lines.append(
            f"[{current}]{_drawtext_filter(style, text_path, clip, window, b.height, output_color)}[{out_label}]"
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
        b.temp_files.append(ass_path)
        out_label = f"ass{i}"
        b.filter_lines.append(
            f"[{current}]ass=filename={_escape_filter_value(ass_path.replace(os.sep, '/'))}[{out_label}]"
        )
        current = out_label
    return current, styled_warnings


def _add_audio_track(b: _GraphBuild, track: Track) -> None:
    """One audio track's clips: crossfade sides, speed ramps and plain clips, each its own input."""
    window = b.window
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
            idx = len(b.input_specs)
            pre_args = ["-ss", f"{max(0.0, clip.source_in):.3f}",
                        "-t", f"{clip.source_length + INPUT_DURATION_MARGIN:.3f}"]
            b.input_specs.append(InputSpec(pre_args=pre_args, path=clip.source_path))
            label = _build_audio_chain(
                idx, clip, b.filter_lines, b.clip_counter, window=window,
                head=window.head(clip.timeline_start), seeked=False, sample_rate=b.sample_rate,
                curve_in=curve_in, curve_out=curve_out,
            )
            b.audio_labels.append(label)
            b.audio_labels_by_track.setdefault(track.id, []).append(label)
            b.clip_counter += 1
            continue
        if len(clip.speed_keyframes) >= 2:
            # Speed ramp: one atempo'd segment per speed segment, as for a
            # video clip's own audio (without crossfade, like the video
            # path). A single constant-speed chain would play the whole
            # source at clip.speed and overrun the clip's ramped length.
            _before = len(b.audio_labels)
            _maybe_add_speed_ramped_audio(
                b.project, clip, _register_speed_ramp_inputs(clip, b.input_specs), b.clip_counter,
                b.filter_lines, b.audio_labels, b.probe_cache, window, b.sample_rate,
            )
            b.track_audio(track, _before)
            b.clip_counter += 1
            continue
        slice_head, source_in, source_length = _clip_source_slice(clip, window, b.fps)
        exact = _slice_is_exact(clip, window, slice_head)
        idx = len(b.input_specs)
        pre_args = ["-ss", f"{max(0.0, source_in):.3f}", "-t", f"{source_length + INPUT_DURATION_MARGIN:.3f}"]
        b.input_specs.append(InputSpec(pre_args=pre_args, path=clip.source_path))
        label = _build_audio_chain(
            idx, clip, b.filter_lines, b.clip_counter,
            window=window, head=slice_head if exact else window.head(clip.timeline_start),
            source_length=source_length, seeked=exact, sample_rate=b.sample_rate, input_head=slice_head,
        )
        b.audio_labels.append(label)
        b.audio_labels_by_track.setdefault(track.id, []).append(label)
        b.clip_counter += 1


def _mix_audio(b: _GraphBuild, out_duration: float, is_windowed: bool) -> Tuple[str, bool, bool]:
    """Track faders/pan and ducking, then the master mix (or silence); returns
    (audio output label, has_audio, has_audible_clips).

    Audio ducking (sidechain compression): any track with duck_trigger_track_id
    set gets its own mixed audio routed through sidechaincompress, keyed off its
    trigger track's mixed audio -- e.g. background music automatically quieting
    under narration. Mutates audio_labels in place (see _apply_audio_ducking's
    docstring). Track faders and pan (F31) first: a ducked bed keeps its fader level."""
    apply_track_mix(b.project, b.filter_lines, b.audio_labels, b.audio_labels_by_track)
    _apply_audio_ducking(b.project, b.filter_lines, b.audio_labels, b.audio_labels_by_track)

    has_audio = bool(b.audio_labels)
    has_audible_clips = has_audio
    if has_audio:
        joined = "".join(f"[{lbl}]" for lbl in b.audio_labels)
        # A clip running past a trim window's end (crossfade/speed-ramp
        # audio isn't cut at the window's end) mustn't outlast the video.
        bound = f",atrim=duration={out_duration:.3f}" if is_windowed else ""
        b.filter_lines.append(
            f"{joined}amix=inputs={len(b.audio_labels)}:duration=longest:dropout_transition=0:normalize=0"
            f",{MASTER_LIMITER}{bound}[aout]"
        )
    else:
        b.filter_lines.append(f"anullsrc=channel_layout=stereo:sample_rate={b.sample_rate}:d={out_duration:.3f}[aout]")
        has_audio = True  # we always emit a (possibly silent) audio track
    return "aout", has_audio, has_audible_clips


