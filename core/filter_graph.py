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

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.clip import Clip, MediaType, TransitionType
from core.project import Project, ProjectSettings
from core.track import TrackType

# Minimum overlap FFmpeg's xfade is allowed to use for a crossfade — below
# this it's not worth the extra filter graph complexity over a hard cut.
MIN_CROSSFADE_SECONDS = 0.05

_NEUTRAL_COLOR = (0.0, 1.0, 1.0, 1.0)  # brightness, contrast, saturation, gamma


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

    # Fit inside the canvas preserving aspect ratio. format=yuva420p happens
    # here (rather than after pad, as before) so alpha exists for every step
    # that follows, including the now-conditional pad and Transform's rotate.
    chain += f",scale={width}:{height}:force_original_aspect_ratio=decrease,format=yuva420p"

    if not apply_transform:
        chain += f",pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"

    if clip.chroma_key_enabled:
        key_color = clip.chroma_key_color.lstrip("#") or "00FF00"
        chain += f",colorkey=0x{key_color}:{clip.chroma_key_similarity:.3f}:{clip.chroma_key_blend:.3f}"

    if (clip.brightness, clip.contrast, clip.saturation, clip.gamma) != _NEUTRAL_COLOR:
        chain += (
            f",eq=brightness={clip.brightness:.3f}:contrast={clip.contrast:.3f}"
            f":saturation={clip.saturation:.3f}:gamma={clip.gamma:.3f}"
        )

    if apply_transform:
        if abs(clip.transform_scale - 1.0) > 1e-6:
            chain += f",scale=iw*{clip.transform_scale:.4f}:ih*{clip.transform_scale:.4f}"
        if abs(clip.transform_rotation) > 1e-6:
            radians = math.radians(clip.transform_rotation)
            chain += f",rotate={radians:.6f}:c=none:ow=rotw({radians:.6f}):oh=roth({radians:.6f})"

    clip_len = clip.duration
    if clip.fade_in > 0 and not skip_fade_in:
        chain += f",fade=t=in:st=0:d={clip.fade_in:.3f}:alpha=1"
    if clip.fade_out > 0 and not skip_fade_out:
        fade_start = max(0.0, clip_len - clip.fade_out)
        chain += f",fade=t=out:st={fade_start:.3f}:d={clip.fade_out:.3f}:alpha=1"

    if clip.opacity < 1.0:
        chain += f",colorchannelmixer=aa={clip.opacity:.3f}"

    return chain


def _crossfade_duration(clip: Clip, next_clip: Clip) -> float:
    """Clamp a requested crossfade to what both clips can actually provide."""
    return min(clip.transition_duration, clip.duration, next_clip.duration)


def _find_crossfade_partner(clips: List[Clip], i: int) -> Optional[int]:
    """Return the index of ``clips[i]``'s crossfade partner, if a valid one follows it.

    Valid means: ``clips[i]`` requests a crossfade out, the next clip is a
    video/image clip touching its tail (no gap — clips can't overlap per
    ``Track.has_overlap``), and the clamped crossfade duration is meaningful.
    Only one pair is ever formed per clip — chaining three or more
    consecutive crossfades on the same track isn't supported.
    """
    clip = clips[i]
    if clip.transition_out != TransitionType.CROSSFADE:
        return None
    if i + 1 >= len(clips):
        return None
    nxt = clips[i + 1]
    if nxt.media_type not in (MediaType.VIDEO, MediaType.IMAGE):
        return None
    if abs(nxt.timeline_start - clip.timeline_end) > 0.02:
        return None
    if _crossfade_duration(clip, nxt) <= MIN_CROSSFADE_SECONDS:
        return None
    return i + 1


def build_filter_graph(project: Project, settings: ProjectSettings) -> GraphResult:
    """Construct the full FFmpeg input list + filter graph for ``project``."""

    duration = max(project.total_duration, 0.1)
    width, height, fps = settings.width, settings.height, settings.fps

    input_specs: List[InputSpec] = []
    filter_lines: List[str] = []

    # (track_order, start, label, (transform_x, transform_y))
    video_layer_labels: List[Tuple[int, float, str, Tuple[float, float]]] = []
    audio_labels: List[str] = []
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

            if partner_i is None:
                idx = _register_video_input(clip, fps, input_specs)
                chain = _build_video_clip_chain(idx, clip, width, height)
                if clip.timeline_start > 0:
                    chain += f",tpad=start_duration={clip.timeline_start:.3f}:color=black@0.0"
                label = f"v{clip_counter}"
                filter_lines.append(f"{chain}[{label}]")
                video_layer_labels.append((track_order, clip.timeline_start, label, (clip.transform_x, clip.transform_y)))
                _maybe_add_audio(project, clip, idx, filter_lines, audio_labels, clip_counter, probe_cache)
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
                filter_lines.append(
                    f"[{label_a}][{label_b}]xfade=transition=fade:duration={d:.3f}:offset={offset:.3f}[{merged_label}]"
                )

                final_label = f"v{clip_counter}"
                if clip.timeline_start > 0:
                    filter_lines.append(
                        f"[{merged_label}]tpad=start_duration={clip.timeline_start:.3f}:color=black@0.0[{final_label}]"
                    )
                else:
                    final_label = merged_label
                # Transform doesn't apply mid-crossfade (see
                # _build_video_clip_chain's docstring) — the merged output is
                # always full-canvas, so center it same as the old behavior.
                video_layer_labels.append((track_order, clip.timeline_start, final_label, (0.5, 0.5)))

                # Audio stays independent of the video crossfade (see
                # _find_crossfade_partner's docstring) — each side keeps its
                # own volume/fade/timeline position, meeting at a clean cut.
                _maybe_add_audio(project, clip, idx_a, filter_lines, audio_labels, clip_counter, probe_cache)
                clip_counter += 1
                _maybe_add_audio(project, partner, idx_b, filter_lines, audio_labels, clip_counter, probe_cache)
                clip_counter += 1
                consumed_as_partner.add(partner_i)

    # ---- Composite video layers onto the base canvas, bottom to top ----
    # Position is expressed relative to the *canvas* (main_w/main_h) and the
    # layer's own (possibly Transform-scaled/rotated, so not necessarily
    # canvas-sized) frame (w/h), centering it at (transform_x, transform_y).
    # For a layer at the default (0.5, 0.5) with no Transform applied, this
    # expression evaluates to exactly 0:0 — identical to the old hardcoded
    # overlay=0:0, so an unedited clip's composited position is unchanged.
    current = "base"
    for i, (_track_order, _start, label, (pos_x, pos_y)) in enumerate(video_layer_labels):
        out_label = f"comp{i}"
        x_expr = f"(main_w*{pos_x:.4f})-(w/2)"
        y_expr = f"(main_h*{pos_y:.4f})-(h/2)"
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
            audio_labels.append(_build_audio_chain(idx, clip, filter_lines, clip_counter))
            clip_counter += 1

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
