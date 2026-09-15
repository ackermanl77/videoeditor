"""Data models for clips placed on the timeline.

A :class:`Clip` represents one piece of media (or generated text) placed on a
:class:`~core.track.Track`. All time values are expressed in seconds
(``float``) for precision; the UI layer converts to pixels for display.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


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
    def duration(self) -> float:
        """Length of the clip as it appears on the timeline (seconds)."""
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
            text_style=TextStyle.from_dict(text_style) if text_style else None,
            source_duration=data.get("source_duration"),
        )
