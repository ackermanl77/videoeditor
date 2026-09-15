"""Represents an imported media file available in the project's media bin."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class MediaAsset:
    """Metadata about a file imported into the project's media pool."""

    path: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    thumbnail_path: Optional[str] = None
    waveform_path: Optional[str] = None
    codec: str = ""

    @property
    def display_name(self) -> str:
        return os.path.basename(self.path)

    @property
    def is_image(self) -> bool:
        return not self.has_video and not self.has_audio and self.width > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "thumbnail_path": self.thumbnail_path,
            "waveform_path": self.waveform_path,
            "codec": self.codec,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MediaAsset":
        return cls(**data)
