"""The video side of the export filter graph (R9: split out of core/filter_graph.py, which
re-exports every name): source colour/HDR handling, fit/fill and aspect, stabilisation, input
slicing, each clip's video chain with its fades and opacity, speed ramps and positions."""

from __future__ import annotations

import math
from dataclasses import dataclass
import re
from typing import List, Optional, Tuple

from core.clip import Clip, MediaType, ReframeMode
from core.effects import ANALYSIS_FILE_PARAM, effect_filters
from core.media_asset import MediaAsset
from core.output_color import REFERENCE_WHITE_NITS, SDR_8BIT, OutputColor
from core.stabilize import make_job
from core.filter_graph_base import _GraphContext, _piecewise_linear_expr, InputSpec, INPUT_DURATION_MARGIN, _Window, _timeline_shift


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
    pre = effect_filters(clip.effects, "pre", t_offset, aspect=_source_aspect(asset, width, height), easing=clip.easing_for("effects"))
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
    return effect_filters(clip.effects, "post", t_offset, aspect=aspect, easing=clip.easing_for("effects"))


def _post_aspect(clip: Clip, width: int, height: int, pad: bool, asset: Optional[MediaAsset]) -> float:
    """The frame's aspect after _fit_or_fill: the canvas when filled or padded, else the source's."""
    if clip.reframe_mode == ReframeMode.FILL or pad:
        return width / height if height else 16 / 9
    return _source_aspect(asset, width, height)


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
    easing = clip.easing_for("position")
    return _piecewise_linear_expr(x_keyframes, t_local, easing), _piecewise_linear_expr(y_keyframes, t_local, easing)


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
        opacity_expr = _piecewise_linear_expr(clip.opacity_keyframes, "T", clip.easing_for("opacity"))
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
