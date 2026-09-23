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
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.clip import Clip, MediaType, ReframeMode, TransitionType
from core.project import Project, ProjectSettings
from core.track import TrackType

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
    asset = project.find_asset_by_path(clip.source_path)
    if asset is not None:
        return asset.has_audio
    if clip.source_path in probe_cache:
        return probe_cache[clip.source_path]
    has_audio = False
    try:
        from core.media_probe import probe_file

        has_audio = probe_file(clip.source_path).has_audio
    except Exception:  # noqa: BLE001 - never let a missing/corrupt file abort export planning
        has_audio = False
    probe_cache[clip.source_path] = has_audio
    return has_audio


def _escape_path(path: str) -> str:
    """Escape a filesystem path for safe use inside a filter option value."""
    escaped = path.replace("\\", "/")
    escaped = escaped.replace(":", "\\:")
    return escaped


def _escape_text(text: str) -> str:
    """Escape literal text for use inside an FFmpeg ``drawtext`` filter."""
    escaped = text.replace("\\", "\\\\")
    escaped = escaped.replace(":", "\\:")
    escaped = escaped.replace("'", "\\'")
    escaped = escaped.replace("%", "\\%")
    return escaped


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


def _position_expr(clip: Clip) -> Tuple[str, str]:
    """Return (x_expr, y_expr) for the overlay compositing step's x/y formulas.

    A plain float string for a non-keyframed clip (identical to before
    position keyframing existed). For 2+ position keyframes, a
    piecewise-linear expression over *global* time — the overlay step runs
    after each layer's ``tpad`` shift to ``clip.timeline_start``, so ``t``
    there is project time, not the clip-local time keyframes are specified
    in; ``(t-timeline_start)`` converts back to clip-local time.

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
    t_local = f"(t-{clip.timeline_start:.6f})"
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


def _register_video_input(clip: Clip, fps: float, input_specs: List[InputSpec]) -> int:
    """Add clip's ``-i`` input (looped for a still image, seeked for video) and return its index."""
    idx = len(input_specs)
    if clip.media_type == MediaType.IMAGE:
        pre_args = ["-loop", "1", "-framerate", str(fps), "-t", f"{clip.source_length:.3f}"]
    else:
        pre_args = ["-ss", f"{max(0.0, clip.source_in):.3f}"]
    input_specs.append(InputSpec(pre_args=pre_args, path=clip.source_path))
    return idx


def _build_video_clip_chain(
    idx: int,
    clip: Clip,
    width: int,
    height: int,
    skip_fade_in: bool = False,
    skip_fade_out: bool = False,
    apply_transform: bool = True,
) -> str:
    """Build a clip's local-time (pre-``tpad``) video filter chain, ending unbracketed.

    Shared by both standalone clips and crossfade pairs — a crossfade needs
    each side's chain up to (but not including) the timeline-position
    ``tpad`` step, so the two can be combined with ``xfade`` first and
    ``tpad``-shifted only once, as a single merged layer.

    ``apply_transform=False`` (used only for crossfade-pair sides) skips
    Transform and instead pads the fitted frame to the full canvas size —
    ``xfade`` requires both its inputs to be the same size, and Transform's
    overlay-based positioning (see ``build_filter_graph``) deliberately
    allows a clip's frame to end up smaller (or larger, when zoomed in)
    than the canvas, which ``xfade`` can't reconcile between two different
    clips. Transform is a documented no-op on a clip while it's crossfading.
    """
    if clip.media_type == MediaType.IMAGE:
        chain = f"[{idx}:v]setpts=PTS-STARTPTS"
    else:
        chain = f"[{idx}:v]trim=duration={clip.source_length:.3f},setpts=PTS-STARTPTS"

    speed = clip.speed if clip.speed > 0 else 1.0
    if abs(speed - 1.0) > 1e-6:
        chain += f",setpts=PTS/{speed:.6f}"

    # format=yuva420p happens right after the fit/fill step (rather than
    # after pad, as originally) so alpha exists for every step that
    # follows, including the now-conditional pad and Transform's rotate.
    if clip.reframe_mode == ReframeMode.FILL:
        # Scale to fully COVER the canvas (the opposite of "decrease" below)
        # and crop the overflow, centered on the clip's crop focus point —
        # fills a differently-shaped canvas (e.g. 16:9 source into a 9:16
        # vertical export) with no letterboxing. The cropped frame is
        # already exactly canvas-sized, so no pad step is needed either way
        # (`apply_transform` doesn't affect this branch at all) — see
        # docs/workflows/shorts-export/workflow.md.
        chain += (
            f",scale={width}:{height}:force_original_aspect_ratio=increase,format=yuva420p"
            f",crop={width}:{height}:'(iw-{width})*{clip.crop_focus_x:.4f}'"
            f":'(ih-{height})*{clip.crop_focus_y:.4f}'"
        )
    else:
        # Fit entirely inside the canvas preserving aspect ratio (may leave
        # black bars, filled in below when this clip isn't Transform-positioned).
        chain += f",scale={width}:{height}:force_original_aspect_ratio=decrease,format=yuva420p"
        if not apply_transform:
            chain += f",pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"

    if clip.has_chroma_key:
        key_color = clip.chroma_key_color.lstrip("#") or "00FF00"
        chain += f",colorkey=0x{key_color}:{clip.chroma_key_similarity:.3f}:{clip.chroma_key_blend:.3f}"

    if clip.has_color_correction:
        chain += (
            f",eq=brightness={clip.brightness:.3f}:contrast={clip.contrast:.3f}"
            f":saturation={clip.saturation:.3f}:gamma={clip.gamma:.3f}"
        )

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


def _build_speed_ramp_video_chain(
    clip: Clip,
    width: int,
    height: int,
    clip_counter: int,
    filter_lines: List[str],
    input_specs: List[InputSpec],
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
    seg_labels: List[str] = []
    segment_specs: List[Tuple[int, float, float, float]] = []
    for i, (seg_in, seg_out, seg_speed) in enumerate(clip.speed_segments):
        idx = len(input_specs)
        input_specs.append(InputSpec(pre_args=["-ss", f"{max(0.0, seg_in):.3f}"], path=clip.source_path))
        segment_specs.append((idx, seg_in, seg_out, seg_speed))

        seg_chain = f"[{idx}:v]trim=duration={(seg_out - seg_in):.3f},setpts=PTS-STARTPTS"
        if abs(seg_speed - 1.0) > 1e-6:
            seg_chain += f",setpts=PTS/{seg_speed:.6f}"
        if clip.reframe_mode == ReframeMode.FILL:
            seg_chain += (
                f",scale={width}:{height}:force_original_aspect_ratio=increase,format=yuva420p"
                f",crop={width}:{height}:'(iw-{width})*{clip.crop_focus_x:.4f}'"
                f":'(ih-{height})*{clip.crop_focus_y:.4f}'"
            )
        else:
            seg_chain += (
                f",scale={width}:{height}:force_original_aspect_ratio=decrease"
                f",pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
                ",format=yuva420p"
            )
        if clip.has_chroma_key:
            key_color = clip.chroma_key_color.lstrip("#") or "00FF00"
            seg_chain += f",colorkey=0x{key_color}:{clip.chroma_key_similarity:.3f}:{clip.chroma_key_blend:.3f}"
        if clip.has_color_correction:
            seg_chain += (
                f",eq=brightness={clip.brightness:.3f}:contrast={clip.contrast:.3f}"
                f":saturation={clip.saturation:.3f}:gamma={clip.gamma:.3f}"
            )

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
) -> None:
    """Speed-ramped-clip counterpart to ``_maybe_add_audio``.

    Builds one ``atempo``-adjusted audio segment per video segment (reusing
    each segment's already-registered ``-i`` input, see
    ``_build_speed_ramp_video_chain``), concatenates them, then applies
    fade/volume/timeline-delay once to the whole result — identical
    end-to-end behavior to ``_build_audio_chain``, just fed by N segments
    instead of one continuous stream.
    """
    if clip.muted or clip.volume <= 0:
        return
    if not _clip_has_audio(project, clip, probe_cache):
        return

    seg_labels = []
    for i, (idx, seg_in, seg_out, seg_speed) in enumerate(segment_specs):
        seg_chain = f"[{idx}:a]atrim=duration={(seg_out - seg_in):.3f},asetpts=PTS-STARTPTS"
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

    tail = ""
    if clip.has_denoise:
        tail += f",afftdn=nr={clip.denoise_strength:.3f}"
    clip_len = clip.duration
    if clip.fade_in > 0:
        tail += f",afade=t=in:st=0:d={clip.fade_in:.3f}"
    if clip.fade_out > 0:
        fade_start = max(0.0, clip_len - clip.fade_out)
        tail += f",afade=t=out:st={fade_start:.3f}:d={clip.fade_out:.3f}"
    if abs(clip.volume - 1.0) > 1e-6:
        tail += f",volume={clip.volume:.3f}"
    if clip.timeline_start > 0:
        delay_ms = int(clip.timeline_start * 1000)
        tail += f",adelay={delay_ms}|{delay_ms}"

    if tail:
        final_label = f"sr{counter}af"
        filter_lines.append(f"[{base_label}]{tail.lstrip(',')}[{final_label}]")
        audio_labels.append(final_label)
    else:
        audio_labels.append(base_label)


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
) -> GraphResult:
    """Construct the full FFmpeg input list + filter graph for ``project``.

    ``reframe_override``, when given, forces every video/image clip's
    reframe mode for this export only (see ``_project_with_reframe_override``)
    — used by the "auto-fill vertical frame" Shorts option so it doesn't have
    to be set per-clip. ``trim_range`` (start, end), when given, cuts the
    final composited output down to that range (end defaults to the
    project's total duration when ``None``) — used for the Shorts duration
    cap and general export-range trimming.
    """

    if reframe_override is not None:
        project = _project_with_reframe_override(project, reframe_override)

    duration = max(project.total_duration, 0.1)
    width, height, fps = settings.width, settings.height, settings.fps

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

    # Base canvas: solid black, full project duration.
    filter_lines.append(
        f"color=c=black:s={width}x{height}:d={duration:.3f}:rate={fps}[base]"
    )

    video_tracks = [t for t in project.tracks if t.track_type == TrackType.VIDEO]
    audio_tracks = [t for t in project.tracks if t.track_type == TrackType.AUDIO]

    clip_counter = 0
    probe_cache: Dict[str, bool] = {}

    # ---- Video tracks (bottom to top = list order) ----
    for track_order, track in enumerate(video_tracks):
        if track.hidden:
            continue
        sorted_clips = track.sorted_clips()
        consumed_as_partner = set()

        for i, clip in enumerate(sorted_clips):
            if i in consumed_as_partner:
                continue
            if clip.media_type == MediaType.TEXT:
                text_clips.append(clip)
                continue
            if clip.media_type not in (MediaType.VIDEO, MediaType.IMAGE):
                continue

            partner_i = _find_crossfade_partner(sorted_clips, i)

            if partner_i is None and len(clip.speed_keyframes) >= 2:
                # Speed ramp: N segments concatenated, rather than one -i +
                # one chain. Transform's scale/rotation doesn't apply here
                # (see _build_speed_ramp_video_chain's docstring); position
                # (static or keyframed) still does, since that's resolved
                # separately at the compositing/overlay step below.
                body_label, segment_specs = _build_speed_ramp_video_chain(
                    clip, width, height, clip_counter, filter_lines, input_specs
                )
                suffix = _video_fade_and_opacity_suffix(clip)
                if suffix:
                    faded_label = f"sr{clip_counter}f"
                    filter_lines.append(f"[{body_label}]{suffix.lstrip(',')}[{faded_label}]")
                    body_label = faded_label
                if clip.timeline_start > 0:
                    tpad_label = f"v{clip_counter}"
                    filter_lines.append(
                        f"[{body_label}]tpad=start_duration={clip.timeline_start:.3f}:color=black@0.0[{tpad_label}]"
                    )
                    body_label = tpad_label
                video_layer_labels.append((track_order, clip.timeline_start, body_label, _position_expr(clip)))
                _before = len(audio_labels)
                _maybe_add_speed_ramped_audio(project, clip, segment_specs, clip_counter, filter_lines, audio_labels, probe_cache)
                audio_labels_by_track.setdefault(track.id, []).extend(audio_labels[_before:])
                clip_counter += 1
            elif partner_i is None:
                idx = _register_video_input(clip, fps, input_specs)
                chain = _build_video_clip_chain(idx, clip, width, height)
                if clip.timeline_start > 0:
                    chain += f",tpad=start_duration={clip.timeline_start:.3f}:color=black@0.0"
                label = f"v{clip_counter}"
                filter_lines.append(f"{chain}[{label}]")
                video_layer_labels.append((track_order, clip.timeline_start, label, _position_expr(clip)))
                _before = len(audio_labels)
                _maybe_add_audio(project, clip, idx, filter_lines, audio_labels, clip_counter, probe_cache)
                audio_labels_by_track.setdefault(track.id, []).extend(audio_labels[_before:])
                clip_counter += 1
            else:
                # Crossfade pair: build each side's chain up to (not including)
                # its own fade in/out — the xfade transition replaces those —
                # then merge and shift into position as a single layer.
                partner = sorted_clips[partner_i]
                d = _crossfade_duration(clip, partner)

                # xfade requires constant-frame-rate inputs ("inputs needs to
                # be a constant frame rate" is a hard failure otherwise) —
                # neither trim/setpts nor the final -r output flag guarantees
                # that on their own, so force it explicitly on both sides
                # feeding into it. Standalone clips don't need this: overlay
                # has no such requirement, and the exporter's -r already
                # fixes the final output rate.
                idx_a = _register_video_input(clip, fps, input_specs)
                chain_a = (
                    _build_video_clip_chain(idx_a, clip, width, height, skip_fade_out=True, apply_transform=False)
                    + f",fps={fps}"
                )
                idx_b = _register_video_input(partner, fps, input_specs)
                chain_b = (
                    _build_video_clip_chain(idx_b, partner, width, height, skip_fade_in=True, apply_transform=False)
                    + f",fps={fps}"
                )

                label_a = f"xf{clip_counter}a"
                label_b = f"xf{clip_counter}b"
                filter_lines.append(f"{chain_a}[{label_a}]")
                filter_lines.append(f"{chain_b}[{label_b}]")

                merged_label = f"xf{clip_counter}"
                offset = max(0.0, clip.duration - d)
                xfade_name = _XFADE_TRANSITION_NAMES.get(clip.transition_out, "fade")
                filter_lines.append(
                    f"[{label_a}][{label_b}]xfade=transition={xfade_name}:duration={d:.3f}:offset={offset:.3f}[{merged_label}]"
                )

                final_label = f"v{clip_counter}"
                if clip.timeline_start > 0:
                    filter_lines.append(
                        f"[{merged_label}]tpad=start_duration={clip.timeline_start:.3f}:color=black@0.0[{final_label}]"
                    )
                else:
                    final_label = merged_label
                # Transform/position-keyframes don't apply mid-crossfade (see
                # _build_video_clip_chain's docstring) — the merged output is
                # always full-canvas, so center it same as the old behavior.
                video_layer_labels.append((track_order, clip.timeline_start, final_label, ("0.5", "0.5")))

                # Audio stays independent of the video crossfade (see
                # _find_crossfade_partner's docstring) — each side keeps its
                # own volume/fade/timeline position, meeting at a clean cut.
                _before = len(audio_labels)
                _maybe_add_audio(project, clip, idx_a, filter_lines, audio_labels, clip_counter, probe_cache)
                clip_counter += 1
                _maybe_add_audio(project, partner, idx_b, filter_lines, audio_labels, clip_counter, probe_cache)
                clip_counter += 1
                audio_labels_by_track.setdefault(track.id, []).extend(audio_labels[_before:])
                consumed_as_partner.add(partner_i)

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
    for i, clip in enumerate(text_clips):
        style = clip.text_style
        if style is None:
            continue
        out_label = f"text{i}"
        text_escaped = _escape_text(style.content)
        x_expr = f"(w-text_w)*{style.position_x:.3f}"
        y_expr = f"(h-text_h)*{style.position_y:.3f}"
        weight = ":bold=1" if style.bold else ""
        draw = (
            f"[{current}]drawtext=text='{text_escaped}'"
            f":fontcolor={style.color}:fontsize={style.font_size}"
            f":bordercolor={style.outline_color}:borderw={style.outline_width}"
            f":x={x_expr}:y={y_expr}"
            f":enable='between(t,{clip.timeline_start:.3f},{clip.timeline_end:.3f})'"
            f"[{out_label}]"
        )
        filter_lines.append(draw)
        current = out_label

    video_out_label = current

    # ---- Audio tracks ----
    for track in audio_tracks:
        if track.muted:
            continue
        for clip in track.sorted_clips():
            if clip.media_type != MediaType.AUDIO or clip.muted or clip.volume <= 0:
                continue
            idx = len(input_specs)
            pre_args = ["-ss", f"{max(0.0, clip.source_in):.3f}"]
            input_specs.append(InputSpec(pre_args=pre_args, path=clip.source_path))
            label = _build_audio_chain(idx, clip, filter_lines, clip_counter)
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
    if has_audio:
        joined = "".join(f"[{lbl}]" for lbl in audio_labels)
        filter_lines.append(
            f"{joined}amix=inputs={len(audio_labels)}:duration=longest:dropout_transition=0:normalize=0[aout]"
        )
        audio_out_label = "aout"
    else:
        filter_lines.append(f"anullsrc=channel_layout=stereo:sample_rate=48000:d={duration:.3f}[aout]")
        audio_out_label = "aout"
        has_audio = True  # we always emit a (possibly silent) audio track

    if trim_range is not None:
        trim_start, trim_end = trim_range
        trim_start = max(0.0, trim_start)
        trim_end = duration if trim_end is None else min(trim_end, duration)
        if trim_end > trim_start:
            trimmed_video = "trimout_v"
            trimmed_audio = "trimout_a"
            filter_lines.append(
                f"[{video_out_label}]trim=start={trim_start:.3f}:end={trim_end:.3f},"
                f"setpts=PTS-STARTPTS[{trimmed_video}]"
            )
            filter_lines.append(
                f"[{audio_out_label}]atrim=start={trim_start:.3f}:end={trim_end:.3f},"
                f"asetpts=PTS-STARTPTS[{trimmed_audio}]"
            )
            video_out_label = trimmed_video
            audio_out_label = trimmed_audio

    filter_complex = ";".join(filter_lines)

    return GraphResult(
        input_specs=input_specs,
        filter_complex=filter_complex,
        video_out_label=video_out_label,
        audio_out_label=audio_out_label,
        has_audio=has_audio,
    )


def _maybe_add_audio(
    project: Project,
    clip: Clip,
    input_index: int,
    filter_lines: List[str],
    audio_labels: List[str],
    counter: int,
    probe_cache: Dict[str, bool],
) -> None:
    """Append clip's audio chain to ``audio_labels`` if it's actually audible.

    Shared by every place a video/image clip's own audio needs handling
    (standalone clips and both sides of a crossfade pair) so the has-audio /
    muted / volume checks can't drift out of sync between them.
    """
    if clip.media_type == MediaType.IMAGE:
        return
    if clip.muted or clip.volume <= 0:
        return
    if not _clip_has_audio(project, clip, probe_cache):
        return
    audio_labels.append(_build_audio_chain(input_index, clip, filter_lines, counter))


def _build_audio_chain(
    input_index: int,
    clip: Clip,
    filter_lines: List[str],
    counter: int,
) -> str:
    label = f"a{counter}"
    chain = f"[{input_index}:a]atrim=duration={clip.source_length:.3f},asetpts=PTS-STARTPTS"

    speed = clip.speed if clip.speed > 0 else 1.0
    if abs(speed - 1.0) > 1e-6:
        for f in _atempo_chain(speed):
            chain += f",{f}"

    # Noise reduction, applied before fade/volume so it works on the clip's
    # unmodified audio level rather than an already-faded/attenuated signal.
    if clip.has_denoise:
        chain += f",afftdn=nr={clip.denoise_strength:.3f}"

    # Fade in/out apply to a clip's audio too, not just its video alpha —
    # otherwise the Properties panel's Fade sliders silently do nothing for
    # an audio-only clip (and don't duck a video clip's own audio either).
    clip_len = clip.duration
    if clip.fade_in > 0:
        chain += f",afade=t=in:st=0:d={clip.fade_in:.3f}"
    if clip.fade_out > 0:
        fade_start = max(0.0, clip_len - clip.fade_out)
        chain += f",afade=t=out:st={fade_start:.3f}:d={clip.fade_out:.3f}"

    if abs(clip.volume - 1.0) > 1e-6:
        chain += f",volume={clip.volume:.3f}"

    if clip.timeline_start > 0:
        delay_ms = int(clip.timeline_start * 1000)
        chain += f",adelay={delay_ms}|{delay_ms}"

    filter_lines.append(f"{chain}[{label}]")
    return label
