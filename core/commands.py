"""Concrete undoable commands for editing a :class:`~core.project.Project`.

Each command captures just enough state to reverse itself. Commands operate
directly on ``Track``/``Clip`` objects rather than going through extra
indirection, keeping the editing engine easy to follow and extend.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from core.clip import Clip, MediaType, interpolate_keyframes
from core.history import CompositeCommand, Command
from core.project import Marker, Project
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


class TrimClipCommand(Command):
    """Change a clip's in/out points and/or timeline start (ripple-free trim)."""

    def __init__(
        self,
        project: Project,
        clip_id: str,
        source_in: float,
        source_out: float,
        timeline_start: float,
        freeze_frame_duration: Optional[float] = None,
    ):
        """``freeze_frame_duration`` None keeps the clip's freeze frame as it is."""
        self.project = project
        self.clip_id = clip_id
        clip = project.find_clip(clip_id)
        self.old_values = self._snapshot(clip) if clip else None
        if clip is not None:
            # Keyframes re-based with the trim (see Clip.with_source_range).
            trimmed = clip.with_source_range(source_in, source_out)
            trimmed.timeline_start = timeline_start
            if freeze_frame_duration is not None:
                trimmed.freeze_frame_duration = max(0.0, freeze_frame_duration)
            self.new_values = self._snapshot(trimmed)
        else:
            self.new_values = None
        self.label = "Trim Clip"

    _FIELDS = ("source_in", "source_out", "timeline_start", "freeze_frame_duration", "speed_keyframes",
               "opacity_keyframes", "position_keyframes", "volume_keyframes")

    @classmethod
    def _snapshot(cls, clip: Clip) -> dict:
        return {name: copy.copy(getattr(clip, name)) for name in cls._FIELDS}

    def do(self) -> None:
        self._apply(self.new_values)

    def undo(self) -> None:
        self._apply(self.old_values)

    def _apply(self, values: Optional[dict]) -> None:
        clip = self.project.find_clip(self.clip_id)
        if clip is None or values is None:
            return
        for name, value in values.items():
            setattr(clip, name, copy.copy(value))


class SplitClipCommand(Command):
    """Split a clip into two independent clips at timeline position ``t``."""

    def __init__(self, project: Project, clip_id: str, split_time: float):
        self.project = project
        self.clip_id = clip_id
        self.split_time = split_time
        # A fresh id, like every other clip creation (Clip's own
        # default_factory), generated once so redo recreates the *same*
        # second half any later history entries refer to. It used to be
        # `id + "_b"`, which collided on a repeated split (splitting the
        # first half again produced a second "<id>_b").
        self._new_clip_id: str = uuid.uuid4().hex[:12]
        self._original_snapshot: Optional[Clip] = None
        self.label = "Split Clip"

    def do(self) -> None:
        track = self.project.find_clip_track(self.clip_id)
        clip = self.project.find_clip(self.clip_id)
        if track is None or clip is None:
            return
        if not (clip.timeline_start < self.split_time < clip.timeline_end):
            return  # split point outside clip; no-op
        offset_in_timeline = self.split_time - clip.timeline_start
        if clip.in_freeze_frame(offset_in_timeline):
            # Inside the freeze-frame tail there is no footage left to give
            # the second half; trim the freeze (drag the right edge) instead.
            return

        self._original_snapshot = clip.copy()

        # Source seconds from source_in at the cut. A reversed clip plays
        # source_out -> source_in, so its first half keeps the *end* of the
        # source range and the second half the start.
        consumed = min(max(0.0, clip.playback_source_consumed(offset_in_timeline)), clip.source_length)
        offset_in_source = clip.source_length - consumed if clip.reverse else consumed

        second = clip.copy()
        second.id = self._new_clip_id
        second.timeline_start = self.split_time
        # The freeze frame belongs to the clip's end: only the second half keeps it.
        clip_freeze = clip.freeze_frame_duration
        clip.freeze_frame_duration = 0.0
        second.freeze_frame_duration = clip_freeze
        second.fade_in = 0.0
        # Opacity/position keyframe times are clip-relative timeline seconds;
        # speed keyframe times are source_in-relative source seconds. Each
        # half keeps only its own side of the split, re-based to its own
        # start, with a keyframe added exactly at the cut so the animated
        # value (or speed) in effect there carries on unchanged.
        tail_timeline = clip.duration + clip_freeze - offset_in_timeline
        second.opacity_keyframes = _keyframes_after(clip.opacity_keyframes, offset_in_timeline, tail_timeline)
        second.position_keyframes = _keyframes_after(clip.position_keyframes, offset_in_timeline, tail_timeline)
        second.volume_keyframes = _keyframes_after(clip.volume_keyframes, offset_in_timeline, tail_timeline)
        second.effects = [
            e.with_keyframes_mapped(lambda keys: _keyframes_after(keys, offset_in_timeline, tail_timeline))
            for e in clip.effects
        ]
        keys_before = _speed_keyframes_before(clip.speed_keyframes, offset_in_source)
        keys_after = _speed_keyframes_after(clip.speed_keyframes, offset_in_source)
        cut = clip.source_in + offset_in_source
        if clip.reverse:
            second.source_out = cut
            _apply_speed_keyframes(second, keys_before)
            first_speed_keyframes = keys_after
            clip.source_in = cut
        else:
            second.source_in = cut
            _apply_speed_keyframes(second, keys_after)
            first_speed_keyframes = keys_before
            clip.source_out = cut
        clip.fade_out = 0.0
        clip.opacity_keyframes = _keyframes_before(clip.opacity_keyframes, offset_in_timeline)
        clip.position_keyframes = _keyframes_before(clip.position_keyframes, offset_in_timeline)
        clip.volume_keyframes = _keyframes_before(clip.volume_keyframes, offset_in_timeline)
        clip.effects = [
            e.with_keyframes_mapped(lambda keys: _keyframes_before(keys, offset_in_timeline)) for e in clip.effects
        ]
        _apply_speed_keyframes(clip, first_speed_keyframes)

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


_KF_EPS = 1e-6


def _source_offset_at(clip: Clip, timeline_offset: float) -> float:
    """Source seconds (from ``source_in``) shown ``timeline_offset`` seconds into ``clip``.

    Clamped to the clip's own trim; see :meth:`Clip.source_offset_at`.
    """
    return min(clip.source_offset_at(timeline_offset), clip.source_length)


_interpolate_keyframes = interpolate_keyframes


def _keyframes_before(keyframes, cut: float):
    """First half's opacity/position keyframes: those before ``cut``, plus one at ``cut``.

    Fewer than 2 keyframes means "not animated" (the scalar field applies),
    so those are left exactly as they were. Always returns 2+ entries
    otherwise, so a half whose only keyframe would be the cut itself still
    holds the animated value rather than falling back to the scalar field.
    """
    if len(keyframes) < 2:
        return list(keyframes)
    value = _interpolate_keyframes(keyframes, cut)
    kept = [tuple(k) for k in sorted(keyframes, key=lambda k: k[0]) if k[0] < cut - _KF_EPS]
    if not kept:
        kept = [(0.0, *value)]
    return kept + [(cut, *value)]


def _keyframes_after(keyframes, cut: float, tail: float):
    """Second half's opacity/position keyframes, re-based so ``cut`` becomes 0."""
    if len(keyframes) < 2:
        return list(keyframes)
    value = _interpolate_keyframes(keyframes, cut)
    kept = [(k[0] - cut, *k[1:]) for k in sorted(keyframes, key=lambda k: k[0]) if k[0] > cut + _KF_EPS]
    if not kept:
        kept = [(max(tail, _KF_EPS), *value)]
    return [(0.0, *value)] + kept


def _speed_at(keyframes, source_t: float) -> float:
    """Step-function speed in effect at ``source_t`` (see ``Clip.speed_segments``)."""
    kfs = sorted(keyframes, key=lambda k: k[0])
    speed = kfs[0][1]
    for t, s in kfs:
        if t <= source_t + _KF_EPS:
            speed = s
    return speed if speed > 0 else 1.0


def _speed_keyframes_before(keyframes, cut: float):
    """First half's speed keyframes (``None`` = not speed-ramped, leave as is)."""
    if len(keyframes) < 2:
        return None
    kept = [tuple(k) for k in sorted(keyframes, key=lambda k: k[0]) if k[0] < cut - _KF_EPS]
    return kept or [(0.0, _speed_at(keyframes, 0.0))]


def _speed_keyframes_after(keyframes, cut: float):
    """Second half's speed keyframes, re-based so ``cut`` becomes 0 (``None`` = not ramped)."""
    if len(keyframes) < 2:
        return None
    kept = [(k[0] - cut, k[1]) for k in sorted(keyframes, key=lambda k: k[0]) if k[0] > cut + _KF_EPS]
    return [(0.0, _speed_at(keyframes, cut))] + kept


def _apply_speed_keyframes(clip: Clip, keyframes) -> None:
    """Store a split half's speed keyframes; a half left with a single
    speed collapses to a plain constant ``speed`` (a lone keyframe would
    otherwise be ignored in favour of the clip's stale ``speed`` field)."""
    if keyframes is None:
        return
    if len(keyframes) >= 2:
        clip.speed_keyframes = keyframes
    else:
        clip.speed = keyframes[0][1]
        clip.speed_keyframes = []


class UpdateClipPropertyCommand(Command):
    """Generic setter for simple scalar clip properties (volume, speed, ...).

    ``mergeable=True`` (used for the Properties panel's live slider edits)
    lets consecutive edits of the same clip attribute collapse into one undo
    step that still undoes to the value from before the first of them.
    """

    def __init__(
        self,
        project: Project,
        clip_id: str,
        attribute: str,
        new_value: Any,
        label: Optional[str] = None,
        mergeable: bool = False,
    ):
        self.project = project
        self.clip_id = clip_id
        self.attribute = attribute
        self.new_value = new_value
        clip = project.find_clip(clip_id)
        self.old_value = getattr(clip, attribute, None) if clip else None
        self.label = label or f"Edit {attribute.replace('_', ' ').title()}"
        self.merge_key = ("clip-property", clip_id, attribute) if mergeable else None

    def merge(self, other: Command) -> bool:
        if not isinstance(other, UpdateClipPropertyCommand):
            return False
        self.new_value = other.new_value
        return True

    def do(self) -> None:
        clip = self.project.find_clip(self.clip_id)
        if clip is not None:
            setattr(clip, self.attribute, self.new_value)

    def undo(self) -> None:
        clip = self.project.find_clip(self.clip_id)
        if clip is not None:
            setattr(clip, self.attribute, self.old_value)


class UpdateTrackPropertyCommand(Command):
    """Generic setter for a track-level field (muted, locked, hidden, duck trigger, ...).

    The timeline header's mute/lock/hide toggles used to flip these fields
    directly on the model, so they bypassed History: not undoable, and
    undoing an earlier edit never restored them.
    """

    def __init__(self, project: Project, track_id: str, attribute: str, new_value: Any, label: Optional[str] = None):
        self.project = project
        self.track_id = track_id
        self.attribute = attribute
        self.new_value = new_value
        track = project.find_track(track_id)
        self.old_value = getattr(track, attribute, None) if track else None
        self.label = label or f"Edit Track {attribute.replace('_', ' ').title()}"

    def do(self) -> None:
        track = self.project.find_track(self.track_id)
        if track is not None:
            setattr(track, self.attribute, self.new_value)

    def undo(self) -> None:
        track = self.project.find_track(self.track_id)
        if track is not None:
            setattr(track, self.attribute, self.old_value)


class AddTrackCommand(Command):
    """Append a new track.

    The :class:`Track` (and so its id) is created once, here, and the same
    object is re-appended on redo. It used to be created inside ``do()``, so
    every redo made a track with a new id -- breaking any later command in
    the same history (e.g. a clip added to the new track) that referred to
    the first one, and making "add a clip on a new track" impossible to
    express as one undoable step.
    """

    def __init__(self, project: Project, track_type: TrackType, name: Optional[str] = None):
        self.project = project
        self.track_type = track_type
        count = sum(1 for t in project.tracks if t.track_type == track_type) + 1
        default_name = f"{'Video' if track_type == TrackType.VIDEO else 'Audio'} {count}"
        self.track = Track(track_type=track_type, name=name or default_name)
        self.label = "Add Track"

    @property
    def track_id(self) -> str:
        return self.track.id

    def do(self) -> None:
        if self.project.find_track(self.track.id) is None:
            self.project.tracks.append(self.track)

    def undo(self) -> None:
        self.project.remove_track(self.track.id)


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
    freeze_frame_duration: Optional[float] = None,
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
    trim_cmd = TrimClipCommand(project, clip_id, source_in, source_out, timeline_start, freeze_frame_duration)
    if track is None or clip is None:
        return trim_cmd

    old_end = clip.timeline_end
    # Clip.duration of the trimmed clip, so a speed ramp is accounted for
    # (``(out - in) / speed`` shifted later clips by the wrong amount and
    # could overlap them).
    probe = clip.with_source_range(source_in, max(source_in, source_out))
    if freeze_frame_duration is not None:
        probe.freeze_frame_duration = max(0.0, freeze_frame_duration)
    new_duration = probe.duration
    delta = (timeline_start + new_duration) - old_end
    if abs(delta) <= 1e-6:
        return trim_cmd

    affected = [c for c in track.clips if c.id != clip_id and c.timeline_start >= old_end - 1e-3]
    if not affected:
        return trim_cmd

    commands: List[Command] = [trim_cmd]
    commands.extend(MoveClipCommand(project, c.id, c.timeline_start + delta, track.id) for c in affected)
    return CompositeCommand(commands, "Ripple Trim")


class AddMarkerCommand(Command):
    """Add a marker to the project's shared timeline."""

    def __init__(self, project: Project, time: float, name: Optional[str] = None):
        self.project = project
        self.time = time
        self.name = name
        self._marker: Optional[Marker] = None
        self.label = "Add Marker"

    def do(self) -> None:
        self._marker = self.project.add_marker(self.time, self.name)

    def undo(self) -> None:
        if self._marker is not None:
            self.project.remove_marker(self._marker.id)


class RemoveMarkerCommand(Command):
    """Remove a marker from the project."""

    def __init__(self, project: Project, marker_id: str):
        self.project = project
        self.marker_id = marker_id
        marker = project.find_marker(marker_id)
        self._snapshot: Optional[Marker] = copy.deepcopy(marker) if marker else None
        self.label = f"Delete {self._snapshot.name if self._snapshot else 'Marker'}"

    def do(self) -> None:
        self.project.remove_marker(self.marker_id)

    def undo(self) -> None:
        if self._snapshot is not None:
            self.project.markers.append(copy.deepcopy(self._snapshot))


class RenameMarkerCommand(Command):
    """Rename an existing marker."""

    def __init__(self, project: Project, marker_id: str, new_name: str):
        self.project = project
        self.marker_id = marker_id
        self.new_name = new_name
        marker = project.find_marker(marker_id)
        self.old_name = marker.name if marker else ""
        self.label = "Rename Marker"

    def do(self) -> None:
        marker = self.project.find_marker(self.marker_id)
        if marker is not None:
            marker.name = self.new_name

    def undo(self) -> None:
        marker = self.project.find_marker(self.marker_id)
        if marker is not None:
            marker.name = self.old_name


class RecolorMarkerCommand(Command):
    """Change a marker's colour (F15)."""

    def __init__(self, project: Project, marker_id: str, color: str):
        self.project = project
        self.marker_id = marker_id
        self.new_color = color
        marker = project.find_marker(marker_id)
        self.old_color = marker.color if marker else ""
        self.label = "Recolor Marker"

    def do(self) -> None:
        marker = self.project.find_marker(self.marker_id)
        if marker is not None:
            marker.color = self.new_color

    def undo(self) -> None:
        marker = self.project.find_marker(self.marker_id)
        if marker is not None:
            marker.color = self.old_color


# Clip attributes whose change alters Clip.duration (and so can push a clip
# into the next one on its track).
DURATION_ATTRIBUTES = frozenset({"speed", "speed_keyframes", "source_in", "source_out", "freeze_frame_duration"})


def property_change_overlaps(project: Project, clip_id: str, attribute: str, new_value: Any) -> bool:
    """Whether setting ``attribute`` to ``new_value`` would make the clip overlap a neighbour.

    Lowering Speed (or adding a slow-motion speed ramp) lengthens a clip on
    the timeline; applied blindly it ran into the next clip, and the export
    then composited two clips on one track. Callers refuse such an edit.
    Always False for attributes that don't change the clip's duration.
    """
    if attribute not in DURATION_ATTRIBUTES:
        return False
    track = project.find_clip_track(clip_id)
    clip = project.find_clip(clip_id)
    if track is None or clip is None:
        return False
    probe = copy.copy(clip)
    setattr(probe, attribute, new_value)
    return track.has_overlap(probe)


def find_free_track(
    project: Project,
    track_type: TrackType,
    start: float,
    duration: float,
    preferred_track_id: Optional[str] = None,
    order: Optional[List[Track]] = None,
) -> Optional[Track]:
    """First track of ``track_type`` where ``[start, start+duration)`` is free (and not locked).

    Tries ``preferred_track_id`` first, then the other tracks of that type
    in ``order`` (default: project order). None when every candidate would
    overlap -- the caller then adds a new track (see :func:`build_add_clip_command`).
    """
    probe = Clip(media_type=MediaType.VIDEO, source_in=0.0, source_out=max(duration, 1e-3), timeline_start=start)
    candidates = [t for t in (order or project.tracks) if t.track_type == track_type]
    preferred = project.find_track(preferred_track_id) if preferred_track_id else None
    if preferred is not None and preferred.track_type == track_type:
        candidates = [preferred] + [t for t in candidates if t.id != preferred.id]
    for track in candidates:
        if not track.locked and not track.has_overlap(probe):
            return track
    return None


def build_add_clip_command(
    project: Project,
    clip: Clip,
    track_type: TrackType,
    preferred_track_id: Optional[str] = None,
    order: Optional[List[Track]] = None,
) -> Command:
    """Add ``clip`` at its ``timeline_start`` without overlapping anything.

    The preferred track if the spot is free there, else another track of the
    same type where it is, else a brand-new track -- added through
    :class:`AddTrackCommand` inside the same :class:`CompositeCommand`, so
    one Undo removes both (auto-created tracks used to bypass History and
    survive the undo as orphans). Dropping onto an occupied spot or pressing
    T over an existing clip used to create overlapping clips.
    """
    track = find_free_track(project, track_type, clip.timeline_start, clip.duration, preferred_track_id, order)
    if track is not None:
        return AddClipCommand(project, track.id, clip)
    add_track = AddTrackCommand(project, track_type)
    return CompositeCommand([add_track, AddClipCommand(project, add_track.track_id, clip)], f"Add {clip.name}")
