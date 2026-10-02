"""Data models for clips placed on the timeline.

A :class:`Clip` represents one piece of media (or generated text) placed on a
:class:`~core.track.Track`. All time values are expressed in seconds
(``float``) for precision; the UI layer converts to pixels for display.
"""

from __future__ import annotations

import uuid
from dataclasses import InitVar, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from core.effects import (
    LEGACY_FIELDS,
    LEGACY_KINDS,
    EffectInstance,
    keyframe_times,
    legacy_default,
    legacy_effect_dicts,
    legacy_insert_index,
)


class MediaType(str, Enum):
    """The kind of media a clip represents."""

    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    TEXT = "text"


class TransitionType(str, Enum):
    """Supported transitions applied at the head/tail of a clip.

    Every member except ``NONE`` (and the legacy ``FADE``, which was never
    actually wired into the crossfade path) is applied between adjacent
    clips via FFmpeg's ``xfade`` filter — see
    ``core/filter_graph.py::_XFADE_TRANSITION_NAMES`` for the mapping from
    each member to the actual ``xfade`` ``transition=`` name, and
    ``_find_crossfade_partner`` for how a clip's ``transition_out`` decides
    whether it pairs with the next clip on its track. ``CROSSFADE`` is kept
    as the original member (mapping to ``xfade``'s ``fade`` transition) for
    backward compatibility with already-saved ``.vproj`` files.
    """

    NONE = "none"
    FADE = "fade"
    CROSSFADE = "crossfade"
    WIPE_LEFT = "wipeleft"
    WIPE_RIGHT = "wiperight"
    WIPE_UP = "wipeup"
    WIPE_DOWN = "wipedown"
    SLIDE_LEFT = "slideleft"
    SLIDE_RIGHT = "slideright"
    SLIDE_UP = "slideup"
    SLIDE_DOWN = "slidedown"
    CIRCLE_CROP = "circlecrop"
    DISSOLVE = "dissolve"


class ReframeMode(str, Enum):
    """How a clip's frame is fit into a canvas of a different aspect ratio."""

    #: Scale to fit entirely inside the canvas, letterboxing with black —
    #: the long-standing default behavior.
    FIT = "fit"
    #: Scale to fully cover the canvas and crop the excess, centered on
    #: (crop_focus_x, crop_focus_y) — for reframing e.g. 16:9 source into a
    #: 9:16 vertical export without black bars.
    FILL = "fill"


@dataclass
class TextStyle:
    """Styling options for a text overlay clip."""

    content: str = "Sample Text"
    font_family: str = "Arial"
    font_size: int = 48
    color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_width: int = 2
    bold: bool = False
    italic: bool = False
    position_x: float = 0.5  # relative 0..1 (0 = left, 1 = right)
    position_y: float = 0.9  # relative 0..1 (0 = top, 1 = bottom)
    alignment: str = "center"  # left | center | right (line alignment within a multi-line block)
    # Drop shadow and background box (drawtext shadowx/shadowy/shadowcolor
    # and box/boxcolor/boxborderw). Off by default, so existing projects
    # render exactly as before. Sizes/offsets are, like font_size, pixels at
    # 1080p output height and scale with the export resolution.
    shadow_enabled: bool = False
    shadow_color: str = "#000000"
    shadow_offset: int = 3
    box_enabled: bool = False
    box_color: str = "#000000"
    box_opacity: float = 0.5  # 0..1
    box_padding: int = 12

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "font_family": self.font_family,
            "font_size": self.font_size,
            "color": self.color,
            "outline_color": self.outline_color,
            "outline_width": self.outline_width,
            "bold": self.bold,
            "italic": self.italic,
            "position_x": self.position_x,
            "position_y": self.position_y,
            "alignment": self.alignment,
            "shadow_enabled": self.shadow_enabled,
            "shadow_color": self.shadow_color,
            "shadow_offset": self.shadow_offset,
            "box_enabled": self.box_enabled,
            "box_color": self.box_color,
            "box_opacity": self.box_opacity,
            "box_padding": self.box_padding,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TextStyle":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def interpolate_keyframes(keyframes, t: float) -> Tuple[float, ...]:
    """Value(s) at ``t`` under the export's piecewise-linear, hold-at-ends semantics."""
    kfs = sorted(keyframes, key=lambda k: k[0])
    if t <= kfs[0][0]:
        return tuple(kfs[0][1:])
    for (t0, *v0), (t1, *v1) in zip(kfs, kfs[1:]):
        if t < t1:
            frac = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
            return tuple(a + (b - a) * frac for a, b in zip(v0, v1))
    return tuple(kfs[-1][1:])


def _step_value(keyframes, t: float) -> Tuple[float, ...]:
    """Step-function value in effect at ``t`` (speed keyframes, see Clip.speed_segments)."""
    kfs = sorted(keyframes, key=lambda k: k[0])
    value = tuple(kfs[0][1:])
    for k in kfs:
        if k[0] <= t + 1e-9:
            value = tuple(k[1:])
    return value


def _rebase_keyframes(keyframes, shift: float, step: bool = False) -> list:
    """Keyframes re-based so time ``shift`` becomes 0 (fewer than 2 = not animated: unchanged).

    A positive shift (content cut from the front) drops the keyframes before
    it and adds one at 0 holding the value in effect there; a negative shift
    (front extended) just moves every keyframe later.
    """
    if len(keyframes) < 2:
        return list(keyframes)
    if shift <= 0:
        return [(k[0] - shift, *k[1:]) for k in sorted(keyframes, key=lambda k: k[0])]
    value = _step_value(keyframes, shift) if step else interpolate_keyframes(keyframes, shift)
    kept = [(k[0] - shift, *k[1:]) for k in sorted(keyframes, key=lambda k: k[0]) if k[0] > shift + 1e-6]
    rebased = [(0.0, *value)] + kept
    if len(rebased) < 2:  # the cut is past the last keyframe: hold its value
        rebased.append((max(1e-3, 1.0), *value))
    return rebased


@dataclass
class Clip:
    """A single clip instance placed on a track.

    ``source_in``/``source_out`` describe the trimmed region of the source
    media (in the source's own time base, seconds). ``timeline_start`` is
    where the clip begins on the shared project timeline, also in seconds.
    """

    media_type: MediaType
    source_path: str = ""
    source_in: float = 0.0
    source_out: float = 5.0
    timeline_start: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""

    # Audio / playback adjustments
    volume: float = 1.0
    muted: bool = False
    speed: float = 1.0

    # Audio noise reduction, via FFmpeg's `afftdn` filter. Disabled (False)
    # by default so an unedited clip never emits the filter, matching the
    # neutral-default convention used by Color Correction/Chroma Key above.
    # denoise_strength maps directly to afftdn's `nr` (noise reduction)
    # option, in decibels, 0.01..97 per FFmpeg's own accepted range.
    denoise_enabled: bool = False
    denoise_strength: float = 12.0

    # Visual adjustments
    opacity: float = 1.0

    # Transitions (duration in seconds, applied within the clip's own span)
    fade_in: float = 0.0
    fade_out: float = 0.0
    transition_out: TransitionType = TransitionType.NONE
    # Crossfade duration used when transition_out == CROSSFADE, in seconds.
    # Independent of fade_out (which remains a simple fade to transparent).
    transition_duration: float = 0.5

    # Transform (video/image clips only). The clip is first scaled to fit
    # the canvas preserving aspect ratio (as always), then this additional
    # scale/rotation is applied, and the result is positioned by its center
    # at (transform_x, transform_y) — relative 0..1 of the canvas, same
    # convention as TextStyle.position_x/y. Defaults reproduce the old,
    # always-centered-and-padded-to-canvas behavior exactly.
    transform_x: float = 0.5  # 0 = left edge, 1 = right edge
    transform_y: float = 0.5  # 0 = top edge, 1 = bottom edge
    transform_scale: float = 1.0  # multiplier on top of the fit-to-canvas size
    transform_rotation: float = 0.0  # degrees, clockwise

    # Keyframes (video/image clips only). Empty or single-entry = constant
    # value from the scalar field above (``opacity`` / ``transform_x``,
    # ``transform_y``). 2+ entries enable smooth piecewise-linear animation
    # between them (holding the first/last value outside the keyframed
    # range). Times are seconds from the clip's own start on the timeline.
    opacity_keyframes: List[Tuple[float, float]] = field(default_factory=list)  # (time, opacity 0..1)
    position_keyframes: List[Tuple[float, float, float]] = field(default_factory=list)  # (time, x, y)

    # Speed ramp. Empty or single-entry = constant ``speed`` above. 2+
    # entries define speed *segments*, not a smooth continuous ramp: each
    # keyframe (source_time, speed) sets the speed from that point in the
    # SOURCE (source_in-relative) forward, until the next keyframe — a step
    # change at each boundary. Affects Clip.duration (below) regardless of
    # media type, but core/filter_graph.py only implements the actual
    # segmented render for video/image clips — a standalone AUDIO-track
    # clip's speed_keyframes are (for now) read for timing purposes only
    # and don't yet get a segmented render of their own. See
    # docs/workflows/speed-ramps/workflow.md for both of these scope calls.
    speed_keyframes: List[Tuple[float, float]] = field(default_factory=list)  # (source_time, speed)

    # Reframe (video/image clips only) — how this clip fits a canvas whose
    # aspect ratio differs from its own, e.g. cropping 16:9 source to fill a
    # 9:16 vertical export instead of letterboxing it. crop_focus_x/y (0..1)
    # only matter in FILL mode: which part of the now-overflowing frame to
    # keep, same 0=left/top, 1=right/bottom, 0.5=center convention as
    # Transform's position. FILL mode supersedes Transform's scale/rotation
    # /position entirely for this clip (the frame already exactly fills the
    # canvas after cropping, so there's nothing left for Transform to do) —
    # see docs/workflows/shorts-export/workflow.md.
    reframe_mode: ReframeMode = ReframeMode.FIT
    crop_focus_x: float = 0.5
    crop_focus_y: float = 0.5

    # Text overlay data (only used when media_type == TEXT)
    text_style: Optional[TextStyle] = None

    # Original untrimmed source duration, when known (seconds)
    source_duration: Optional[float] = None

    # The media-pool asset this clip was made from (None for text clips and
    # for clips from projects saved before asset ids existed, which are then
    # matched by path). Relinking an asset rewrites every clip linked to it.
    asset_id: Optional[str] = None

    # Clips sharing a link_group move together (e.g. a video clip and the
    # audio clip detached from it, see core.commands.build_detach_audio_command).
    link_group: Optional[str] = None

    # Volume automation: (clip-local timeline seconds, gain 0..2). 2+ entries
    # override the scalar ``volume`` with a piecewise-linear envelope, exactly
    # like opacity_keyframes override ``opacity``.
    volume_keyframes: List[Tuple[float, float]] = field(default_factory=list)

    # Generic effect stack (colour correction, keys, LUT, sharpen, blur,
    # vignette, denoise, stabilization), applied in list order -- see
    # core/effects.py. Effect parameters can carry their own keyframes.
    effects: List[EffectInstance] = field(default_factory=list)

    # Pre-format-3 colour correction / RGB chroma key fields. They are no
    # longer stored on the clip: given to the constructor they become
    # ``color_correction``/``colorkey`` effects, and reading or assigning
    # ``clip.brightness`` (etc.) reads or edits the first such effect -- see
    # the properties installed after the class. None = not given.
    brightness: InitVar[Optional[float]] = None
    contrast: InitVar[Optional[float]] = None
    saturation: InitVar[Optional[float]] = None
    gamma: InitVar[Optional[float]] = None
    chroma_key_enabled: InitVar[Optional[bool]] = None
    chroma_key_color: InitVar[Optional[str]] = None
    chroma_key_similarity: InitVar[Optional[float]] = None
    chroma_key_blend: InitVar[Optional[float]] = None

    def __post_init__(self, brightness, contrast, saturation, gamma, chroma_key_enabled, chroma_key_color,
                      chroma_key_similarity, chroma_key_blend) -> None:
        if not self.name:
            self.name = self._default_name()
        if self.media_type == MediaType.TEXT and self.text_style is None:
            self.text_style = TextStyle()
        given = {
            "brightness": brightness, "contrast": contrast, "saturation": saturation, "gamma": gamma,
            "chroma_key_enabled": chroma_key_enabled, "chroma_key_color": chroma_key_color,
            "chroma_key_similarity": chroma_key_similarity, "chroma_key_blend": chroma_key_blend,
        }
        legacy = {name: value for name, value in given.items() if value is not None}
        for data in legacy_effect_dicts(legacy) if legacy else []:
            effect = EffectInstance.from_dict(data)
            self.effects.insert(legacy_insert_index([e.kind for e in self.effects], effect.kind), effect)

    def legacy_effect(self, kind: str) -> Optional[EffectInstance]:
        """The first ``kind`` effect in the stack (what the Color Correction / Chroma Key groups edit)."""
        return next((e for e in self.effects if e.kind == kind), None)

    def _default_name(self) -> str:
        if self.source_path:
            import os

            return os.path.basename(self.source_path)
        if self.media_type == MediaType.TEXT:
            return "Text"
        return "Clip"

    @property
    def source_length(self) -> float:
        """Length of the trimmed region in source time (seconds)."""
        return max(0.0, self.source_out - self.source_in)

    @property
    def speed_segments(self) -> List[Tuple[float, float, float]]:
        """``(source_in, source_out, speed)`` triples covering the trimmed region.

        A single segment at the constant ``speed`` when there are fewer
        than 2 speed keyframes (the common case, and exactly what
        ``source_length``/``speed`` already implied before speed ramps
        existed). 2+ keyframes split the region into the step-function
        segments ``speed_keyframes`` describes: each keyframe sets the
        speed from its (source-in-relative) time forward until the next
        one. Extends the first/last keyframe's speed to cover the region's
        actual edges when the keyframes don't already start/end exactly there.
        """
        span = self.source_length
        if len(self.speed_keyframes) < 2:
            return [(self.source_in, self.source_out, self.speed if self.speed > 0 else 1.0)]

        boundaries: List[float] = []
        speeds: List[float] = []
        for t, s in sorted(self.speed_keyframes, key=lambda k: k[0]):
            clamped_t = self.source_in + min(max(0.0, t), span)
            s = s if s > 0 else 1.0
            if boundaries and abs(boundaries[-1] - clamped_t) < 1e-6:
                speeds[-1] = s  # same instant re-specified: last one wins
            else:
                boundaries.append(clamped_t)
                speeds.append(s)

        segments: List[Tuple[float, float, float]] = []
        if boundaries[0] > self.source_in + 1e-6:
            segments.append((self.source_in, boundaries[0], speeds[0]))
        for i, start in enumerate(boundaries):
            end = boundaries[i + 1] if i + 1 < len(boundaries) else self.source_out
            if end - start > 1e-6:
                segments.append((start, end, speeds[i]))
        return segments or [(self.source_in, self.source_out, 1.0)]

    @property
    def duration(self) -> float:
        """Length of the clip as it appears on the timeline (seconds)."""
        if len(self.speed_keyframes) >= 2:
            return sum((seg_out - seg_in) / seg_speed for seg_in, seg_out, seg_speed in self.speed_segments)
        speed = self.speed if self.speed > 0 else 1.0
        return self.source_length / speed

    def source_offset_at(self, timeline_offset: float) -> float:
        """Source seconds (from ``source_in``) on screen ``timeline_offset`` seconds into the clip.

        Walks :attr:`speed_segments`, so it is right for speed-ramped clips
        too (``offset * speed`` only holds at a constant speed). Past the
        clip's end it extrapolates at the last segment's speed -- what
        extending the clip's right edge would show.
        """
        if len(self.speed_keyframes) < 2:
            return timeline_offset * (self.speed if self.speed > 0 else 1.0)
        elapsed = 0.0
        segments = self.speed_segments
        for seg_in, seg_out, seg_speed in segments:
            seg_len = (seg_out - seg_in) / seg_speed
            if timeline_offset <= elapsed + seg_len:
                return (seg_in - self.source_in) + (timeline_offset - elapsed) * seg_speed
            elapsed += seg_len
        last_in, last_out, last_speed = segments[-1]
        return (last_out - self.source_in) + (timeline_offset - elapsed) * last_speed

    def timeline_offset_at(self, source_offset: float) -> float:
        """Inverse of :meth:`source_offset_at`: clip-local timeline seconds showing ``source_offset``."""
        if len(self.speed_keyframes) < 2:
            return source_offset / (self.speed if self.speed > 0 else 1.0)
        elapsed = 0.0
        segments = self.speed_segments
        if source_offset <= 0:
            return source_offset / segments[0][2]  # before the clip: first segment's speed
        for seg_in, seg_out, seg_speed in segments:
            rel_in, rel_out = seg_in - self.source_in, seg_out - self.source_in
            if source_offset <= rel_out:
                return elapsed + max(0.0, source_offset - rel_in) / seg_speed
            elapsed += (seg_out - seg_in) / seg_speed
        last_in, last_out, last_speed = segments[-1]
        return elapsed + (source_offset - (last_out - self.source_in)) / last_speed

    def speed_at(self, timeline_offset: float) -> float:
        """Playback speed in effect ``timeline_offset`` seconds into the clip."""
        if len(self.speed_keyframes) < 2:
            return self.speed if self.speed > 0 else 1.0
        elapsed = 0.0
        for seg_in, seg_out, seg_speed in self.speed_segments:
            elapsed += (seg_out - seg_in) / seg_speed
            if timeline_offset < elapsed:
                return seg_speed
        return self.speed_segments[-1][2]

    def with_source_range(self, source_in: float, source_out: float) -> "Clip":
        """A shallow copy with a different trim: what the clip becomes after trimming.

        Used wherever a trim's result is needed (the trim command itself,
        ripple trim, the timeline's trim drag), so all three agree:
        recomputing the length as ``(out - in) / speed`` ignored speed ramps
        and let a ripple trim overlap the next clip.

        Moving ``source_in`` re-bases the keyframes so they stay on the same
        content: speed keyframes are ``source_in``-relative source seconds,
        opacity/position/volume keyframes clip-relative timeline seconds. A
        left trim used to leave them where they were, so a ramp or an
        animation slid along the footage by the trimmed amount.
        """
        import copy as _copy

        probe = _copy.copy(self)
        probe.source_in, probe.source_out = source_in, source_out
        source_shift = source_in - self.source_in
        if abs(source_shift) > 1e-9:
            timeline_shift = self.timeline_offset_at(source_shift)
            probe.speed_keyframes = _rebase_keyframes(self.speed_keyframes, source_shift, step=True)
            probe.opacity_keyframes = _rebase_keyframes(self.opacity_keyframes, timeline_shift)
            probe.position_keyframes = _rebase_keyframes(self.position_keyframes, timeline_shift)
            probe.volume_keyframes = _rebase_keyframes(self.volume_keyframes, timeline_shift)
            probe.effects = [
                e.with_keyframes_mapped(lambda keys: _rebase_keyframes(keys, timeline_shift)) if e.keyframes else e
                for e in self.effects
            ]
        return probe

    @property
    def timeline_end(self) -> float:
        return self.timeline_start + self.duration

    # -- "is this effect active" checks ---------------------------------
    # These mirror `core/filter_graph.py`'s own per-field "should I emit
    # this filter" checks exactly (same field tuples/comparisons), so the
    # export graph and any other consumer (e.g. the timeline's per-clip
    # effect-indicator badges, `ui/widgets/timeline_items.py::ClipItem`)
    # never drift apart on what counts as "non-default."
    @property
    def has_color_correction(self) -> bool:
        """Whether a colour-correction effect changes the picture (non-neutral or animated)."""
        return any(e.kind == "color_correction" and e.is_renderable() for e in self.effects)

    @property
    def has_chroma_key(self) -> bool:
        return any(e.kind == "colorkey" and e.is_renderable() for e in self.effects)

    @property
    def has_denoise(self) -> bool:
        return self.denoise_enabled

    @property
    def has_transition(self) -> bool:
        return self.transition_out != TransitionType.NONE

    @property
    def has_effects(self) -> bool:
        """Any renderable effect besides colour correction / RGB key (which have their own badges)."""
        return any(e.is_renderable() and e.kind not in LEGACY_KINDS for e in self.effects)

    @property
    def has_stabilization(self) -> bool:
        return any(e.kind == "stabilize" and e.is_renderable() for e in self.effects)

    def effect_keyframe_times(self) -> List[float]:
        """Clip-local times of every animated effect parameter's keyframes."""
        return keyframe_times(self.effects)

    @property
    def has_volume_automation(self) -> bool:
        return len(self.volume_keyframes) >= 2

    @property
    def is_silent(self) -> bool:
        """Muted, or at zero volume with no volume automation to bring it back up."""
        return self.muted or (self.volume <= 0 and not self.has_volume_automation)

    def contains_time(self, t: float) -> bool:
        return self.timeline_start <= t < self.timeline_end

    def copy(self) -> "Clip":
        import copy as _copy

        return _copy.deepcopy(self)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "media_type": self.media_type.value,
            "source_path": self.source_path,
            "source_in": self.source_in,
            "source_out": self.source_out,
            "timeline_start": self.timeline_start,
            "volume": self.volume,
            "muted": self.muted,
            "speed": self.speed,
            "denoise_enabled": self.denoise_enabled,
            "denoise_strength": self.denoise_strength,
            "opacity": self.opacity,
            "fade_in": self.fade_in,
            "fade_out": self.fade_out,
            "transition_out": self.transition_out.value,
            "transition_duration": self.transition_duration,
            "transform_x": self.transform_x,
            "transform_y": self.transform_y,
            "transform_scale": self.transform_scale,
            "transform_rotation": self.transform_rotation,
            "opacity_keyframes": [list(kf) for kf in self.opacity_keyframes],
            "position_keyframes": [list(kf) for kf in self.position_keyframes],
            "speed_keyframes": [list(kf) for kf in self.speed_keyframes],
            "reframe_mode": self.reframe_mode.value,
            "crop_focus_x": self.crop_focus_x,
            "crop_focus_y": self.crop_focus_y,
            "text_style": self.text_style.to_dict() if self.text_style else None,
            "source_duration": self.source_duration,
            "asset_id": self.asset_id,
            "link_group": self.link_group,
            "volume_keyframes": [list(kf) for kf in self.volume_keyframes],
            "effects": [e.to_dict() for e in self.effects],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], warnings: Optional[List[str]] = None) -> "Clip":
        """Build a clip from saved data, tolerating values this version doesn't know.

        An unknown ``transition_out``/``reframe_mode`` (e.g. a transition
        added by a newer version) falls back to a safe default and is
        reported in ``warnings`` rather than failing the whole load. An
        unknown ``media_type`` still raises ``ValueError`` -- there is no
        safe stand-in for it; :meth:`core.track.Track.from_dict` skips such
        a clip with a warning.
        """
        text_style = data.get("text_style")
        name = data.get("name", "")

        def enum(enum_cls, key, default):
            raw = data.get(key, default.value)
            try:
                return enum_cls(raw)
            except ValueError:
                if warnings is not None:
                    warnings.append(f"Clip '{name or data.get('id', '?')}': unknown {key} '{raw}', using '{default.value}'.")
                return default

        return cls(
            media_type=MediaType(data["media_type"]),
            source_path=data.get("source_path", ""),
            source_in=data.get("source_in", 0.0),
            source_out=data.get("source_out", 0.0),
            timeline_start=data.get("timeline_start", 0.0),
            id=data.get("id", uuid.uuid4().hex[:12]),
            name=name,
            volume=data.get("volume", 1.0),
            muted=data.get("muted", False),
            speed=data.get("speed", 1.0),
            denoise_enabled=data.get("denoise_enabled", False),
            denoise_strength=data.get("denoise_strength", 12.0),
            opacity=data.get("opacity", 1.0),
            fade_in=data.get("fade_in", 0.0),
            fade_out=data.get("fade_out", 0.0),
            transition_out=enum(TransitionType, "transition_out", TransitionType.NONE),
            transition_duration=data.get("transition_duration", 0.5),
            transform_x=data.get("transform_x", 0.5),
            transform_y=data.get("transform_y", 0.5),
            transform_scale=data.get("transform_scale", 1.0),
            transform_rotation=data.get("transform_rotation", 0.0),
            opacity_keyframes=[tuple(kf) for kf in data.get("opacity_keyframes", [])],
            position_keyframes=[tuple(kf) for kf in data.get("position_keyframes", [])],
            speed_keyframes=[tuple(kf) for kf in data.get("speed_keyframes", [])],
            reframe_mode=enum(ReframeMode, "reframe_mode", ReframeMode.FIT),
            crop_focus_x=data.get("crop_focus_x", 0.5),
            crop_focus_y=data.get("crop_focus_y", 0.5),
            text_style=TextStyle.from_dict(text_style) if text_style else None,
            source_duration=data.get("source_duration"),
            asset_id=data.get("asset_id"),
            link_group=data.get("link_group"),
            volume_keyframes=[tuple(kf) for kf in data.get("volume_keyframes", [])],
            effects=[EffectInstance.from_dict(e) for e in data.get("effects", []) if isinstance(e, dict)],
            # Old-style colour/key keys (pre-format-3 data that skipped the
            # project migration, e.g. a hand-made dict) still become effects.
            **{name: data[name] for name in LEGACY_FIELDS if name in data},
        )


def _legacy_property(name: str) -> property:
    """``Clip.<old field>`` read/written through the first matching effect (see Clip.__post_init__)."""
    kind, param = LEGACY_FIELDS[name]

    def getter(self: Clip):
        effect = self.legacy_effect(kind)
        if effect is None:
            return legacy_default(name)
        if param is None:
            return effect.enabled
        return effect.resolved_params()[param]

    def setter(self: Clip, value) -> None:
        effect = self.legacy_effect(kind)
        if effect is None:
            if value == legacy_default(name):
                return  # nothing to record: no effect already means "neutral" / "off"
            effect = EffectInstance.create(kind)
            # Picking a key colour before ticking "Enable" must not start keying.
            effect.enabled = kind != "colorkey"
            self.effects.insert(legacy_insert_index([e.kind for e in self.effects], kind), effect)
        if param is None:
            effect.enabled = bool(value)
        else:
            effect.params[param] = value

    return property(getter, setter, doc=f"Compatibility view of the {kind} effect ({param or 'enabled'}).")


for _name in LEGACY_FIELDS:
    setattr(Clip, _name, _legacy_property(_name))
del _name
