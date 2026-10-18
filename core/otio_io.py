"""OpenTimelineIO (.otio) export and import (F21): exchange a timeline with Kdenlive, DaVinci
Resolve, Premiere (through OTIO's adapters) and anything else that reads OTIO.

``.otio`` is OpenTimelineIO's JSON serialization. The ``opentimelineio`` package has no wheel for
every Python this app runs on, so the format is written and read here directly -- the schemas used
are the stable core ones (Timeline.1, Stack.1, Track.1, Clip.2 with ``media_references``, Gap.1,
Marker.2, LinearTimeWarp.1, ExternalReference.1, GeneratorReference.1, RationalTime.1,
TimeRange.1), and the files are checked against the real library in the tests where it's available.

What maps:

- Video/audio tracks in order; clips with their file, trim (``source_range`` start = source in,
  duration = timeline duration) and constant speed/reverse as a LinearTimeWarp (negative = reverse);
  empty space as Gaps; text clips as GeneratorReference "text" clips; timeline markers with the
  nearest OTIO marker colour.
- Everything this editor knows about a clip (effects, keyframes, transforms, speed ramps,
  transitions...) also goes into the clip's metadata under ``open_video_editor``, so a round trip
  through this editor is lossless; another program reads what OTIO itself describes.

Import builds a new project: files are probed (``probe``) when they exist and kept as missing media
(for Relink Media) when they don't; transitions and nested stacks are skipped with a warning.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import unquote, urlparse

from core.clip import Clip, MediaType, TextStyle
from core.media_asset import MediaAsset
from core.project import Marker, Project, ProjectSettings
from core.track import Track, TrackType

META_KEY = "open_video_editor"
OTIO_COLORS = {
    "RED": (220, 38, 38), "PINK": (236, 72, 153), "ORANGE": (249, 115, 22), "YELLOW": (234, 179, 8),
    "GREEN": (34, 197, 94), "CYAN": (6, 182, 212), "BLUE": (59, 130, 246), "PURPLE": (139, 92, 246),
    "MAGENTA": (217, 70, 239), "BLACK": (0, 0, 0), "WHITE": (255, 255, 255),
}


# --- building blocks -------------------------------------------------------------------------------

def _time(seconds: float, rate: float) -> Dict[str, Any]:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": float(rate), "value": round(seconds * rate, 6)}


def _range(start: float, duration: float, rate: float) -> Dict[str, Any]:
    return {"OTIO_SCHEMA": "TimeRange.1", "start_time": _time(start, rate), "duration": _time(duration, rate)}


def _seconds(rational: Optional[Dict[str, Any]]) -> float:
    if not rational:
        return 0.0
    rate = float(rational.get("rate") or 1.0)
    return float(rational.get("value") or 0.0) / rate


def _item(schema: str, name: str, **fields: Any) -> Dict[str, Any]:
    item = {"OTIO_SCHEMA": schema, "name": name, "metadata": {}, "effects": [], "markers": [], "enabled": True}
    item.update(fields)
    return item


def _nearest_color(hex_color: str) -> str:
    try:
        rgb = tuple(int(hex_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, TypeError):
        return "BLUE"
    return min(OTIO_COLORS, key=lambda name: sum((a - b) ** 2 for a, b in zip(OTIO_COLORS[name], rgb)))


# --- export ----------------------------------------------------------------------------------------

def project_to_otio(project: Project) -> Dict[str, Any]:
    rate = float(project.settings.fps or 25.0)
    tracks = []
    for track in project.tracks:
        children: List[Dict[str, Any]] = []
        position = 0.0
        for clip in track.sorted_clips():
            if clip.timeline_start > position + 1e-6:
                children.append(_item("Gap.1", "", source_range=_range(0.0, clip.timeline_start - position, rate)))
            children.append(_clip_to_otio(project, clip, rate))
            position = max(position, clip.timeline_end)
        kind = "Audio" if track.track_type == TrackType.AUDIO else "Video"
        otio_track = _item("Track.1", track.name, kind=kind, children=children, source_range=None)
        otio_track.pop("enabled")
        otio_track["metadata"][META_KEY] = {"muted": track.muted, "hidden": track.hidden, "locked": track.locked}
        tracks.append(otio_track)
    stack = _item("Stack.1", "tracks", children=tracks, source_range=None)
    stack["markers"] = [
        {"OTIO_SCHEMA": "Marker.2", "name": marker.name, "marked_range": _range(marker.time, 0.0, rate),
         "color": _nearest_color(marker.color), "comment": "", "metadata": {META_KEY: {"color": marker.color}}}
        for marker in sorted(project.markers, key=lambda m: m.time)
    ]
    return {
        "OTIO_SCHEMA": "Timeline.1", "name": project.name, "global_start_time": None,
        "metadata": {META_KEY: {"settings": project.settings.to_dict()}}, "tracks": stack,
    }


def _clip_to_otio(project: Project, clip: Clip, rate: float) -> Dict[str, Any]:
    duration = clip.duration
    if clip.media_type == MediaType.TEXT:
        reference = {"OTIO_SCHEMA": "GeneratorReference.1", "name": "", "generator_kind": "text",
                     "parameters": {"text": clip.text_style.content if clip.text_style else ""},
                     "available_range": None, "metadata": {}}
        source_range = _range(0.0, duration, rate)
    else:
        asset = project.asset_for_clip(clip)
        available = _range(0.0, asset.duration, rate) if asset is not None and asset.duration else None
        reference = {"OTIO_SCHEMA": "ExternalReference.1", "name": os.path.basename(clip.source_path),
                     "target_url": _file_url(clip.source_path), "available_range": available, "metadata": {}}
        source_range = _range(clip.source_in, duration, rate)
    item = _item("Clip.2", clip.name, source_range=source_range, media_references={"DEFAULT_MEDIA": reference},
                 active_media_reference_key="DEFAULT_MEDIA")
    scalar = (-1.0 if clip.reverse else 1.0) * (clip.speed if clip.speed > 0 else 1.0)
    if abs(scalar - 1.0) > 1e-9 and clip.media_type != MediaType.TEXT:
        item["effects"].append({"OTIO_SCHEMA": "LinearTimeWarp.1", "name": "", "effect_name": "LinearTimeWarp",
                                "time_scalar": scalar, "metadata": {}})
    item["metadata"][META_KEY] = {"clip": clip.to_dict()}
    return item


def _file_url(path: str) -> str:
    try:
        return Path(os.path.abspath(path)).as_uri()
    except ValueError:
        return path


def export_otio(project: Project, path: str) -> None:
    text = json.dumps(project_to_otio(project), indent=4)
    partial = f"{path}.partial"
    with open(partial, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(partial, path)


# --- import ----------------------------------------------------------------------------------------

def _local_path(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme == "file":
        path = unquote(parsed.path)
        if len(path) > 2 and path[0] == "/" and path[2] == ":":  # /C:/... on Windows
            path = path[1:]
        return os.path.normpath(path) if not parsed.netloc else f"//{parsed.netloc}{path}"
    if len(parsed.scheme) == 1:  # a bare Windows path "C:/..."
        return os.path.normpath(url)
    return url if not parsed.scheme else url


def _reference(item: Dict[str, Any]) -> Dict[str, Any]:
    references = item.get("media_references")
    if isinstance(references, dict) and references:
        key = item.get("active_media_reference_key") or "DEFAULT_MEDIA"
        return references.get(key) or next(iter(references.values()))
    return item.get("media_reference") or {}  # Clip.1


def otio_to_project(data: Dict[str, Any], probe: Optional[Callable[[str], MediaAsset]] = None) -> Project:
    if not isinstance(data, dict) or not str(data.get("OTIO_SCHEMA", "")).startswith("Timeline."):
        raise ValueError("not an OpenTimelineIO timeline")
    warnings: List[str] = []
    project = Project(name=str(data.get("name") or "Imported Timeline"))
    project.tracks.clear()
    ours = (data.get("metadata") or {}).get(META_KEY) or {}
    if isinstance(ours.get("settings"), dict):
        project.settings = ProjectSettings.from_dict(ours["settings"])
    assets: Dict[str, MediaAsset] = {}
    stack = data.get("tracks") or {}
    rate_seen: Optional[float] = None
    for otio_track in stack.get("children") or []:
        if not str(otio_track.get("OTIO_SCHEMA", "")).startswith("Track."):
            warnings.append(f"Skipped a nested {otio_track.get('OTIO_SCHEMA', 'item')}.")
            continue
        audio = str(otio_track.get("kind", "Video")).lower() == "audio"
        track = Track(track_type=TrackType.AUDIO if audio else TrackType.VIDEO, name=str(otio_track.get("name") or ""))
        flags = (otio_track.get("metadata") or {}).get(META_KEY) or {}
        track.muted, track.hidden, track.locked = bool(flags.get("muted")), bool(flags.get("hidden")), bool(flags.get("locked"))
        position = 0.0
        for item in otio_track.get("children") or []:
            schema = str(item.get("OTIO_SCHEMA", ""))
            source_range = item.get("source_range") or {}
            duration = _seconds(source_range.get("duration"))
            if schema.startswith("Gap."):
                position += duration
                continue
            if schema.startswith("Transition."):
                warnings.append("Skipped a transition (add it again in the editor).")
                continue
            if not schema.startswith("Clip."):
                warnings.append(f"Skipped a nested {schema or 'item'} on track '{track.name}'.")
                position += duration
                continue
            rate_seen = rate_seen or float((source_range.get("duration") or {}).get("rate") or 0) or None
            clip = _clip_from_otio(item, track, position, duration, assets, probe, project, warnings)
            if clip is not None:
                track.clips.append(clip)
            position += duration
        project.tracks.append(track)
    if not project.tracks:
        project.tracks.extend([Track(track_type=TrackType.VIDEO, name="Video 1"), Track(track_type=TrackType.AUDIO, name="Audio 1")])
    if not isinstance(ours.get("settings"), dict) and rate_seen:
        project.settings.fps = rate_seen
    for marker in stack.get("markers") or []:
        start = _seconds(((marker.get("marked_range") or {}).get("start_time")))
        color = ((marker.get("metadata") or {}).get(META_KEY) or {}).get("color")
        if not color:
            rgb = OTIO_COLORS.get(str(marker.get("color", "BLUE")).upper(), OTIO_COLORS["BLUE"])
            color = "#%02x%02x%02x" % rgb
        project.markers.append(Marker(time=start, name=str(marker.get("name") or "Marker"), color=color))
    project.load_warnings.extend(warnings)
    return project


def _clip_from_otio(item, track, position, duration, assets, probe, project, warnings) -> Optional[Clip]:
    reference = _reference(item)
    ours = ((item.get("metadata") or {}).get(META_KEY) or {}).get("clip")
    if str(reference.get("OTIO_SCHEMA", "")).startswith("GeneratorReference."):
        if isinstance(ours, dict):
            clip = Clip.from_dict(ours)
        else:
            text = str((reference.get("parameters") or {}).get("text") or item.get("name") or "Text")
            clip = Clip(media_type=MediaType.TEXT, source_path="", name=text[:40], text_style=TextStyle(content=text),
                        source_in=0.0, source_out=duration, source_duration=duration)
        clip.timeline_start = position
        return clip if track.track_type == TrackType.VIDEO else None
    path = _local_path(str(reference.get("target_url") or ""))
    if not path:
        warnings.append(f"Skipped clip '{item.get('name', '')}': it has no media file.")
        return None
    asset = assets.get(path)
    if asset is None:
        asset = _asset_for(path, reference, probe, warnings)
        assets[path] = asset
        project.add_media_asset(asset)
    if isinstance(ours, dict):
        clip = Clip.from_dict(ours)
        clip.source_path, clip.asset_id, clip.timeline_start = path, asset.id, position
        return clip
    scalar = 1.0
    for effect in item.get("effects") or []:
        if str(effect.get("OTIO_SCHEMA", "")).startswith("LinearTimeWarp."):
            scalar *= float(effect.get("time_scalar") or 1.0)
    start = _seconds(((item.get("source_range") or {}).get("start_time")))
    speed = abs(scalar) or 1.0
    if track.track_type == TrackType.AUDIO:
        media_type = MediaType.AUDIO
    else:
        media_type = MediaType.IMAGE if asset.is_image else MediaType.VIDEO
    source_length = duration * (1.0 if media_type == MediaType.IMAGE else speed)
    return Clip(media_type=media_type, source_path=path, name=str(item.get("name") or os.path.basename(path)),
                source_in=start, source_out=start + source_length, timeline_start=position,
                source_duration=asset.duration or start + source_length, asset_id=asset.id,
                speed=speed, reverse=scalar < 0)


def _asset_for(path: str, reference, probe, warnings) -> MediaAsset:
    if probe is not None and os.path.isfile(path):
        try:
            return probe(path)
        except Exception as error:  # noqa: BLE001 -- unreadable: keep it as missing media
            warnings.append(f"Couldn't read {os.path.basename(path)}: {error}")
    elif not os.path.isfile(path):
        warnings.append(f"Missing media: {path} (use Relink Media).")
    available = reference.get("available_range") or {}
    return MediaAsset(path=path, duration=_seconds(available.get("duration")), has_video=True, has_audio=True)


def import_otio(path: str, probe: Optional[Callable[[str], MediaAsset]] = None) -> Project:
    with open(path, "r", encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as error:
            raise ValueError(f"not an OpenTimelineIO file: {error}") from error
    return otio_to_project(data, probe)
