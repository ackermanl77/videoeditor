"""Media inspection: probing files with ffprobe and generating thumbnails."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

from core.ffmpeg_utils import find_binary, run
from core.media_asset import MediaAsset


def probe_file(path: str) -> MediaAsset:
    """Run ffprobe on ``path`` and return a populated :class:`MediaAsset`.

    Falls back to sane defaults if ffprobe is unavailable or the file has
    unusual metadata, so the UI never crashes on a bad import.
    """
    asset = MediaAsset(path=path)

    try:
        ffprobe = find_binary("ffprobe")
    except FileNotFoundError:
        return asset

    args = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    result = run(args)
    if result.returncode != 0 or not result.stdout:
        return asset

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return asset

    fmt = data.get("format", {})
    try:
        asset.duration = float(fmt.get("duration", 0.0))
    except (TypeError, ValueError):
        asset.duration = 0.0

    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type")
        if codec_type == "video":
            asset.has_video = True
            asset.width = int(stream.get("width", 0) or 0)
            asset.height = int(stream.get("height", 0) or 0)
            asset.codec = stream.get("codec_name", "")
            rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
            asset.fps = _parse_fraction(rate)
            if asset.duration == 0.0:
                try:
                    asset.duration = float(stream.get("duration", 0.0))
                except (TypeError, ValueError):
                    pass
        elif codec_type == "audio":
            asset.has_audio = True
            if asset.duration == 0.0:
                try:
                    asset.duration = float(stream.get("duration", 0.0))
                except (TypeError, ValueError):
                    pass

    # A single-frame "video" stream with no duration/audio is really a
    # still image (e.g. imported via -loop). Treat near-zero duration
    # image formats as images.
    ext = os.path.splitext(path)[1].lower()
    if ext in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff"}:
        asset.has_video = False
        asset.has_audio = False

    return asset


def _parse_fraction(value: str) -> float:
    try:
        if "/" in value:
            num, den = value.split("/")
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def generate_thumbnail(path: str, cache_dir: str, timestamp: float = 1.0) -> Optional[str]:
    """Extract a single JPEG thumbnail frame from ``path`` at ``timestamp``.

    Returns the thumbnail's file path, or ``None`` on failure. Thumbnails
    are cached by a hash of the source path + mtime so repeated imports of
    the same file are cheap.
    """
    os.makedirs(cache_dir, exist_ok=True)

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0

    key = hashlib.sha1(f"{path}:{mtime}:{timestamp}".encode("utf-8")).hexdigest()
    out_path = os.path.join(cache_dir, f"{key}.jpg")
    if os.path.exists(out_path):
        return out_path

    try:
        ffmpeg = find_binary("ffmpeg")
    except FileNotFoundError:
        return None

    ext = os.path.splitext(path)[1].lower()
    is_image = ext in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff"}

    args = [ffmpeg, "-y"]
    if not is_image:
        args += ["-ss", str(max(0.0, timestamp))]
    args += ["-i", path, "-frames:v", "1", "-vf", "scale=320:-1", out_path]

    result = run(args)
    if result.returncode != 0 or not os.path.exists(out_path):
        return None
    return out_path
