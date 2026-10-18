"""Nested sequences / compound clips (F20).

"Nest" turns a selection of clips (on any tracks) into one compound clip: the clips move into a small
project of their own, stored on the compound clip (``Clip.nested``, the sub-project's ``to_dict()``),
and the compound clip plays a render of that sub-project -- an intermediate file in
``~/.video_editor/nested/``, named by a hash of the sub-project and the project's resolution, frame
rate and sample rate. It is an ordinary video clip with embedded audio, so it can be moved, trimmed,
split, faded, keyed, sped up or nested again like any other, and the export, the preview and frame
renders treat it as a normal source.

The render is made right after nesting, and again whenever it is missing (another computer, a
cleared cache) or out of date (the project's resolution, rate or sample rate changed):
:func:`ensure_nested_renders` is called by the exporter, the frame renderer and the editor when a
project opens. "Un-nest" puts the clips back onto the timeline (only for an untrimmed compound clip
at normal speed, where that is exact).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from core.clip import Clip, MediaType
from core.history import Command
from core.project import Project, ProjectSettings
from core.track import Track, TrackType

NESTED_QUALITY = "High (CRF 18)"


class NestingError(ValueError):
    """Why a selection can't be nested, or a compound clip can't be expanded."""


def nested_cache_dir() -> str:
    path = os.path.join(os.path.expanduser("~"), ".video_editor", "nested")
    os.makedirs(path, exist_ok=True)
    return path


def render_key(nested: Dict, settings: ProjectSettings) -> str:
    payload = json.dumps({"nested": nested, "w": settings.width, "h": settings.height, "fps": settings.fps,
                          "sr": settings.sample_rate}, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


def render_path(nested: Dict, settings: ProjectSettings, cache_dir: Optional[str] = None) -> str:
    return os.path.join(cache_dir or nested_cache_dir(), f"nested_{render_key(nested, settings)}.mp4")


def nested_project(clip: Clip, settings: ProjectSettings) -> Project:
    """The compound clip's sub-project, at the outer project's settings."""
    project = Project.from_dict(copy.deepcopy(clip.nested or {}))
    project.settings = ProjectSettings(width=settings.width, height=settings.height, fps=settings.fps,
                                       sample_rate=settings.sample_rate)
    return project


def compound_clips(project: Project) -> List[Clip]:
    return [clip for track in project.tracks for clip in track.clips if clip.nested]


def missing_renders(project: Project, cache_dir: Optional[str] = None) -> List[Clip]:
    """Compound clips whose render isn't there (or no longer matches the project's settings)."""
    return [clip for clip in compound_clips(project)
            if not os.path.isfile(render_path(clip.nested, project.settings, cache_dir))]


def ensure_nested_renders(project: Project, cache_dir: Optional[str] = None,
                          on_progress: Optional[Callable[[int, int], None]] = None,
                          render_missing: bool = True) -> int:
    """Render every compound clip whose intermediate is missing (unless ``render_missing`` is
    False), and point each compound clip at its current render. Returns how many were rendered.
    Raises the exporter's errors."""
    from core.exporter import ExportSettings, Exporter

    rendered = 0
    clips = compound_clips(project)
    for index, clip in enumerate(clips):
        path = render_path(clip.nested, project.settings, cache_dir)
        if not os.path.isfile(path) and render_missing:
            if on_progress is not None:
                on_progress(index, len(clips))
            sub = nested_project(clip, project.settings)
            partial = path + ".part.mp4"
            Exporter(sub).export(ExportSettings(output_path=partial, width=project.settings.width,
                                                height=project.settings.height, fps=project.settings.fps,
                                                quality_preset=NESTED_QUALITY))
            os.replace(partial, path)
            rendered += 1
        clip.source_path = path
    return rendered


# -- nest ------------------------------------------------------------------------------------------
def _selection(project: Project, clip_ids: Sequence[str]) -> List[Tuple[Track, Clip]]:
    chosen = []
    for track in project.tracks:
        for clip in track.clips:
            if clip.id in clip_ids:
                chosen.append((track, clip))
    return chosen


def plan_nest(project: Project, clip_ids: Sequence[str]) -> Tuple[Track, Clip, List[Tuple[str, Clip]]]:
    """(track for the compound clip, the compound clip, [(track id, original clip)]) for nesting
    ``clip_ids``. Raises NestingError when the selection can't be nested."""
    chosen = _selection(project, clip_ids)
    if len(chosen) < 1:
        raise NestingError("Select the clips to nest first.")
    if any(track.locked for track, _clip in chosen):
        raise NestingError("Some of the selected clips are on a locked track.")
    video_tracks = [t for t in project.tracks if t.track_type == TrackType.VIDEO]
    chosen_video = [t for t in video_tracks if any(track is t for track, _c in chosen)]
    if not chosen_video:
        raise NestingError("Select at least one clip on a video track (a compound clip is a video clip).")
    start = min(clip.timeline_start for _t, clip in chosen)
    end = max(clip.timeline_end for _t, clip in chosen)
    if end - start <= 1e-3:
        raise NestingError("The selected clips have no length.")
    target = chosen_video[0]  # the lowest video track in the selection
    chosen_ids = {clip.id for _t, clip in chosen}
    for other in target.clips:
        if other.id not in chosen_ids and other.timeline_start < end - 1e-6 and start + 1e-6 < other.timeline_end:
            raise NestingError(f"“{other.name}” on {target.name} is in the way: select it too, or move it.")

    sub = Project(name="Compound Clip")
    sub.settings = copy.deepcopy(project.settings)
    sub.tracks = []
    used_assets = set()
    used_scripts = set()
    for track in project.tracks:
        clips = [clip for t, clip in chosen if t is track]
        if not clips:
            continue
        copy_track = Track(track_type=track.track_type, name=track.name, muted=track.muted, hidden=track.hidden)
        for clip in clips:
            moved = clip.copy()
            moved.timeline_start = clip.timeline_start - start
            copy_track.clips.append(moved)
            if clip.asset_id:
                used_assets.add(clip.asset_id)
            if clip.ass_event is not None:
                used_scripts.add(clip.ass_event.script_id)
        sub.tracks.append(copy_track)
    sub.media_pool = [copy.deepcopy(a) for a in project.media_pool if a.id in used_assets]
    sub.subtitle_scripts = {k: v for k, v in project.subtitle_scripts.items() if k in used_scripts}
    nested = sub.to_dict()
    length = end - start
    count = 1 + sum(1 for c in compound_clips(project))
    compound = Clip(media_type=MediaType.VIDEO, source_path=render_path(nested, project.settings), source_in=0.0,
                    source_out=length, source_duration=length, timeline_start=start, name=f"Compound Clip {count}",
                    nested=nested)
    return target, compound, [(t.id, clip) for t, clip in chosen]


class NestClipsCommand(Command):
    """Replace the selected clips by one compound clip (one undo step)."""

    def __init__(self, project: Project, clip_ids: Sequence[str]):
        self.project = project
        self.target, self.compound, self.originals = plan_nest(project, clip_ids)
        self.target_id = self.target.id
        self.label = "Nest Clips"

    def do(self) -> None:
        removed = {clip.id for _track_id, clip in self.originals}
        for track in self.project.tracks:
            track.clips = [c for c in track.clips if c.id not in removed]
        target = self.project.find_track(self.target_id)
        if target is not None:
            target.clips.append(self.compound)

    def undo(self) -> None:
        target = self.project.find_track(self.target_id)
        if target is not None:
            target.clips = [c for c in target.clips if c.id != self.compound.id]
        for track_id, clip in self.originals:
            track = self.project.find_track(track_id)
            if track is not None:
                track.clips.append(clip)


# -- un-nest ---------------------------------------------------------------------------------------
def unnest_problem(clip: Clip) -> Optional[str]:
    if not clip.nested:
        return "it isn't a compound clip"
    sub = Project.from_dict(copy.deepcopy(clip.nested))
    if clip.source_in > 1e-3 or clip.source_out < sub.total_duration - 1e-3:
        return "it has been trimmed"
    if abs(clip.speed - 1.0) > 1e-9 or len(clip.speed_keyframes) >= 2 or clip.reverse:
        return "its speed or direction has been changed"
    if clip.freeze_frame_duration > 0:
        return "it has a freeze frame"
    return None


class UnnestClipCommand(Command):
    """Put a compound clip's clips back on the timeline: its video tracks onto the compound clip's
    track and the ones above it, its audio tracks onto the audio tracks in order (adding tracks
    when there aren't enough). Refused (NestingError) when that would overlap other clips."""

    def __init__(self, project: Project, clip_id: str):
        self.project = project
        clip = project.find_clip(clip_id)
        if clip is None:
            raise NestingError("Select a compound clip first.")
        problem = unnest_problem(clip)
        if problem:
            raise NestingError(f"This clip can't be expanded: {problem}.")
        self.compound = clip
        track = project.find_clip_track(clip_id)
        self.compound_track_id = track.id
        sub = Project.from_dict(copy.deepcopy(clip.nested))
        offset = clip.timeline_start
        video = [t for t in project.tracks if t.track_type == TrackType.VIDEO]
        audio = [t for t in project.tracks if t.track_type == TrackType.AUDIO]
        base = video.index(track)
        self.new_tracks: List[Track] = []
        self.placements: List[Tuple[str, Clip]] = []
        counts = {TrackType.VIDEO: 0, TrackType.AUDIO: 0}
        for sub_track in sub.tracks:
            kind = sub_track.track_type
            pool = video if kind == TrackType.VIDEO else audio
            index = (base if kind == TrackType.VIDEO else 0) + counts[kind]
            counts[kind] += 1
            if index < len(pool):
                target = pool[index]
            else:
                target = Track(track_type=kind, name=f"{'Video' if kind == TrackType.VIDEO else 'Audio'} "
                                                     f"{len(pool) + 1}")
                pool.append(target)
                self.new_tracks.append(target)
            for sub_clip in sub_track.clips:
                placed = sub_clip.copy()
                placed.timeline_start = sub_clip.timeline_start + offset
                ignore = clip.id if target is track else None
                if any(other.id != ignore and placed.timeline_start < other.timeline_end - 1e-6
                       and other.timeline_start + 1e-6 < placed.timeline_end for other in target.clips):
                    raise NestingError(f"The expanded clips would overlap other clips on {target.name}.")
                if target.locked:
                    raise NestingError(f"{target.name} is locked.")
                self.placements.append((target.id, placed))
        self.new_assets = [a for a in sub.media_pool if project.find_asset(a.id) is None]
        self.label = "Expand Compound Clip"

    def do(self) -> None:
        for track in self.new_tracks:
            if self.project.find_track(track.id) is None:
                self.project.tracks.append(track)
        for asset in self.new_assets:
            if self.project.find_asset(asset.id) is None:
                self.project.add_media_asset(asset)
        source = self.project.find_track(self.compound_track_id)
        if source is not None:
            source.clips = [c for c in source.clips if c.id != self.compound.id]
        for track_id, clip in self.placements:
            self.project.find_track(track_id).clips.append(clip)

    def undo(self) -> None:
        placed = {clip.id for _t, clip in self.placements}
        for track in self.project.tracks:
            track.clips = [c for c in track.clips if c.id not in placed]
        source = self.project.find_track(self.compound_track_id)
        if source is not None:
            source.clips.append(self.compound)
        for asset in self.new_assets:
            self.project.remove_media_asset(asset.id)
        new_ids = {t.id for t in self.new_tracks}
        self.project.tracks = [t for t in self.project.tracks if t.id not in new_ids]
