"""Media inspection: probing files with ffprobe and generating thumbnails."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from typing import Optional

from core.ffmpeg_utils import find_binary, run
from core.media_asset import MediaAsset

# Extensions ffprobe would otherwise report as a single-frame "video" stream
# (still images have no true duration/audio) — treated as images instead.
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff", ".tif"}

PROBE_TIMEOUT_S = 30.0
THUMBNAIL_TIMEOUT_S = 30.0


class MediaProbeError(Exception):
    """ffprobe ran but couldn't read the file as audio/video/image media."""


def probe_file(path: str) -> MediaAsset:
    """Run ffprobe on ``path`` and return a populated :class:`MediaAsset`.

    Falls back to sane defaults if ffprobe itself is unavailable or the
    file has unusual metadata. But if ffprobe *did* run and couldn't read
    the file (non-zero exit, unparseable output, or no audio/video stream at
    all), raises :class:`MediaProbeError` rather than returning an empty
    ``duration=0``/no-streams asset — importing that as if it were valid
    media just produced a broken, zero-length clip with no explanation.
    ``ui/import_worker.py`` reports the exception as a per-file import error.
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
    try:
        result = run(args, timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise MediaProbeError(
            f"ffprobe did not finish within {PROBE_TIMEOUT_S:.0f} s (damaged file or slow drive?)."
        ) from exc
    if result.returncode != 0 or not result.stdout:
        detail = (result.stderr or "").strip().splitlines()
        raise MediaProbeError(
            "Unreadable media file (ffprobe could not open it)"
            + (f": {detail[-1]}" if detail else ".")
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError(f"Unreadable media file (invalid ffprobe output: {exc}).") from exc

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
            _read_color_description(asset, stream)
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

    if not asset.has_video and not asset.has_audio:
        raise MediaProbeError("Unreadable media file: no audio or video streams found.")

    # A single-frame "video" stream with no duration/audio is really a
    # still image (e.g. imported via -loop). Treat near-zero duration
    # image formats as images.
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTENSIONS:
        asset.has_video = False
        asset.has_audio = False

    return asset


def _read_color_description(asset: MediaAsset, stream: dict) -> None:
    """Fill the asset's pixel format, bit depth and colour tags from an ffprobe video stream."""
    asset.pix_fmt = stream.get("pix_fmt", "") or ""
    raw_depth = stream.get("bits_per_raw_sample")
    try:
        asset.bit_depth = int(raw_depth) if raw_depth else bit_depth_of(asset.pix_fmt)
    except (TypeError, ValueError):
        asset.bit_depth = bit_depth_of(asset.pix_fmt)
    asset.color_transfer = stream.get("color_transfer", "") or ""
    asset.color_primaries = stream.get("color_primaries", "") or ""
    asset.color_space = stream.get("color_space", "") or ""
    asset.color_range = stream.get("color_range", "") or ""


def bit_depth_of(pix_fmt: str) -> int:
    """Bit depth implied by an FFmpeg pixel format name (``yuv420p10le`` -> 10, ``p010le`` -> 10)."""
    match = re.search(r"p(\d{2})(le|be)$", pix_fmt or "")
    if match:
        return int(match.group(1))
    return 10 if (pix_fmt or "").startswith("p010") else 8


def _parse_fraction(value: str) -> float:
    try:
        if "/" in value:
            num, den = value.split("/")
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def safe_thumbnail_time(duration: float, preferred: float = 1.0) -> float:
    """A thumbnail timestamp inside the source (``-ss 1`` past the end of a sub-second clip gave no frame)."""
    if duration and duration > 0:
        return min(preferred, duration / 2.0)
    return preferred


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
    is_image = ext in IMAGE_EXTENSIONS

    args = [ffmpeg, "-y"]
    if not is_image:
        args += ["-ss", str(max(0.0, timestamp))]
    args += ["-i", path, "-frames:v", "1", "-vf", "scale=320:-1", out_path]

    try:
        result = run(args, timeout=THUMBNAIL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not os.path.exists(out_path):
        return None
    return out_path
