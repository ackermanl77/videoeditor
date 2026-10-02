"""Export presets (built-in and user-saved) and source-based export defaults.

A preset is a plain dict of Export dialog settings; any key may be missing,
meaning "leave that control as it is". User presets live in
``~/.video_editor/export_presets.json`` as ``{name: settings}``.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

from core.clip import MediaType
from core.media_asset import MediaAsset
from core.project import Project, write_json_atomic
from core.track import TrackType

#: Frame rates offered by the Export dialog, as (label, frames per second).
#: The NTSC rates are exact fractions (written to FFmpeg as 30000/1001 etc.,
#: see core.filter_graph.fps_expr) so camera footage isn't judder-converted.
FPS_CHOICES = (
    ("23.976", 24000 / 1001),
    ("24", 24.0),
    ("25", 25.0),
    ("29.97", 30000 / 1001),
    ("30", 30.0),
    ("50", 50.0),
    ("59.94", 60000 / 1001),
    ("60", 60.0),
)

BUILTIN_PRESETS: Dict[str, Dict[str, Any]] = {
    "YouTube 1080p": {
        "container": ".mp4", "video_codec": "h264", "width": 1920, "height": 1080,
        "quality": "High (CRF 18)", "audio_bitrate": 192, "loudness": -14.0, "two_pass": False,
    },
    "YouTube 4K (HEVC)": {
        "container": ".mp4", "video_codec": "hevc", "width": 3840, "height": 2160,
        "quality": "High (CRF 18)", "audio_bitrate": 256, "loudness": -14.0, "two_pass": False,
    },
    "Shorts / Reels / TikTok (vertical)": {
        "container": ".mp4", "video_codec": "h264", "width": 1080, "height": 1920, "fps": 30.0,
        "quality": "High (CRF 18)", "audio_bitrate": 192, "loudness": -14.0, "platform": "YouTube Shorts",
    },
    "Archive (ProRes 422 HQ)": {
        "container": ".mov", "video_codec": "prores", "two_pass": False, "loudness": None,
    },
    "Animated GIF": {
        "container": ".gif", "width": 854, "height": 480, "fps": 15.0,
    },
}


def user_presets_path() -> str:
    path = os.path.join(os.path.expanduser("~"), ".video_editor")
    os.makedirs(path, exist_ok=True)
    return os.path.join(path, "export_presets.json")


def load_user_presets(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    try:
        with open(path or user_presets_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def save_user_preset(name: str, settings: Dict[str, Any], path: Optional[str] = None) -> None:
    presets = load_user_presets(path)
    presets[name] = settings
    write_json_atomic(path or user_presets_path(), presets, indent=2)


def delete_user_preset(name: str, path: Optional[str] = None) -> None:
    presets = load_user_presets(path)
    if presets.pop(name, None) is not None:
        write_json_atomic(path or user_presets_path(), presets, indent=2)


def dominant_source(project: Project) -> Optional[MediaAsset]:
    """The video asset covering the most time on the bottom video track (else on any video track)."""
    video_tracks = [t for t in project.tracks if t.track_type == TrackType.VIDEO]
    for tracks in (video_tracks[:1], video_tracks):
        totals: Dict[str, float] = {}
        assets: Dict[str, MediaAsset] = {}
        for track in tracks:
            for clip in track.clips:
                if clip.media_type != MediaType.VIDEO:
                    continue
                asset = project.asset_for_clip(clip)
                if asset is None or asset.width <= 0 or asset.height <= 0:
                    continue
                totals[asset.id] = totals.get(asset.id, 0.0) + clip.duration
                assets[asset.id] = asset
        if totals:
            return assets[max(totals, key=totals.get)]
    return None


def nearest_fps_choice(fps: float) -> Optional[str]:
    """Label of the offered frame rate closest to ``fps`` (None if nothing is within 0.5%)."""
    if fps <= 0:
        return None
    label, value = min(FPS_CHOICES, key=lambda c: abs(c[1] - fps))
    return label if abs(value - fps) / fps < 0.005 else None


def source_defaults(project: Project) -> Optional[Tuple[int, int, Optional[str]]]:
    """``(width, height, fps label or None)`` of the dominant source, for the dialog's defaults."""
    asset = dominant_source(project)
    if asset is None:
        return None
    return asset.width, asset.height, nearest_fps_choice(asset.fps)


def hdr_passthrough_choice(project: Project) -> Optional[str]:
    """The HDR passthrough ("pq"/"hlg") to offer: the transfer all HDR video sources share, else None.

    Decides whether the Export dialog offers an HDR passthrough (see
    core.output_color.passthrough_choice); hidden tracks count too, since
    they can be unhidden before exporting.
    """
    from core.output_color import passthrough_choice

    transfers = []
    for track in project.tracks:
        for clip in track.clips:
            if clip.media_type == MediaType.VIDEO:
                asset = project.asset_for_clip(clip)
                if asset is not None:
                    transfers.append(asset.color_transfer)
    return passthrough_choice(transfers)
