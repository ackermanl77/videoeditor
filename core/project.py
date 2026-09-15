"""Project model: the top-level container for a video-editing session.

A :class:`Project` owns the media pool (imported assets), the list of
tracks (each holding clips) and project-wide settings such as resolution
and frame rate. Projects are serialized to a simple, human-readable JSON
file with the ``.vproj`` extension.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.clip import Clip
from core.media_asset import MediaAsset
from core.track import Track, TrackType

PROJECT_FILE_VERSION = 1


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
    # Serialization
    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_version": PROJECT_FILE_VERSION,
            "name": self.name,
            "settings": self.settings.to_dict(),
            "tracks": [t.to_dict() for t in self.tracks],
            "media_pool": [a.to_dict() for a in self.media_pool],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Project":
        project = cls(
            name=data.get("name", "Untitled Project"),
            settings=ProjectSettings.from_dict(data.get("settings", {})) if data.get("settings") else ProjectSettings(),
        )
        project.tracks = [Track.from_dict(t) for t in data.get("tracks", [])] or project.tracks
        project.media_pool = [MediaAsset.from_dict(a) for a in data.get("media_pool", [])]
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
