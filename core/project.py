"""Project model: the top-level container for a video-editing session.

A :class:`Project` owns the media pool (imported assets), the list of
tracks (each holding clips) and project-wide settings such as resolution
and frame rate. Projects are serialized to a simple, human-readable JSON
file with the ``.vproj`` extension.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.clip import Clip
from core.media_asset import MediaAsset
from core.track import Track, TrackType

PROJECT_FILE_VERSION = 1


@dataclass
class Marker:
    """A named point on the project's shared timeline (a "chapter" point).

    Independent of any track/clip — deleting or moving clips around never
    moves a marker. Rendered on the timeline ruler; also embeddable as real
    chapter metadata in the exported file (see core/exporter.py).
    """

    time: float
    name: str = "Marker"
    color: str = "#3b82f6"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "time": self.time, "name": self.name, "color": self.color}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Marker":
        return cls(
            id=data.get("id", uuid.uuid4().hex[:12]),
            time=data.get("time", 0.0),
            name=data.get("name", "Marker"),
            color=data.get("color", "#3b82f6"),
        )


@dataclass
class ProjectSettings:
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    sample_rate: int = 48000

    def to_dict(self) -> Dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "sample_rate": self.sample_rate,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectSettings":
        return cls(**data)


@dataclass
class Project:
    """A complete editing project: media pool, tracks and settings."""

    name: str = "Untitled Project"
    settings: ProjectSettings = field(default_factory=ProjectSettings)
    tracks: List[Track] = field(default_factory=list)
    media_pool: List[MediaAsset] = field(default_factory=list)
    markers: List[Marker] = field(default_factory=list)
    file_path: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.tracks:
            self.tracks.append(Track(track_type=TrackType.VIDEO, name="Video 1"))
            self.tracks.append(Track(track_type=TrackType.AUDIO, name="Audio 1"))

    # ------------------------------------------------------------------
    # Media pool management
    # ------------------------------------------------------------------
    def add_media_asset(self, asset: MediaAsset) -> None:
        self.media_pool.append(asset)

    def remove_media_asset(self, asset_id: str) -> None:
        self.media_pool = [a for a in self.media_pool if a.id != asset_id]

    def find_asset(self, asset_id: str) -> Optional[MediaAsset]:
        for a in self.media_pool:
            if a.id == asset_id:
                return a
        return None

    def find_asset_by_path(self, path: str) -> Optional[MediaAsset]:
        norm = os.path.normpath(path)
        for a in self.media_pool:
            if os.path.normpath(a.path) == norm:
                return a
        return None

    # ------------------------------------------------------------------
    # Track management
    # ------------------------------------------------------------------
    def add_track(self, track_type: TrackType, name: Optional[str] = None) -> Track:
        count = sum(1 for t in self.tracks if t.track_type == track_type) + 1
        default_name = f"{'Video' if track_type == TrackType.VIDEO else 'Audio'} {count}"
        track = Track(track_type=track_type, name=name or default_name)
        self.tracks.append(track)
        return track

    def remove_track(self, track_id: str) -> None:
        self.tracks = [t for t in self.tracks if t.id != track_id]

    def find_track(self, track_id: str) -> Optional[Track]:
        for t in self.tracks:
            if t.id == track_id:
                return t
        return None

    def find_clip(self, clip_id: str) -> Optional[Clip]:
        for t in self.tracks:
            c = t.find_clip(clip_id)
            if c:
                return c
        return None

    def find_clip_track(self, clip_id: str) -> Optional[Track]:
        for t in self.tracks:
            if t.find_clip(clip_id):
                return t
        return None

    @property
    def total_duration(self) -> float:
        if not self.tracks:
            return 0.0
        return max((t.duration for t in self.tracks), default=0.0)

    # ------------------------------------------------------------------
    # Markers
    # ------------------------------------------------------------------
    def add_marker(self, time: float, name: Optional[str] = None) -> Marker:
        marker = Marker(time=max(0.0, time), name=name or f"Marker {len(self.markers) + 1}")
        self.markers.append(marker)
        return marker

    def remove_marker(self, marker_id: str) -> None:
        self.markers = [m for m in self.markers if m.id != marker_id]

    def find_marker(self, marker_id: str) -> Optional[Marker]:
        for m in self.markers:
            if m.id == marker_id:
                return m
        return None

    def sorted_markers(self) -> List[Marker]:
        return sorted(self.markers, key=lambda m: m.time)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_version": PROJECT_FILE_VERSION,
            "name": self.name,
            "settings": self.settings.to_dict(),
            "tracks": [t.to_dict() for t in self.tracks],
            "media_pool": [a.to_dict() for a in self.media_pool],
            "markers": [m.to_dict() for m in self.markers],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Project":
        project = cls(
            name=data.get("name", "Untitled Project"),
            settings=ProjectSettings.from_dict(data.get("settings", {})) if data.get("settings") else ProjectSettings(),
        )
        project.tracks = [Track.from_dict(t) for t in data.get("tracks", [])] or project.tracks
        project.media_pool = [MediaAsset.from_dict(a) for a in data.get("media_pool", [])]
        project.markers = [Marker.from_dict(m) for m in data.get("markers", [])]
        return project

    def save(self, path: str) -> None:
        self.file_path = path
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path: str) -> "Project":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        project = cls.from_dict(data)
        project.file_path = path
        return project
