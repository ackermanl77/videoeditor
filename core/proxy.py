"""Preview proxies: small, short-GOP H.264 copies of heavy sources.

4K, HEVC and 10-bit camera files are long-GOP and slow to seek, so
scrubbing and cut-to-cut playback in the preview stall on decoding. A proxy
(540p, a keyframe every 15 frames, no B-frames) seeks almost instantly. The
preview plays the proxy when one exists (``MediaAsset.preview_path``);
export always reads the original file -- ``core/filter_graph.py`` only ever
uses ``Clip.source_path``.

Encoded with NVENC when available (nearly free on an NVIDIA GPU), else
libx264 ``veryfast``. HDR sources are tone-mapped so the proxy looks like
the SDR export.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from typing import List, Optional

from core.ffmpeg_utils import apply_decode_hwaccel, decode_hwaccel_modes, find_binary, nvenc_available, run
from core.filter_graph import _source_color_filters
from core.media_asset import MediaAsset

PROXY_HEIGHT = 540
PROXY_GOP = 15


def proxy_cache_dir() -> str:
    path = os.path.join(os.path.expanduser("~"), ".video_editor", "proxies")
    os.makedirs(path, exist_ok=True)
    return path


def needs_proxy(asset: MediaAsset) -> bool:
    """The "auto" rule: video taller than 1080p, deeper than 8-bit, or HDR."""
    return asset.has_video and (asset.height > 1080 or asset.bit_depth > 8 or asset.is_hdr)


def proxy_path_for(source_path: str, cache_dir: str) -> str:
    """Cache file for a source, keyed by path + modification time (like thumbnails)."""
    try:
        mtime = os.path.getmtime(source_path)
    except OSError:
        mtime = 0
    key = hashlib.sha1(f"proxy:{source_path}:{mtime}".encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, f"{key}.mp4")


def build_proxy_args(asset: MediaAsset, output_path: str, use_gpu: bool) -> List[str]:
    tonemap = _source_color_filters(asset).lstrip(",")
    video_filter = ",".join(part for part in (tonemap, f"scale=-2:{PROXY_HEIGHT}", "format=yuv420p") if part)
    args = [find_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", asset.path, "-vf", video_filter]
    if use_gpu:
        args += ["-c:v", "h264_nvenc", "-preset", "p1", "-rc:v", "vbr", "-cq:v", "28", "-b:v", "0"]
    else:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26"]
    args += ["-g", str(PROXY_GOP), "-bf", "0"]
    args += ["-c:a", "aac", "-b:a", "128k"] if asset.has_audio else ["-an"]
    args += ["-movflags", "+faststart", output_path]
    return args


def proxy_timeout(duration: float) -> float:
    """Encode budget: 2 minutes plus twice the source duration."""
    return 120.0 + 2.0 * max(0.0, duration or 0.0)


def generate_proxy(asset: MediaAsset, cache_dir: Optional[str] = None, use_gpu: Optional[bool] = None) -> Optional[str]:
    """Create (or reuse) the asset's proxy; returns its path, or None on failure.

    Renders to ``<name>.partial.mp4`` and renames on success, so a failed
    or interrupted encode never leaves a truncated proxy that the preview
    would then try to play. A failed NVENC encode is retried on the CPU.
    """
    if not asset.has_video or not os.path.isfile(asset.path):
        return None
    cache_dir = cache_dir or proxy_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    final_path = proxy_path_for(asset.path, cache_dir)
    if os.path.isfile(final_path):
        return final_path
    partial = final_path[:-4] + ".partial.mp4"
    attempts = [True, False] if (nvenc_available() if use_gpu is None else use_gpu) else [False]
    for gpu in attempts:
        # GPU decode modes first when enabled, ending in software decoding;
        # each attempt overwrites the same .partial file (-y) and a failed
        # one is deleted, so only a complete encode is ever renamed into place.
        for mode in decode_hwaccel_modes():
            try:
                result = run(apply_decode_hwaccel(build_proxy_args(asset, partial, gpu), mode),
                             timeout=proxy_timeout(asset.duration))
            except (subprocess.TimeoutExpired, FileNotFoundError):
                result = None
            if result is not None and result.returncode == 0 and os.path.isfile(partial):
                os.replace(partial, final_path)
                return final_path
            try:
                os.remove(partial)
            except OSError:
                pass
    return None
