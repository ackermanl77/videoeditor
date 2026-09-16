"""Concrete undoable commands for editing a :class:`~core.project.Project`.

Each command captures just enough state to reverse itself. Commands operate
directly on ``Track``/``Clip`` objects rather than going through extra
indirection, keeping the editing engine easy to follow and extend.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from core.clip import Clip
from core.history import CompositeCommand, Command
from core.project import Project
from core.track import Track, TrackType


class AddClipCommand(Command):
    """Add a clip to a track."""

    def __init__(self, project: Project, track_id: str, clip: Clip):
        self.project = project
        self.track_id = track_id
        self.clip = clip
        self.label = f"Add {clip.name}"

    def do(self) -> None:
        track = self.project.find_track(self.track_id)
        if track is not None:
            track.clips.append(self.clip)

    def undo(self) -> None:
        track = self.project.find_track(self.track_id)
        if track is not None:
            track.clips = [c for c in track.clips if c.id != self.clip.id]


class RemoveClipCommand(Command):
    """Remove a clip from its track."""

    def __init__(self, project: Project, clip_id: str):
        self.project = project
        self.clip_id = clip_id
        track = project.find_clip_track(clip_id)
        self.track_id = track.id if track else None
        clip = project.find_clip(clip_id)
        self._clip_snapshot: Optional[Clip] = clip.copy() if clip else None
        self.label = f"Delete {self._clip_snapshot.name if self._clip_snapshot else 'Clip'}"

    def do(self) -> None:
        track = self.project.find_track(self.track_id) if self.track_id else None
        if track is not None:
            track.clips = [c for c in track.clips if c.id != self.clip_id]

    def undo(self) -> None:
        track = self.project.find_track(self.track_id) if self.track_id else None
        if track is not None and self._clip_snapshot is not None:
            track.clips.append(self._clip_snapshot.copy())


class MoveClipCommand(Command):
    """Move a clip to a new start time and/or track."""

    def __init__(self, project: Project, clip_id: str, new_start: float, new_track_id: str):
        self.project = project
        self.clip_id = clip_id
        self.new_start = new_start
        self.new_track_id = new_track_id
        old_track = project.find_clip_track(clip_id)
        clip = project.find_clip(clip_id)
        self.old_track_id = old_track.id if old_track else None
        self.old_start = clip.timeline_start if clip else 0.0
        self.label = "Move Clip"

    def do(self) -> None:
        self._move_to(self.new_track_id, self.new_start)

    def undo(self) -> None:
        self._move_to(self.old_track_id, self.old_start)

    def _move_to(self, track_id: Optional[str], start: float) -> None:
        if track_id is None:
            return
        source_track = self.project.find_clip_track(self.clip_id)
        clip = self.project.find_clip(self.clip_id)
        if clip is None or source_track is None:
            return
        target_track = self.project.find_track(track_id)
        if target_track is None:
            return
        if source_track.id != target_track.id:
            source_track.clips.remove(clip)
            target_track.clips.append(clip)
        clip.timeline_start = max(0.0, start)
        clip.track_index = self.project.tracks.index(target_track)


class TrimClipCommand(Command):
    """Change a clip's in/out points and/or timeline start (ripple-free trim)."""

    def __init__(
        self,
        project: Project,
        clip_id: str,
        source_in: float,
        source_out: float,
        timeline_start: float,
    ):
        self.project = project
        self.clip_id = clip_id
        self.new_values = (source_in, source_out, timeline_start)
        clip = project.find_clip(clip_id)
        self.old_values: Tuple[float, float, float] = (
            (clip.source_in, clip.source_out, clip.timeline_start) if clip else (0.0, 0.0, 0.0)
        )
        self.label = "Trim Clip"

    def do(self) -> None:
        self._apply(self.new_values)

    def undo(self) -> None:
        self._apply(self.old_values)

    def _apply(self, values: Tuple[float, float, float]) -> None:
        clip = self.project.find_clip(self.clip_id)
        if clip is None:
            return
        clip.source_in, clip.source_out, clip.timeline_start = values


class SplitClipCommand(Command):
    """Split a clip into two independent clips at timeline position ``t``."""

    def __init__(self, project: Project, clip_id: str, split_time: float):
        self.project = project
        self.clip_id = clip_id
        self.split_time = split_time
        self._new_clip_id: Optional[str] = None
        self._original_snapshot: Optional[Clip] = None
        self.label = "Split Clip"

    def do(self) -> None:
        track = self.project.find_clip_track(self.clip_id)
        clip = self.project.find_clip(self.clip_id)
        if track is None or clip is None:
            return
        if not (clip.timeline_start < self.split_time < clip.timeline_end):
            return  # split point outside clip; no-op

        self._original_snapshot = clip.copy()

        offset_in_timeline = self.split_time - clip.timeline_start
        offset_in_source = offset_in_timeline * clip.speed

        second = clip.copy()
        second.id = second.id + "_b"
        second.source_in = clip.source_in + offset_in_source
        second.timeline_start = self.split_time
        second.fade_in = 0.0

        clip.source_out = clip.source_in + offset_in_source
        clip.fade_out = 0.0

        self._new_clip_id = second.id
        track.clips.append(second)

    def undo(self) -> None:
        track = self.project.find_clip_track(self.clip_id)
        if track is None or self._original_snapshot is None:
            return
        track.clips = [c for c in track.clips if c.id != self._new_clip_id]
        original = track.find_clip(self.clip_id)
        if original is not None:
            idx = track.clips.index(original)
            track.clips[idx] = self._original_snapshot.copy()


class UpdateClipPropertyCommand(Command):
    """Generic setter for simple scalar clip properties (volume, speed, ...)."""

    def __init__(self, project: Project, clip_id: str, attribute: str, new_value: Any, label: Optional[str] = None):
        self.project = project
        self.clip_id = clip_id
        self.attribute = attribute
        self.new_value = new_value
        clip = project.find_clip(clip_id)
        self.old_value = getattr(clip, attribute, None) if clip else None
        self.label = label or f"Edit {attribute.replace('_', ' ').title()}"

    def do(self) -> None:
        clip = self.project.find_clip(self.clip_id)
        if clip is not None:
            setattr(clip, self.attribute, self.new_value)

    def undo(self) -> None:
        clip = self.project.find_clip(self.clip_id)
        if clip is not None:
            setattr(clip, self.attribute, self.old_value)


class AddTrackCommand(Command):
    def __init__(self, project: Project, track_type: TrackType, name: Optional[str] = None):
        self.project = project
        self.track_type = track_type
        self.name = name
        self._track: Optional[Track] = None
        self.label = "Add Track"

    def do(self) -> None:
        self._track = self.project.add_track(self.track_type, self.name)

    def undo(self) -> None:
        if self._track is not None:
            self.project.remove_track(self._track.id)


class RemoveTrackCommand(Command):
    def __init__(self, project: Project, track_id: str):
        self.project = project
        self.track_id = track_id
        track = project.find_track(track_id)
        self._snapshot: Optional[Track] = copy.deepcopy(track) if track else None
        self._index = project.tracks.index(track) if track in project.tracks else None
        self.label = "Delete Track"

    def do(self) -> None:
        self.project.remove_track(self.track_id)

    def undo(self) -> None:
        if self._snapshot is not None:
            index = self._index if self._index is not None else len(self.project.tracks)
            self.project.tracks.insert(min(index, len(self.project.tracks)), copy.deepcopy(self._snapshot))


class UpdateTextClipCommand(Command):
    """Replace a text clip's :class:`~core.clip.TextStyle` wholesale."""

    def __init__(self, project: Project, clip_id: str, new_style):
        self.project = project
        self.clip_id = clip_id
        self.new_style = new_style
        clip = project.find_clip(clip_id)
        self.old_style = copy.deepcopy(clip.text_style) if clip else None
        self.label = "Edit Text"

    def do(self) -> None:
        clip = self.project.find_clip(self.clip_id)
        if clip is not None:
            clip.text_style = copy.deepcopy(self.new_style)

    def undo(self) -> None:
        clip = self.project.find_clip(self.clip_id)
        if clip is not None:
            clip.text_style = copy.deepcopy(self.old_style)


def build_ripple_delete_command(project: Project, clip_id: str) -> Command:
    """Delete a clip and shift every later clip on the *same track* left to close the gap.

    Same-track only, deliberately — shifting other tracks too would risk
    desyncing anything on them that was intentionally aligned to a specific
    global time (matching the same scoping trade-off already made for
    crossfade transitions: see docs/workflows/color-and-effects). Returns a
    plain ``RemoveClipCommand`` (identical to non-ripple delete) if the clip
    can't be found or nothing follows it on its track.
    """
    track = project.find_clip_track(clip_id)
    clip = project.find_clip(clip_id)
    remove_cmd = RemoveClipCommand(project, clip_id)
    if track is None or clip is None:
        return remove_cmd

    shift = clip.duration
    affected = [c for c in track.clips if c.id != clip_id and c.timeline_start > clip.timeline_start + 1e-6]
    if not affected:
        return remove_cmd

    commands: List[Command] = [remove_cmd]
    commands.extend(MoveClipCommand(project, c.id, c.timeline_start - shift, track.id) for c in affected)
    return CompositeCommand(commands, remove_cmd.label)


def build_ripple_trim_command(
    project: Project,
    clip_id: str,
    source_in: float,
    source_out: float,
    timeline_start: float,
) -> Command:
    """Trim a clip and shift every later same-track clip by the resulting change in its end time.

    Works for either edge: trimming the right edge changes ``source_out``
    (and hence ``timeline_end``) directly; trimming the left edge changes
    ``source_in``/``timeline_start`` together. Either way, what matters for
    ripple is simply the net change in this clip's ``timeline_end`` — every
    later clip on the track shifts by exactly that delta, positive or
    negative, preserving whatever gap (usually zero) it had before.
    """
    track = project.find_clip_track(clip_id)
    clip = project.find_clip(clip_id)
    trim_cmd = TrimClipCommand(project, clip_id, source_in, source_out, timeline_start)
    if track is None or clip is None:
        return trim_cmd

    old_end = clip.timeline_end
    speed = clip.speed if clip.speed > 0 else 1.0
    new_duration = max(0.0, source_out - source_in) / speed
    delta = (timeline_start + new_duration) - old_end
    if abs(delta) <= 1e-6:
        return trim_cmd

    affected = [c for c in track.clips if c.id != clip_id and c.timeline_start >= old_end - 1e-3]
    if not affected:
        return trim_cmd

    commands: List[Command] = [trim_cmd]
    commands.extend(MoveClipCommand(project, c.id, c.timeline_start + delta, track.id) for c in affected)
    return CompositeCommand(commands, "Ripple Trim")
