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

from dataclasses import dataclass
from typing import Dict, List, Tuple

from core.clip import Clip, MediaType
from core.project import Project, ProjectSettings
from core.track import TrackType

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff", ".gif"}


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


def build_filter_graph(project: Project, settings: ProjectSettings) -> GraphResult:
    """Construct the full FFmpeg input list + filter graph for ``project``."""

    duration = max(project.total_duration, 0.1)
    width, height, fps = settings.width, settings.height, settings.fps

    input_specs: List[InputSpec] = []
    filter_lines: List[str] = []

    video_layer_labels: List[Tuple[int, float, str]] = []  # (track_order, start, label)
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
        for clip in track.sorted_clips():
            if clip.media_type == MediaType.TEXT:
                text_clips.append(clip)
                continue
            if clip.media_type not in (MediaType.VIDEO, MediaType.IMAGE):
                continue

            idx = len(input_specs)
            is_image = clip.media_type == MediaType.IMAGE

            if is_image:
                pre_args = ["-loop", "1", "-framerate", str(fps), "-t", f"{clip.source_length:.3f}"]
                input_specs.append(InputSpec(pre_args=pre_args, path=clip.source_path))
                vsrc = f"[{idx}:v]"
                chain = f"{vsrc}setpts=PTS-STARTPTS"
            else:
                pre_args = ["-ss", f"{max(0.0, clip.source_in):.3f}"]
                input_specs.append(InputSpec(pre_args=pre_args, path=clip.source_path))
                vsrc = f"[{idx}:v]"
                chain = (
                    f"{vsrc}trim=duration={clip.source_length:.3f},setpts=PTS-STARTPTS"
                )

            speed = clip.speed if clip.speed > 0 else 1.0
            if abs(speed - 1.0) > 1e-6:
                chain += f",setpts=PTS/{speed:.6f}"

            chain += (
                f",scale={width}:{height}:force_original_aspect_ratio=decrease"
                f",pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
                ",format=yuva420p"
            )

            clip_len = clip.duration
            if clip.fade_in > 0:
                chain += f",fade=t=in:st=0:d={clip.fade_in:.3f}:alpha=1"
            if clip.fade_out > 0:
                fade_start = max(0.0, clip_len - clip.fade_out)
                chain += f",fade=t=out:st={fade_start:.3f}:d={clip.fade_out:.3f}:alpha=1"

            if clip.opacity < 1.0:
                chain += f",colorchannelmixer=aa={clip.opacity:.3f}"

            if clip.timeline_start > 0:
                chain += f",tpad=start_duration={clip.timeline_start:.3f}:color=black@0.0"

            label = f"v{clip_counter}"
            filter_lines.append(f"{chain}[{label}]")
            video_layer_labels.append((track_order, clip.timeline_start, label))

            clip_has_audio = not is_image and _clip_has_audio(project, clip, probe_cache)
            if clip_has_audio and not clip.muted and clip.volume > 0:
                audio_labels.append(_build_audio_chain(idx, clip, filter_lines, clip_counter))

            clip_counter += 1

    # ---- Composite video layers onto the base canvas, bottom to top ----
    current = "base"
    for i, (_track_order, _start, label) in enumerate(video_layer_labels):
        out_label = f"comp{i}"
        filter_lines.append(
            f"[{current}][{label}]overlay=0:0:shortest=0:eof_action=pass[{out_label}]"
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

    if abs(clip.volume - 1.0) > 1e-6:
        chain += f",volume={clip.volume:.3f}"

    if clip.timeline_start > 0:
        delay_ms = int(clip.timeline_start * 1000)
        chain += f",adelay={delay_ms}|{delay_ms}"

    filter_lines.append(f"{chain}[{label}]")
    return label
