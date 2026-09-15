"""Track model: an ordered lane of clips on the timeline."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from core.clip import Clip


class TrackType(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"


@dataclass
class Track:
    """A single track (video or audio) holding a list of non-overlapping clips."""

    track_type: TrackType
    name: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    clips: List[Clip] = field(default_factory=list)
    muted: bool = False
    locked: bool = False
    hidden: bool = False
    height: int = 80  # UI row height in pixels

    def __post_init__(self) -> None:
        if not self.name:
            self.name = "Video Track" if self.track_type == TrackType.VIDEO else "Audio Track"

    @property
    def duration(self) -> float:
        if not self.clips:
            return 0.0
        return max(c.timeline_end for c in self.clips)

    def sorted_clips(self) -> List[Clip]:
        return sorted(self.clips, key=lambda c: c.timeline_start)

    def clip_at(self, t: float) -> Optional[Clip]:
        for c in self.clips:
            if c.contains_time(t):
                return c
        return None

    def find_clip(self, clip_id: str) -> Optional[Clip]:
        for c in self.clips:
            if c.id == clip_id:
                return c
        return None

    def has_overlap(self, clip: Clip, ignore_id: Optional[str] = None) -> bool:
        for other in self.clips:
            if other.id == (ignore_id or clip.id):
                continue
            if clip.timeline_start < other.timeline_end and other.timeline_start < clip.timeline_end:
                return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "track_type": self.track_type.value,
            "muted": self.muted,
            "locked": self.locked,
            "hidden": self.hidden,
            "height": self.height,
            "clips": [c.to_dict() for c in self.clips],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Track":
        track = cls(
            track_type=TrackType(data["track_type"]),
            name=data.get("name", ""),
            id=data.get("id", uuid.uuid4().hex[:12]),
            muted=data.get("muted", False),
            locked=data.get("locked", False),
            hidden=data.get("hidden", False),
            height=data.get("height", 80),
        )
        track.clips = [Clip.from_dict(c) for c in data.get("clips", [])]
        return track
