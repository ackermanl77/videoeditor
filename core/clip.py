"""Data models for clips placed on the timeline.

A :class:`Clip` represents one piece of media (or generated text) placed on a
:class:`~core.track.Track`. All time values are expressed in seconds
(``float``) for precision; the UI layer converts to pixels for display.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class MediaType(str, Enum):
    """The kind of media a clip represents."""

    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    TEXT = "text"


class TransitionType(str, Enum):
    """Supported transitions applied at the head/tail of a clip."""

    NONE = "none"
    FADE = "fade"
    CROSSFADE = "crossfade"


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
    alignment: str = "center"  # left | center | right

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
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TextStyle":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


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
    track_index: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""

    # Audio / playback adjustments
    volume: float = 1.0
    muted: bool = False
    speed: float = 1.0

    # Visual adjustments
    opacity: float = 1.0

    # Transitions (duration in seconds, applied within the clip's own span)
    fade_in: float = 0.0
    fade_out: float = 0.0
    transition_in: TransitionType = TransitionType.NONE
    transition_out: TransitionType = TransitionType.NONE
    # Crossfade duration used when transition_out == CROSSFADE, in seconds.
    # Independent of fade_out (which remains a simple fade to transparent).
    transition_duration: float = 0.5

    # Color correction (video/image clips only). Neutral values match
    # FFmpeg's ``eq`` filter defaults, so an unedited clip never emits it.
    brightness: float = 0.0  # -1.0 .. 1.0
    contrast: float = 1.0  # 0.0 .. 2.0
    saturation: float = 1.0  # 0.0 .. 3.0
    gamma: float = 1.0  # 0.1 .. 3.0

    # Chroma key (video/image clips only), via FFmpeg's ``colorkey`` filter.
    chroma_key_enabled: bool = False
    chroma_key_color: str = "#00FF00"
    chroma_key_similarity: float = 0.2  # 0.0 .. 1.0
    chroma_key_blend: float = 0.1  # 0.0 .. 1.0

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

    # Text overlay data (only used when media_type == TEXT)
    text_style: Optional[TextStyle] = None

    # Original untrimmed source duration, when known (seconds)
    source_duration: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self._default_name()
        if self.media_type == MediaType.TEXT and self.text_style is None:
            self.text_style = TextStyle()

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

    @property
    def timeline_end(self) -> float:
        return self.timeline_start + self.duration

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
            "track_index": self.track_index,
            "volume": self.volume,
            "muted": self.muted,
            "speed": self.speed,
            "opacity": self.opacity,
            "fade_in": self.fade_in,
            "fade_out": self.fade_out,
            "transition_in": self.transition_in.value,
            "transition_out": self.transition_out.value,
            "transition_duration": self.transition_duration,
            "brightness": self.brightness,
            "contrast": self.contrast,
            "saturation": self.saturation,
            "gamma": self.gamma,
            "chroma_key_enabled": self.chroma_key_enabled,
            "chroma_key_color": self.chroma_key_color,
            "chroma_key_similarity": self.chroma_key_similarity,
            "chroma_key_blend": self.chroma_key_blend,
            "transform_x": self.transform_x,
            "transform_y": self.transform_y,
            "transform_scale": self.transform_scale,
            "transform_rotation": self.transform_rotation,
            "opacity_keyframes": [list(kf) for kf in self.opacity_keyframes],
            "position_keyframes": [list(kf) for kf in self.position_keyframes],
            "speed_keyframes": [list(kf) for kf in self.speed_keyframes],
            "text_style": self.text_style.to_dict() if self.text_style else None,
            "source_duration": self.source_duration,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Clip":
        text_style = data.get("text_style")
        return cls(
            media_type=MediaType(data["media_type"]),
            source_path=data.get("source_path", ""),
            source_in=data.get("source_in", 0.0),
            source_out=data.get("source_out", 0.0),
            timeline_start=data.get("timeline_start", 0.0),
            track_index=data.get("track_index", 0),
            id=data.get("id", uuid.uuid4().hex[:12]),
            name=data.get("name", ""),
            volume=data.get("volume", 1.0),
            muted=data.get("muted", False),
            speed=data.get("speed", 1.0),
            opacity=data.get("opacity", 1.0),
            fade_in=data.get("fade_in", 0.0),
            fade_out=data.get("fade_out", 0.0),
            transition_in=TransitionType(data.get("transition_in", "none")),
            transition_out=TransitionType(data.get("transition_out", "none")),
            transition_duration=data.get("transition_duration", 0.5),
            brightness=data.get("brightness", 0.0),
            contrast=data.get("contrast", 1.0),
            saturation=data.get("saturation", 1.0),
            gamma=data.get("gamma", 1.0),
            chroma_key_enabled=data.get("chroma_key_enabled", False),
            chroma_key_color=data.get("chroma_key_color", "#00FF00"),
            chroma_key_similarity=data.get("chroma_key_similarity", 0.2),
            chroma_key_blend=data.get("chroma_key_blend", 0.1),
            transform_x=data.get("transform_x", 0.5),
            transform_y=data.get("transform_y", 0.5),
            transform_scale=data.get("transform_scale", 1.0),
            transform_rotation=data.get("transform_rotation", 0.0),
            opacity_keyframes=[tuple(kf) for kf in data.get("opacity_keyframes", [])],
            position_keyframes=[tuple(kf) for kf in data.get("position_keyframes", [])],
            speed_keyframes=[tuple(kf) for kf in data.get("speed_keyframes", [])],
            text_style=TextStyle.from_dict(text_style) if text_style else None,
            source_duration=data.get("source_duration"),
        )
