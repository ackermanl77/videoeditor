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


#: Timeline row heights (pixels) offered by a track header's "Track Height" menu.
TRACK_HEIGHT_PRESETS = {"Small": 48, "Medium": 64, "Large": 96}
DEFAULT_TRACK_HEIGHT = TRACK_HEIGHT_PRESETS["Medium"]


def normalize_track_height(value: Any) -> int:
    """A preset height; anything else (older files saved an unused 80) is Medium."""
    return value if isinstance(value, int) and value in TRACK_HEIGHT_PRESETS.values() else DEFAULT_TRACK_HEIGHT


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
    height: int = DEFAULT_TRACK_HEIGHT  # timeline row height in pixels, one of TRACK_HEIGHT_PRESETS

    # Audio ducking (sidechain compression). When set, this track's own
    # audio is a "duck under" target: its mixed audio is routed through
    # FFmpeg's `sidechaincompress` filter, using the trigger track (e.g. a
    # narration/dialogue track) named by `duck_trigger_track_id` as the
    # sidechain "detector" input -- so this track (e.g. background music)
    # automatically gets quieter whenever the trigger track has audio. The
    # trigger track's own audio is unaffected and still plays normally; it's
    # simply also fed into the compressor as a second, non-audible-output
    # input. See core/filter_graph.py::_apply_audio_ducking().
    duck_trigger_track_id: Optional[str] = None
    duck_threshold: float = 0.05  # sidechaincompress `threshold`, linear 0..1
    duck_ratio: float = 8.0  # sidechaincompress `ratio`, 1..20
    duck_attack: float = 5.0  # sidechaincompress `attack`, milliseconds
    duck_release: float = 250.0  # sidechaincompress `release`, milliseconds

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

    def overlapping_pairs(self, tolerance: float = 1e-3) -> List[tuple]:
        """Every pair of clips on this track that overlap by more than ``tolerance`` seconds."""
        ordered = self.sorted_clips()
        pairs = []
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                if b.timeline_start >= a.timeline_end - tolerance:
                    break
                pairs.append((a, b))
        return pairs

    def validate(self) -> List[str]:
        """Human-readable problems with this track (see :meth:`core.project.Project.validate`)."""
        problems = [
            f"Track '{self.name}': '{a.name}' and '{b.name}' overlap "
            f"({b.timeline_start:.2f}s-{min(a.timeline_end, b.timeline_end):.2f}s)."
            for a, b in self.overlapping_pairs()
        ]
        from core.clip import MediaType

        allowed = {MediaType.AUDIO} if self.track_type == TrackType.AUDIO else {
            MediaType.VIDEO, MediaType.IMAGE, MediaType.TEXT
        }
        problems += [
            f"Track '{self.name}': {c.media_type.value} clip '{c.name}' doesn't belong on a {self.track_type.value} track."
            for c in self.clips if c.media_type not in allowed
        ]
        return problems

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
            "duck_trigger_track_id": self.duck_trigger_track_id,
            "duck_threshold": self.duck_threshold,
            "duck_ratio": self.duck_ratio,
            "duck_attack": self.duck_attack,
            "duck_release": self.duck_release,
            "clips": [c.to_dict() for c in self.clips],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], warnings: Optional[List[str]] = None) -> "Track":
        track = cls(
            track_type=TrackType(data["track_type"]),
            name=data.get("name", ""),
            id=data.get("id", uuid.uuid4().hex[:12]),
            muted=data.get("muted", False),
            locked=data.get("locked", False),
            hidden=data.get("hidden", False),
            height=normalize_track_height(data.get("height")),
            duck_trigger_track_id=data.get("duck_trigger_track_id"),
            duck_threshold=data.get("duck_threshold", 0.05),
            duck_ratio=data.get("duck_ratio", 8.0),
            duck_attack=data.get("duck_attack", 5.0),
            duck_release=data.get("duck_release", 250.0),
        )
        track.clips = []
        for clip_data in data.get("clips", []):
            try:
                track.clips.append(Clip.from_dict(clip_data, warnings))
            except (KeyError, ValueError, TypeError) as exc:
                if warnings is None:
                    raise
                warnings.append(f"Track '{track.name}': skipped a clip this version can't read ({exc}).")
        return track
