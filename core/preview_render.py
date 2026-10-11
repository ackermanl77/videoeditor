"""Background preview renders of an In/Out range (F10c).

Playback shows only the top active clip (see ui/preview_player.py); a heavy
composite -- several tracks, transitions, text, effects -- can't play live.
"Render Preview" renders the range with the export engine itself at a small
size (``PREVIEW_HEIGHT`` lines, NVENC when available) into a cache folder,
and the preview then plays that file over the range, exactly as it will
export (picture and the whole mix).

The file is named after a *range key*: a hash of everything that decides
what the range looks and sounds like -- the project's frame size, fps and
sample rate, every clip that overlaps the range (with a margin for
transitions) with all its properties, its track's flags, the subtitle
styles, and each source file's size and modification time. Any edit that
touches the range changes the key, so the render goes stale (the timeline
shows it red and the preview stops using it); undoing back to the same
state finds the old file again. Edits outside the range leave it valid.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Callable, List, Optional

from core.exporter import Exporter, ExportSettings
from core.project import Project

PREVIEW_HEIGHT = 540
# Clips this close to the range count too (a crossfade into or out of it).
RANGE_MARGIN_S = 5.0
CACHE_LIMIT_FILES = 24
RENDER_FORMAT = 1  # bump when the render settings change, so old files aren't reused


@dataclass
class RenderedRange:
    start: float
    end: float
    path: str
    key: str
    valid: bool = True


def preview_cache_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".video_editor", "preview_renders")


def preview_size(project: Project) -> tuple:
    """(width, height) of a preview render: PREVIEW_HEIGHT lines (or the project's own,
    if smaller) at the project's aspect, both even."""
    width, height = project.settings.width, project.settings.height
    target_h = min(PREVIEW_HEIGHT, height)
    target_w = max(2, round(width * target_h / max(height, 1) / 2) * 2)
    return target_w, max(2, target_h // 2 * 2)


def _source_stamp(path: str) -> list:
    try:
        stat = os.stat(path)
        return [stat.st_size, stat.st_mtime_ns]
    except OSError:
        return [None, None]


def range_key(project: Project, start: float, end: float) -> str:
    """The hash described in the module docstring."""
    data = project.to_dict()
    low, high = start - RANGE_MARGIN_S, end + RANGE_MARGIN_S
    tracks = []
    for track in data.get("tracks", []):
        clips = []
        for clip in track.get("clips", []):
            clip_start = float(clip.get("timeline_start", 0.0))
            # A clip's end isn't stored; use the live object for it.
            live = project.find_clip(clip.get("id"))
            clip_end = live.timeline_end if live is not None else clip_start
            if clip_end < low or clip_start > high:
                continue
            clip = {k: v for k, v in clip.items() if k != "source_path_relative"}
            clip["_source"] = _source_stamp(clip.get("source_path", ""))
            clips.append(clip)
        track_info = {k: v for k, v in track.items() if k not in ("clips", "id", "name")}
        tracks.append({"track": track_info, "clips": clips})
    payload = {
        "format": RENDER_FORMAT,
        "range": [round(start, 4), round(end, 4)],
        "settings": data.get("settings"),
        "size": preview_size(project),
        "tracks": tracks,
        "subtitle_scripts": data.get("subtitle_scripts", {}),
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def cached_render(project: Project, start: float, end: float, cache_dir: Optional[str] = None) -> Optional[RenderedRange]:
    """An existing render of exactly this range and state, or None."""
    key = range_key(project, start, end)
    path = os.path.join(cache_dir or preview_cache_dir(), f"{key}.mp4")
    return RenderedRange(start, end, path, key) if os.path.isfile(path) else None


def render_preview(
    project: Project,
    start: float,
    end: float,
    cache_dir: Optional[str] = None,
    on_progress: Optional[Callable[[float, str], None]] = None,
    exporter: Optional[Exporter] = None,
) -> RenderedRange:
    """Render ``start``..``end`` (seconds) into the cache, or reuse an existing render
    of the same state. Raises the exporter's ExportError / ExportCancelled."""
    cache_dir = cache_dir or preview_cache_dir()
    existing = cached_render(project, start, end, cache_dir)
    if existing is not None:
        os.utime(existing.path)  # most recently used: kept by the clean-up
        return existing
    os.makedirs(cache_dir, exist_ok=True)
    key = range_key(project, start, end)
    path = os.path.join(cache_dir, f"{key}.mp4")
    width, height = preview_size(project)
    settings = ExportSettings(
        output_path=path,
        width=width,
        height=height,
        fps=project.settings.fps,
        quality_preset="Smaller File (CRF 28)",
        use_gpu=True,
        trim_start=start,
        trim_end=end,
        subtitle_mode="burn",
    )
    (exporter or Exporter(project)).export(settings, on_progress)
    prune_cache(cache_dir, keep=path)
    return RenderedRange(start, end, path, key)


def prune_cache(cache_dir: str, keep: Optional[str] = None, limit: int = CACHE_LIMIT_FILES) -> List[str]:
    """Delete the least recently used renders beyond ``limit``; returns what was removed."""
    try:
        files = [os.path.join(cache_dir, n) for n in os.listdir(cache_dir) if n.endswith(".mp4")]
    except OSError:
        return []
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    removed = []
    for path in files[limit:]:
        if keep and os.path.normcase(path) == os.path.normcase(keep):
            continue
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    return removed
