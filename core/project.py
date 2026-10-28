"""Project model: the top-level container for a video-editing session.

A :class:`Project` owns the media pool (imported assets), the list of
tracks (each holding clips) and project-wide settings such as resolution
and frame rate. Projects are serialized to a simple, human-readable JSON
file with the ``.vproj`` extension.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Dict, List, Optional

from core import __version__
from core.clip import Clip
from core.effects import LEGACY_FIELDS, legacy_effect_dicts, legacy_insert_index
from core.media_asset import MediaAsset
from core.track import Track, TrackType

#: Version 2 added clip -> asset ids, relative media paths, ``app_version``,
#: volume keyframes and the effect stack. Version 3 moved the old colour
#: correction / RGB chroma key clip fields into the effect stack and added
#: effect-parameter keyframes. Older files are migrated on load.
PROJECT_FILE_VERSION = 3

MAX_PROJECT_FILE_SIZE_BYTES = 64 * 1024 * 1024
MAX_PROJECT_COLLECTION_ITEMS = 50_000
MAX_PROJECT_TOTAL_ITEMS = 200_000
MAX_PROJECT_NESTING_DEPTH = 64


class ProjectVersionError(ValueError):
    """The file was written by a newer version of the app with a newer file format."""


class ProjectFileError(ValueError):
    """A project file exceeds supported resource limits or is malformed."""


def read_project_data(path: str) -> Any:
    """Read bounded UTF-8 JSON from a project file with actionable errors."""
    with open(path, "rb") as f:
        content = f.read(MAX_PROJECT_FILE_SIZE_BYTES + 1)
    if len(content) > MAX_PROJECT_FILE_SIZE_BYTES:
        limit_mib = MAX_PROJECT_FILE_SIZE_BYTES / (1024 * 1024)
        raise ProjectFileError(
            f"Project file exceeds the {limit_mib:g} MiB size limit."
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectFileError("Project file is not valid UTF-8.") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProjectFileError(
            f"Project file contains invalid JSON: {exc.msg}."
        ) from exc
    except RecursionError as exc:
        raise ProjectFileError("Project file is nested too deeply to parse.") from exc


def _validate_project_data(data: Any) -> None:
    if not isinstance(data, dict):
        raise ProjectFileError(
            "Project file must contain a JSON object at the top level."
        )

    pending = [(data, 0)]
    total_items = 0
    while pending:
        value, depth = pending.pop()
        if depth > MAX_PROJECT_NESTING_DEPTH:
            raise ProjectFileError(
                f"Project data is nested too deeply (maximum depth: {MAX_PROJECT_NESTING_DEPTH})."
            )
        if isinstance(value, dict):
            item_count = len(value)
            children = value.values()
        elif isinstance(value, list):
            item_count = len(value)
            children = value
        else:
            continue

        if item_count > MAX_PROJECT_COLLECTION_ITEMS:
            raise ProjectFileError(
                f"A project collection contains too many items "
                f"(maximum: {MAX_PROJECT_COLLECTION_ITEMS:,})."
            )
        total_items += item_count
        if total_items > MAX_PROJECT_TOTAL_ITEMS:
            raise ProjectFileError(
                f"Project data contains too many collection items "
                f"(maximum: {MAX_PROJECT_TOTAL_ITEMS:,} total)."
            )
        pending.extend(
            (child, depth + 1) for child in children if isinstance(child, (dict, list))
        )


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _migrate_1_to_2(data: Dict[str, Any]) -> Dict[str, Any]:
    """v1 clips referenced media by absolute path only: link each one to its asset id."""
    by_path = {}
    for asset in data.get("media_pool", []):
        if isinstance(asset, dict) and asset.get("path") and asset.get("id"):
            by_path[_norm(asset["path"])] = asset["id"]
    for track in data.get("tracks", []):
        for clip in track.get("clips", []):
            path = clip.get("source_path")
            if path and not clip.get("asset_id"):
                clip["asset_id"] = by_path.get(_norm(path))
    data["file_version"] = 2
    return data


def _migrate_2_to_3(data: Dict[str, Any]) -> Dict[str, Any]:
    """v2 clips kept colour correction and the RGB chroma key in fixed fields: make them effects.

    They go to the front of the clip's stack in the order the old graph
    applied them (colorkey, then eq, then the stack), so the render is unchanged.
    """
    for track in data.get("tracks", []):
        for clip in track.get("clips", []):
            if not isinstance(clip, dict):
                continue
            legacy = {name: clip.pop(name) for name in list(LEGACY_FIELDS) if name in clip}
            if not legacy:
                continue
            effects = [e for e in clip.get("effects") or [] if isinstance(e, dict)]
            for effect in legacy_effect_dicts(legacy):
                effects.insert(legacy_insert_index([e.get("kind") for e in effects], effect["kind"]), effect)
            clip["effects"] = effects
    data["file_version"] = 3
    return data


#: ``{from_version: migrate(data) -> data}``, applied in order on load.
MIGRATIONS: Dict[int, Callable[[Dict[str, Any]], Dict[str, Any]]] = {1: _migrate_1_to_2, 2: _migrate_2_to_3}


def migrate_project_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Bring saved project data up to :data:`PROJECT_FILE_VERSION`, or refuse a newer file.

    A newer file used to load silently, dropping whatever this version
    doesn't understand; saving it again then lost that data for good.
    """
    try:
        version = int(data.get("file_version", 1))
    except (TypeError, ValueError):
        version = 1
    if version > PROJECT_FILE_VERSION:
        saved_by = data.get("app_version") or "a newer version"
        raise ProjectVersionError(
            f"This project was created by version {saved_by} of the editor (project format {version}). "
            f"This is version {__version__}, which reads project formats up to {PROJECT_FILE_VERSION}. "
            "Update the editor to open it."
        )
    while version < PROJECT_FILE_VERSION:
        data = MIGRATIONS[version](data)
        version += 1
    return data


def _relative_to(path: str, base_dir: Optional[str]) -> Optional[str]:
    """``path`` relative to ``base_dir``; None without a base or across drive letters."""
    if not base_dir or not path:
        return None
    try:
        return os.path.relpath(path, base_dir)
    except ValueError:  # different drives on Windows
        return None


def _resolve_saved_path(path: str, relative: Optional[str], base_dir: Optional[str]) -> str:
    """Prefer the path stored relative to the project file whenever that file exists.

    Moving a project folder together with its media, or opening it from
    another drive letter or PC, then keeps every link working.
    """
    if relative and base_dir:
        candidate = os.path.normpath(os.path.join(base_dir, relative))
        if os.path.isfile(candidate):
            return candidate
    return path


def write_backup_copy(path: str) -> Optional[str]:
    """Copy an existing file to ``<path>.bak`` before it is overwritten; returns the backup path."""
    if not os.path.isfile(path):
        return None
    backup = path + ".bak"
    try:
        shutil.copy2(path, backup)
    except OSError:
        return None
    return backup


def write_json_atomic(path: str, data: Any, indent: Optional[int] = None) -> None:
    """Write ``data`` as JSON to ``path`` without ever leaving it half-written.

    Serializes into a temp file in the same folder, flushes it to disk, then
    ``os.replace``s it over ``path`` (an atomic rename on the same volume).
    A crash, full disk, or serialization error mid-write used to truncate
    the user's only copy of their project; now ``path`` is either the old
    complete file or the new complete file, and a failed write leaves no
    temp file behind.
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


# Marker colours offered by the timeline (F15); any "#rrggbb" a project holds still shows.
MARKER_COLORS = {
    "Blue": "#3b82f6", "Red": "#ef4444", "Orange": "#f97316", "Yellow": "#eab308",
    "Green": "#22c55e", "Purple": "#a855f7", "Pink": "#ec4899", "Gray": "#9ca3af",
}


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
    note: str = ""  # free text shown in the Markers panel (F15)

    def to_dict(self) -> Dict[str, Any]:
        data = {"id": self.id, "time": self.time, "name": self.name, "color": self.color}
        if self.note:
            data["note"] = self.note
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Marker":
        return cls(
            id=data.get("id", uuid.uuid4().hex[:12]),
            time=data.get("time", 0.0),
            name=data.get("name", "Marker"),
            color=data.get("color", "#3b82f6"),
            note=str(data.get("note", "") or ""),
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
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Project:
    """A complete editing project: media pool, tracks and settings."""

    name: str = "Untitled Project"
    settings: ProjectSettings = field(default_factory=ProjectSettings)
    tracks: List[Track] = field(default_factory=list)
    media_pool: List[MediaAsset] = field(default_factory=list)
    markers: List[Marker] = field(default_factory=list)
    # Subclips (F29, core.subclips): named source In/Out ranges of media-pool files.
    subclips: List[Any] = field(default_factory=list)
    # Imported ASS/SSA scripts' headers (everything but [Events]: script
    # info, styles, embedded fonts) by id; styled subtitle clips refer to one
    # through Clip.ass_event.script_id (see core.subtitles).
    subtitle_scripts: Dict[str, str] = field(default_factory=dict)
    file_path: Optional[str] = None
    # Problems found while loading (unknown values replaced by defaults,
    # unreadable clips skipped), shown to the user once after opening.
    load_warnings: List[str] = field(default_factory=list)

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
        self.subclips = [s for s in self.subclips if s.asset_id != asset_id]

    def find_asset(self, asset_id: str) -> Optional[MediaAsset]:
        for a in self.media_pool:
            if a.id == asset_id:
                return a
        return None

    def asset_for_clip(self, clip: Clip) -> Optional[MediaAsset]:
        """The clip's media-pool asset: by ``asset_id`` first, then by path (older projects)."""
        if clip.asset_id:
            asset = self.find_asset(clip.asset_id)
            if asset is not None:
                return asset
        return self.find_asset_by_path(clip.source_path) if clip.source_path else None

    def offline_assets(self) -> List[MediaAsset]:
        """Assets whose file is missing on disk."""
        return [a for a in self.media_pool if a.is_offline]

    def validate(self) -> List[str]:
        """Every structural problem in the timeline: overlapping clips, clips on the wrong track type.

        Overlapping clips on one track used to be composited by the export in
        sorted order while the preview showed whichever ``clip_at`` found
        first, so the two silently disagreed. Checked on load and, in strict
        mode, after every edit (see :class:`core.history.History`).
        """
        problems: List[str] = []
        for track in self.tracks:
            problems += track.validate()
        return problems

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
    def _used_subtitle_scripts(self) -> Dict[str, str]:
        """The subtitle script headers some clip still refers to (an undone or deleted import's
        header stays in memory for redo, but isn't saved)."""
        used = {c.ass_event.script_id for t in self.tracks for c in t.clips if c.ass_event is not None}
        return {sid: header for sid, header in self.subtitle_scripts.items() if sid in used}

    def to_dict(self, base_dir: Optional[str] = None) -> Dict[str, Any]:
        """Serialize. With ``base_dir`` (the project file's folder), media paths are also
        stored relative to it, so a project folder moved together with its media still loads."""
        media_pool = []
        for asset in self.media_pool:
            entry = asset.to_dict()
            entry["path_relative"] = _relative_to(asset.path, base_dir)
            media_pool.append(entry)
        tracks = []
        for track in self.tracks:
            entry = track.to_dict()
            for clip_entry in entry["clips"]:
                clip_entry["source_path_relative"] = _relative_to(clip_entry.get("source_path", ""), base_dir)
            tracks.append(entry)
        return {
            "file_version": PROJECT_FILE_VERSION,
            "app_version": __version__,
            "name": self.name,
            "settings": self.settings.to_dict(),
            "tracks": tracks,
            "media_pool": media_pool,
            "markers": [m.to_dict() for m in self.markers],
            **({"subclips": [s.to_dict() for s in self.subclips]} if self.subclips else {}),
            **({"subtitle_scripts": scripts} if (scripts := self._used_subtitle_scripts()) else {}),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], base_dir: Optional[str] = None) -> "Project":
        """Load saved data: migrated to the current format and tolerant of unknown keys and values.

        Raises :class:`ProjectVersionError` for a newer file format. Anything
        unknown that can be defaulted or skipped is recorded in
        ``load_warnings`` instead of failing the whole load.
        """
        _validate_project_data(data)
        data = migrate_project_data(dict(data))
        warnings: List[str] = []
        project = cls(
            name=data.get("name", "Untitled Project"),
            settings=ProjectSettings.from_dict(data.get("settings", {})) if data.get("settings") else ProjectSettings(),
        )
        for raw in data.get("media_pool", []):
            try:
                asset = MediaAsset.from_dict(raw)
            except (TypeError, ValueError, AttributeError) as exc:
                warnings.append(f"Skipped a media item this version can't read ({exc}).")
                continue
            asset.path = _resolve_saved_path(asset.path, raw.get("path_relative"), base_dir)
            project.media_pool.append(asset)

        tracks = []
        for raw in data.get("tracks", []):
            try:
                track = Track.from_dict(raw, warnings)
            except (KeyError, ValueError, TypeError) as exc:
                warnings.append(f"Skipped a track this version can't read ({exc}).")
                continue
            relative_by_id = {c.get("id"): c.get("source_path_relative") for c in raw.get("clips", [])}
            for clip in track.clips:
                asset = project.find_asset(clip.asset_id) if clip.asset_id else None
                if asset is not None:
                    clip.source_path = asset.path  # resolved through the asset (relative / relinked)
                elif clip.source_path:
                    clip.source_path = _resolve_saved_path(clip.source_path, relative_by_id.get(clip.id), base_dir)
            tracks.append(track)
        project.tracks = tracks or project.tracks
        project.markers = [Marker.from_dict(m) for m in data.get("markers", [])]
        from core.subclips import Subclip  # added without a format bump: absent = none

        for raw in data.get("subclips", []) if isinstance(data.get("subclips"), list) else []:
            try:
                subclip = Subclip.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                warnings.append("Skipped a subclip this version can't read.")
                continue
            if project.find_asset(subclip.asset_id) is not None:
                project.subclips.append(subclip)
        scripts = data.get("subtitle_scripts")  # added without a format bump
        if isinstance(scripts, dict):
            project.subtitle_scripts = {str(k): v for k, v in scripts.items() if isinstance(v, str)}
        project.load_warnings = warnings
        return project

    def save(self, path: str) -> None:
        """Atomically write the project to ``path`` (see :func:`write_json_atomic`).

        ``file_path`` is only updated once the write has succeeded, so a
        failed Save As doesn't leave the project pointing at a file that
        was never written.
        """
        write_json_atomic(path, self.to_dict(base_dir=os.path.dirname(os.path.abspath(path))), indent=2)
        self.file_path = path

    @classmethod
    def load(cls, path: str) -> "Project":
        data = read_project_data(path)
        project = cls.from_dict(data, base_dir=os.path.dirname(os.path.abspath(path)))
        project.file_path = path
        return project
