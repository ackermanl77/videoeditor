"""Represents an imported media file available in the project's media bin."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Optional

#: ffprobe ``color_transfer`` values that mark a source as HDR (PQ / HLG).
HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})

#: ffprobe ``color_space`` values of standard-definition (BT.601) video.
BT601_MATRICES = frozenset({"bt470bg", "smpte170m"})


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
    # Colour description of the video stream, from ffprobe (empty = unknown).
    # Used to tone-map HDR to SDR and to convert BT.601 sources to BT.709 at
    # export (core/filter_graph.py), and for the media bin's HDR badge.
    pix_fmt: str = ""
    bit_depth: int = 8
    color_transfer: str = ""
    color_primaries: str = ""
    color_space: str = ""
    color_range: str = ""
    # HDR10 static metadata from the first frame (empty = none/unknown), in
    # x265's units: "G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min)" and "maxcll,maxfall".
    # Written into HDR passthrough exports (core/exporter.py).
    mastering_display: str = ""
    max_cll: str = ""
    # Low-resolution, short-GOP copy used by the preview only (export always
    # reads the original ``path``). None until one is generated.
    proxy_path: Optional[str] = None

    @property
    def display_name(self) -> str:
        return os.path.basename(self.path)

    @property
    def is_image(self) -> bool:
        return not self.has_video and not self.has_audio and self.width > 0

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in HDR_TRANSFERS

    @property
    def is_bt601(self) -> bool:
        return self.color_space in BT601_MATRICES

    @property
    def is_offline(self) -> bool:
        return not os.path.isfile(self.path)

    def preview_path(self) -> str:
        """The file the preview should play: the proxy when it exists, else the original."""
        if self.proxy_path and os.path.isfile(self.proxy_path):
            return self.proxy_path
        return self.path

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MediaAsset":
        # Unknown keys (from a newer version, or a hand-edited file) are
        # ignored rather than raising TypeError in __init__.
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
