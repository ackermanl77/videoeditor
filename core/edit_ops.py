"""Multi-clip and NLE-style edits built from the basic commands in ``core.commands``.

Selection groups (move/delete several clips as one undo step), the clip
clipboard (copy/cut/paste/duplicate, paste attributes), insert/overwrite,
roll/slip/slide trims and detaching a video clip's audio. Every builder
returns one :class:`~core.history.Command` (usually a ``CompositeCommand``)
or ``None`` when the edit isn't possible, and never mutates the project
itself -- ``History.execute`` does that, so each edit is one undo step.

Edits that depend on earlier steps of the same edit (pasting several
clips, splitting before shifting) are planned by applying each step
temporarily and undoing them all again before returning; see
:func:`_plan_sequential`.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional

from core.clip import Clip, MediaType
from core.commands import (
    AddClipCommand,
    AddTrackCommand,
    MoveClipCommand,
    RemoveClipCommand,
    SplitClipCommand,
    TrimClipCommand,
    UpdateClipPropertyCommand,
    build_add_clip_command,
    find_free_track,
)
from core.history import Command, CompositeCommand
from core.project import Project
from core.track import Track, TrackType

CLIPBOARD_FORMAT = "video-editor/clips"
_EPS = 1e-6
#: Shortest clip (timeline seconds) a roll/slide/overwrite may leave behind.
MIN_CLIP_SECONDS = 0.1
#: Gap (seconds) below which two clips count as touching (roll/slide neighbours).
ADJACENT_TOLERANCE = 0.02


def _plan_sequential(steps: List[Callable[[], Optional[Command]]], label: str) -> Optional[Command]:
    """Build each step against the state the previous steps produce; return them as one command.

    Every step is applied while planning (so e.g. the second pasted clip
    sees the first one on its track) and undone again before returning.
    A step returning None is skipped.
    """
    done: List[Command] = []
    try:
        for step in steps:
            command = step()
            if command is None:
                continue
            command.do()
            done.append(command)
    finally:
        for command in reversed(done):
            command.undo()
    if not done:
        return None
    return done[0] if len(done) == 1 else CompositeCommand(done, label)


def _track_type_for(clip: Clip) -> TrackType:
    return TrackType.AUDIO if clip.media_type == MediaType.AUDIO else TrackType.VIDEO


def linked_clip_ids(project: Project, clip_id: str) -> List[str]:
    """``clip_id`` plus every clip sharing its ``link_group`` (e.g. detached audio)."""
    clip = project.find_clip(clip_id)
    if clip is None or not clip.link_group:
        return [clip_id] if clip is not None else []
    return [c.id for t in project.tracks for c in t.clips if c.link_group == clip.link_group]


# ---------------------------------------------------------------------------
# Group move / delete
# ---------------------------------------------------------------------------
def build_move_clips_command(project: Project, clip_ids: Iterable[str], delta: float) -> Optional[Command]:
    """Move several clips by ``delta`` seconds on their own tracks, as one undo step.

    Refused (None) if any of them would start before 0, sits on a locked
    track, or would overlap a clip that isn't moving with it.
    """
    ids = [cid for cid in dict.fromkeys(clip_ids) if project.find_clip(cid) is not None]
    if not ids or abs(delta) < _EPS:
        return None
    moving = set(ids)
    commands: List[Command] = []
    for cid in ids:
        clip = project.find_clip(cid)
        track = project.find_clip_track(cid)
        if track is None or track.locked or clip.timeline_start + delta < -_EPS:
            return None
        start, end = clip.timeline_start + delta, clip.timeline_end + delta
        for other in track.clips:
            if other.id not in moving and start < other.timeline_end - _EPS and other.timeline_start < end - _EPS:
                return None
        commands.append(MoveClipCommand(project, cid, max(0.0, start), track.id))
    return commands[0] if len(commands) == 1 else CompositeCommand(commands, f"Move {len(commands)} Clips")


def build_delete_clips_command(project: Project, clip_ids: Iterable[str], ripple: bool = False) -> Optional[Command]:
    """Delete several clips as one undo step; with ``ripple``, close each track's gaps.

    Rippling shifts every later clip on the same track left by the total
    length deleted before it on that track (same-track ripple, as for a
    single clip).
    """
    ids = [cid for cid in dict.fromkeys(clip_ids) if project.find_clip(cid) is not None]
    if not ids:
        return None
    deleted = set(ids)
    commands: List[Command] = [RemoveClipCommand(project, cid) for cid in ids]
    if ripple:
        for track in project.tracks:
            removed = sorted((c for c in track.clips if c.id in deleted), key=lambda c: c.timeline_start)
            if not removed:
                continue
            for other in track.clips:
                if other.id in deleted:
                    continue
                shift = sum(r.duration for r in removed if r.timeline_start < other.timeline_start - _EPS)
                if shift > _EPS:
                    commands.append(MoveClipCommand(project, other.id, other.timeline_start - shift, track.id))
    label = "Delete Clip" if len(ids) == 1 else f"Delete {len(ids)} Clips"
    return commands[0] if len(commands) == 1 else CompositeCommand(commands, label)


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------
def copy_clips(project: Project, clip_ids: Iterable[str]) -> Optional[Dict[str, Any]]:
    """A JSON-safe clipboard payload: each clip, its track, and its offset from the earliest one."""
    entries = []
    for cid in dict.fromkeys(clip_ids):
        clip, track = project.find_clip(cid), project.find_clip_track(cid)
        if clip is not None and track is not None:
            entries.append((clip, track))
    if not entries:
        return None
    origin = min(clip.timeline_start for clip, _ in entries)
    return {
        "format": CLIPBOARD_FORMAT,
        "clips": [
            {
                "track_id": track.id,
                "track_type": track.track_type.value,
                "offset": clip.timeline_start - origin,
                "clip": clip.to_dict(),
            }
            for clip, track in sorted(entries, key=lambda e: e[0].timeline_start)
        ],
    }


def build_paste_command(project: Project, payload: Optional[Dict[str, Any]], at_time: float) -> Optional[Command]:
    """Paste a :func:`copy_clips` payload at ``at_time``, with fresh ids and no overlaps.

    Each clip goes back to its original track when that track still exists
    and the spot is free, else to another free track of the same type, else
    to a new track (all in the same undo step). Clips linked to each other
    stay linked to each other, not to the originals.
    """
    if not payload or payload.get("format") != CLIPBOARD_FORMAT or not payload.get("clips"):
        return None
    group_map: Dict[str, str] = {}
    new_clips = []
    for entry in payload["clips"]:
        clip = Clip.from_dict(copy.deepcopy(entry["clip"]))
        clip.id = uuid.uuid4().hex[:12]
        if clip.link_group:
            clip.link_group = group_map.setdefault(clip.link_group, uuid.uuid4().hex[:12])
        clip.timeline_start = max(0.0, at_time + float(entry.get("offset", 0.0)))
        new_clips.append((clip, entry.get("track_id")))

    def step(clip: Clip, track_id: Optional[str]) -> Callable[[], Command]:
        return lambda: build_add_clip_command(project, clip, _track_type_for(clip), track_id)

    label = "Paste Clip" if len(new_clips) == 1 else f"Paste {len(new_clips)} Clips"
    return _plan_sequential([step(c, t) for c, t in new_clips], label)


def build_duplicate_command(project: Project, clip_ids: Iterable[str]) -> Optional[Command]:
    """Copy the clips and paste them right after the end of the group."""
    ids = list(clip_ids)
    payload = copy_clips(project, ids)
    if payload is None:
        return None
    end = max(project.find_clip(cid).timeline_end for cid in ids if project.find_clip(cid) is not None)
    return build_paste_command(project, payload, end)


#: Fields "Paste Attributes" copies: effects (colour correction and keys included), transform, audio.
VISUAL_ATTRIBUTES = (
    "opacity",
    "transform_x", "transform_y", "transform_scale", "transform_rotation",
    "reframe_mode", "crop_focus_x", "crop_focus_y", "effects",
)
AUDIO_ATTRIBUTES = ("volume", "denoise_enabled", "denoise_strength")


def build_paste_attributes_command(
    project: Project, payload: Optional[Dict[str, Any]], target_ids: Iterable[str]
) -> Optional[Command]:
    """Apply the first copied clip's effects/colour/transform/audio settings to every target."""
    if not payload or payload.get("format") != CLIPBOARD_FORMAT or not payload.get("clips"):
        return None
    source = Clip.from_dict(copy.deepcopy(payload["clips"][0]["clip"]))
    commands: List[Command] = []
    for cid in dict.fromkeys(target_ids):
        target = project.find_clip(cid)
        if target is None or target.media_type == MediaType.TEXT:
            continue
        fields = list(AUDIO_ATTRIBUTES)
        if target.media_type in (MediaType.VIDEO, MediaType.IMAGE) and source.media_type in (
            MediaType.VIDEO, MediaType.IMAGE
        ):
            fields += VISUAL_ATTRIBUTES
        for name in fields:
            value = copy.deepcopy(getattr(source, name))
            if name == "effects":
                for effect in value:
                    effect.id = uuid.uuid4().hex[:12]
                if [e.to_dict() | {"id": ""} for e in value] == [e.to_dict() | {"id": ""} for e in target.effects]:
                    continue
            elif getattr(target, name) == value:
                continue
            commands.append(UpdateClipPropertyCommand(project, cid, name, value, "Paste Attributes"))
    if not commands:
        return None
    return CompositeCommand(commands, "Paste Attributes")


# ---------------------------------------------------------------------------
# Insert / overwrite
# ---------------------------------------------------------------------------
def _split_steps_at(project: Project, tracks: List[Track], t: float) -> List[Callable[[], Optional[Command]]]:
    """Planning steps splitting every clip on ``tracks`` that straddles ``t``."""
    steps = []
    for track in tracks:
        for clip in list(track.clips):
            if clip.timeline_start + _EPS < t < clip.timeline_end - _EPS:
                steps.append(lambda cid=clip.id: SplitClipCommand(project, cid, t))
    return steps


def build_insert_command(
    project: Project, clip: Clip, preferred_track_id: Optional[str] = None
) -> Optional[Command]:
    """Insert ``clip`` at its ``timeline_start``, rippling every unlocked track to make room.

    Clips crossing the insert point are split there; everything from the
    insert point on shifts right by the clip's duration on every unlocked
    track, so other tracks stay in sync with the target track.
    """
    at, length = clip.timeline_start, clip.duration
    kind = _track_type_for(clip)
    unlocked = [t for t in project.tracks if not t.locked]
    target = project.find_track(preferred_track_id) if preferred_track_id else None
    if target is None or target.track_type != kind or target.locked:
        target = next((t for t in unlocked if t.track_type == kind), None)

    steps = _split_steps_at(project, unlocked, at)

    def shift_step() -> Optional[Command]:
        moves = [
            MoveClipCommand(project, c.id, c.timeline_start + length, t.id)
            for t in unlocked for c in t.clips if c.timeline_start >= at - _EPS
        ]
        # Move later clips first so no intermediate state overlaps.
        moves.sort(key=lambda m: -project.find_clip(m.clip_id).timeline_start)
        return CompositeCommand(moves, "Shift") if moves else None

    steps.append(shift_step)
    if target is not None:
        steps.append(lambda: AddClipCommand(project, target.id, clip))
    else:
        steps.append(lambda: build_add_clip_command(project, clip, kind))
    return _plan_sequential(steps, f"Insert {clip.name}")


def build_overwrite_command(project: Project, clip: Clip, track_id: str) -> Optional[Command]:
    """Place ``clip`` on ``track_id``, removing or trimming whatever lies under its span."""
    track = project.find_track(track_id)
    if track is None or track.locked or track.track_type != _track_type_for(clip):
        return None
    start, end = clip.timeline_start, clip.timeline_end
    steps = _split_steps_at(project, [track], start) + [
        lambda: _split_at_end(project, track, end),
        lambda: _remove_range(project, track, start, end),
        lambda: AddClipCommand(project, track.id, clip),
    ]
    return _plan_sequential(steps, f"Overwrite with {clip.name}")


def _split_at_end(project: Project, track: Track, t: float) -> Optional[Command]:
    for c in track.clips:
        if c.timeline_start + _EPS < t < c.timeline_end - _EPS:
            return SplitClipCommand(project, c.id, t)
    return None


def _remove_range(project: Project, track: Track, start: float, end: float) -> Optional[Command]:
    inside = [c.id for c in track.clips if c.timeline_start >= start - _EPS and c.timeline_end <= end + _EPS]
    return build_delete_clips_command(project, inside) if inside else None


# ---------------------------------------------------------------------------
# Roll / slip / slide
# ---------------------------------------------------------------------------
def _neighbours(project: Project, clip_id: str):
    """``(track, previous touching clip or None, clip, next touching clip or None)``."""
    track = project.find_clip_track(clip_id)
    clip = project.find_clip(clip_id)
    if track is None or clip is None:
        return None, None, None, None
    ordered = track.sorted_clips()
    i = next(k for k, c in enumerate(ordered) if c.id == clip_id)
    prev = ordered[i - 1] if i > 0 else None
    nxt = ordered[i + 1] if i + 1 < len(ordered) else None
    if prev is not None and abs(prev.timeline_end - clip.timeline_start) > ADJACENT_TOLERANCE:
        prev = None
    if nxt is not None and abs(nxt.timeline_start - clip.timeline_end) > ADJACENT_TOLERANCE:
        nxt = None
    return track, prev, clip, nxt


def _source_limit(clip: Clip) -> float:
    return clip.source_duration if clip.source_duration is not None else float("inf")


def _trim_right_to(project: Project, clip: Clip, new_end: float) -> Optional[TrimClipCommand]:
    """Trim/extend ``clip``'s right edge to timeline time ``new_end`` (None if impossible).

    The freeze-frame tail is trimmed first (see :meth:`Clip.trim_right_range`);
    on a reversed clip the right edge is the ``source_in`` end.
    """
    local = new_end - clip.timeline_start
    if local < MIN_CLIP_SECONDS - _EPS:
        return None
    if clip.media_type in (MediaType.IMAGE, MediaType.TEXT):
        source_out = clip.source_in + max(0.0, local - clip.freeze_frame_duration)  # stills/text: any length
        return TrimClipCommand(project, clip.id, clip.source_in, source_out, clip.timeline_start)
    source_in, source_out, freeze = clip.trim_right_range(local)
    if source_out > _source_limit(clip) + 1e-3 or source_in < -1e-3:
        return None  # not enough media past the current edge
    return TrimClipCommand(project, clip.id, max(0.0, source_in), source_out, clip.timeline_start, freeze)


def _trim_left_to(project: Project, clip: Clip, new_start: float) -> Optional[TrimClipCommand]:
    """Trim/extend ``clip``'s left edge to timeline time ``new_start``, keeping its right edge.

    On a reversed clip the left edge is the ``source_out`` end; the left
    edge never reaches into the freeze-frame tail.
    """
    delta = new_start - clip.timeline_start
    if clip.timeline_end - new_start < MIN_CLIP_SECONDS - _EPS or new_start < -_EPS:
        return None
    if clip.media_type in (MediaType.IMAGE, MediaType.TEXT):
        return TrimClipCommand(project, clip.id, clip.source_in, clip.source_out - delta, new_start)
    if clip.playable_duration - delta < MIN_CLIP_SECONDS - _EPS:
        return None
    source_in, source_out = clip.trim_left_range(delta)
    if source_in < -1e-3 or source_out > _source_limit(clip) + 1e-3:
        return None
    return TrimClipCommand(project, clip.id, max(0.0, source_in), source_out, new_start)


def build_roll_command(project: Project, clip_id: str, new_edit_time: float) -> Optional[Command]:
    """Roll the edit point between ``clip_id`` and the clip touching its right edge.

    The left clip's out point and the right clip's in point move together
    to ``new_edit_time``; nothing else on the timeline moves.
    """
    _track, _prev, clip, nxt = _neighbours(project, clip_id)
    if clip is None or nxt is None:
        return None
    left = _trim_right_to(project, clip, new_edit_time)
    right = _trim_left_to(project, nxt, new_edit_time)
    if left is None or right is None:
        return None
    return CompositeCommand([left, right], "Roll Edit")


def build_roll_at_edge(project: Project, clip_id: str, edge: str, new_edit_time: float) -> Optional[Command]:
    """Roll the edit point at ``clip_id``'s ``edge`` ("left" or "right") to ``new_edit_time``.

    The right edge rolls with the clip touching it on the right; the left
    edge with the clip touching it on the left. None when there is no
    touching neighbour on that side or not enough media.
    """
    if edge == "right":
        return build_roll_command(project, clip_id, new_edit_time)
    _track, prev, clip, _nxt = _neighbours(project, clip_id)
    if clip is None or prev is None:
        return None
    return build_roll_command(project, prev.id, new_edit_time)


def build_ripple_move_command(project: Project, clip_id: str, delta: float) -> Optional[Command]:
    """Move a clip and everything after it on its track by ``delta`` seconds, as one undo step.

    Clips linked to any of them (detached audio) move too, so pictures and
    their sound stay in sync. Refused (None) when the move would overlap a
    clip that stays put, start before 0, or touch a locked track.
    """
    clip = project.find_clip(clip_id)
    track = project.find_clip_track(clip_id)
    if clip is None or track is None or track.locked:
        return None
    ids = [c.id for c in track.clips if c.timeline_start >= clip.timeline_start - _EPS]
    for cid in list(ids):
        ids += [linked for linked in linked_clip_ids(project, cid) if linked not in ids]
    command = build_move_clips_command(project, ids, delta)
    if command is not None:
        command.label = "Ripple Move"
    return command


class SlipClipCommand(Command):
    """Shift which part of the source a clip shows; its place on the timeline stays fixed."""

    def __init__(self, project: Project, clip_id: str, source_delta: float):
        self.project = project
        self.clip_id = clip_id
        clip = project.find_clip(clip_id)
        self.old = (clip.source_in, clip.source_out) if clip else None
        self.new = (clip.source_in + source_delta, clip.source_out + source_delta) if clip else None
        self.label = "Slip Clip"

    def _apply(self, values) -> None:
        clip = self.project.find_clip(self.clip_id)
        if clip is not None and values is not None:
            clip.source_in, clip.source_out = values

    def do(self) -> None:
        self._apply(self.new)

    def undo(self) -> None:
        self._apply(self.old)


def build_slip_command(project: Project, clip_id: str, source_delta: float) -> Optional[Command]:
    """Slip by ``source_delta`` source seconds, clamped to the media actually available."""
    clip = project.find_clip(clip_id)
    track = project.find_clip_track(clip_id)
    if clip is None or track is None or track.locked or clip.media_type not in (MediaType.VIDEO, MediaType.AUDIO):
        return None
    low = -clip.source_in
    high = _source_limit(clip) - clip.source_out
    delta = max(low, min(high, source_delta))
    if abs(delta) < 1e-4:
        return None
    return SlipClipCommand(project, clip_id, delta)


def build_slide_command(project: Project, clip_id: str, delta: float) -> Optional[Command]:
    """Slide a clip by ``delta`` seconds between its touching neighbours.

    The clip's own content is unchanged; the previous clip's out point and
    the next clip's in point absorb the move, so the total length of the
    sequence doesn't change. A side with no touching neighbour must have
    room (no overlap) for the move.
    """
    track, prev, clip, nxt = _neighbours(project, clip_id)
    if clip is None or track.locked or abs(delta) < _EPS:
        return None
    new_start = clip.timeline_start + delta
    commands: List[Command] = []
    if prev is not None:
        trim = _trim_right_to(project, prev, new_start)
        if trim is None:
            return None
        commands.append(trim)
    if nxt is not None:
        trim = _trim_left_to(project, nxt, new_start + clip.duration)
        if trim is None:
            return None
        commands.append(trim)
    probe = copy.copy(clip)
    probe.timeline_start = new_start
    neighbours = {c.id for c in (prev, nxt) if c is not None}
    if new_start < -_EPS or any(
        o.id not in neighbours and o.id != clip.id
        and probe.timeline_start < o.timeline_end - _EPS and o.timeline_start < probe.timeline_end - _EPS
        for o in track.clips
    ):
        return None
    # Shrinking neighbours first and growing them last keeps every step overlap-free.
    move = MoveClipCommand(project, clip.id, new_start, track.id)
    ordered = [c for c in commands if _shrinks(project, c)] + [move] + [c for c in commands if not _shrinks(project, c)]
    return CompositeCommand(ordered, "Slide Clip")


def _shrinks(project: Project, trim: TrimClipCommand) -> bool:
    clip = project.find_clip(trim.clip_id)
    trimmed = clip.with_source_range(trim.new_values["source_in"], trim.new_values["source_out"])
    return trimmed.duration < clip.duration


# ---------------------------------------------------------------------------
# Detach audio
# ---------------------------------------------------------------------------
def build_detach_audio_command(project: Project, clip_id: str, has_audio: bool = True) -> Optional[Command]:
    """Move a video clip's own audio onto an audio track as a separate, linked clip.

    The new audio clip has the same source, trim, speed and timing; the
    video clip is muted (its picture is untouched) and both share a
    ``link_group`` so they move together. Lets the audio be trimmed or
    offset independently of the picture (J/L cuts).
    """
    clip = project.find_clip(clip_id)
    if clip is None or clip.media_type != MediaType.VIDEO or clip.muted or not has_audio:
        return None
    group = clip.link_group or uuid.uuid4().hex[:12]
    audio = Clip(
        media_type=MediaType.AUDIO,
        source_path=clip.source_path,
        source_in=clip.source_in,
        source_out=clip.source_out,
        timeline_start=clip.timeline_start,
        name=f"{clip.name} (audio)",
        volume=clip.volume,
        speed=clip.speed,
        speed_keyframes=list(clip.speed_keyframes),
        speed_ramp_smooth=clip.speed_ramp_smooth,
        volume_keyframes=list(clip.volume_keyframes),
        fade_in=clip.fade_in,
        fade_out=clip.fade_out,
        denoise_enabled=clip.denoise_enabled,
        denoise_strength=clip.denoise_strength,
        source_duration=clip.source_duration,
        asset_id=clip.asset_id,
        link_group=group,
    )
    commands: List[Command] = []
    track = find_free_track(project, TrackType.AUDIO, audio.timeline_start, audio.duration)
    if track is None:
        add_track = AddTrackCommand(project, TrackType.AUDIO)
        commands.append(add_track)
        commands.append(AddClipCommand(project, add_track.track_id, audio))
    else:
        commands.append(AddClipCommand(project, track.id, audio))
    commands.append(UpdateClipPropertyCommand(project, clip.id, "muted", True))
    if clip.link_group != group:
        commands.append(UpdateClipPropertyCommand(project, clip.id, "link_group", group))
    return CompositeCommand(commands, "Detach Audio")


# ---------------------------------------------------------------------------
# Premiere-style commands (F28): Lift, Extract, Add Edit to All Tracks, Extend Edit, Match Frame
# ---------------------------------------------------------------------------
def build_lift_command(project: Project, start: float, end: float) -> Optional[Command]:
    """Lift (;): remove the In/Out range from every unlocked track, leaving a gap.

    Clips crossing In or Out are split there first, so only the part inside the
    range goes.
    """
    if end - start < _EPS:
        return None
    unlocked = [t for t in project.tracks if not t.locked]
    # Each split looks up the clip under its time when it runs, after the earlier splits.
    steps = [lambda tr=track, at=at: _split_at_end(project, tr, at) for at in (start, end) for track in unlocked]
    steps += [lambda tr=track: _remove_range(project, tr, start, end) for track in unlocked]
    return _plan_sequential(steps, "Lift")


def build_extract_command(project: Project, start: float, end: float) -> Optional[Command]:
    """Extract ('): remove the In/Out range from every unlocked track and close the gap.

    Like Lift, then everything after Out moves left by the range's length on the
    same tracks, so they stay in sync. Locked tracks don't move (as in Premiere,
    a locked track keeps its place).
    """
    if end - start < _EPS:
        return None
    lift = build_lift_command(project, start, end)
    unlocked = [t for t in project.tracks if not t.locked]
    length = end - start

    def lift_step() -> Optional[Command]:
        return lift

    def shift_step() -> Optional[Command]:
        moves = [
            MoveClipCommand(project, c.id, c.timeline_start - length, t.id)
            for t in unlocked for c in t.clips if c.timeline_start >= end - _EPS
        ]
        moves.sort(key=lambda m: project.find_clip(m.clip_id).timeline_start)  # earliest first: no overlaps
        return CompositeCommand(moves, "Close Gap") if moves else None

    return _plan_sequential([lift_step, shift_step], "Extract")


def build_add_edit_all_tracks(project: Project, t: float) -> Optional[Command]:
    """Add Edit to All Tracks: split every clip under ``t`` on every unlocked track."""
    unlocked = [track for track in project.tracks if not track.locked]
    return _plan_sequential(_split_steps_at(project, unlocked, t), "Add Edit to All Tracks")


def build_extend_edit_command(project: Project, clip_id: str, t: float) -> Optional[Command]:
    """Extend Edit (E): move the selected clip's nearer edit point to ``t``.

    An edge touching a neighbour rolls with it (the neighbour gives or takes the
    same time); a free edge is trimmed or extended, refused when it would run
    into another clip or past the media.
    """
    track, prev, clip, nxt = _neighbours(project, clip_id)
    if clip is None or track is None or track.locked:
        return None
    edge = "right" if abs(t - clip.timeline_end) <= abs(t - clip.timeline_start) else "left"
    if edge == "right":
        if nxt is not None:
            return build_roll_command(project, clip.id, t)
        following = [c for c in track.clips if c.id != clip.id and c.timeline_start >= clip.timeline_end - _EPS]
        if following and t > min(c.timeline_start for c in following) + _EPS:
            return None
        return _trim_right_to(project, clip, t)
    if prev is not None:
        return build_roll_command(project, prev.id, t)
    preceding = [c for c in track.clips if c.id != clip.id and c.timeline_end <= clip.timeline_start + _EPS]
    if preceding and t < max(c.timeline_end for c in preceding) - _EPS:
        return None
    return _trim_left_to(project, clip, t)


def match_frame(project: Project, t: float, clip_id: Optional[str] = None):
    """Match Frame (F): ``(clip, source seconds)`` of the frame shown at timeline time ``t``.

    ``clip_id`` (the selected clip) wins when it is under ``t``; otherwise the
    top-most visible video clip there (the highest video track is drawn on top, as in
    the preview), else an audio clip. None when nothing with a source file is under ``t``.
    """
    def under(clip: Clip) -> bool:
        return clip.timeline_start - _EPS <= t < clip.timeline_end and clip.source_path and clip.media_type != MediaType.TEXT

    chosen = project.find_clip(clip_id) if clip_id else None
    if chosen is None or not under(chosen):
        chosen = None
        for kind in (TrackType.VIDEO, TrackType.AUDIO):
            for track in reversed(project.tracks):
                if track.track_type != kind or track.hidden:
                    continue
                found = next((c for c in track.clips if under(c)), None)
                if found is not None:
                    chosen = found
                    break
            if chosen is not None:
                break
    if chosen is None:
        return None
    offset = min(chosen.source_offset_at(t - chosen.timeline_start), chosen.source_length)
    source = chosen.source_out - offset if chosen.reverse else chosen.source_in + offset
    return chosen, max(0.0, source)


# ---------------------------------------------------------------------------
# Close gaps, select left/right of the playhead, box blade (UI9)
# ---------------------------------------------------------------------------
def _gap_moves(project: Project, moves: List[MoveClipCommand], label: str) -> Optional[Command]:
    moves.sort(key=lambda m: project.find_clip(m.clip_id).timeline_start)  # earliest first: no overlaps
    return CompositeCommand(moves, label) if moves else None


def build_close_track_gaps_command(project: Project, track_id: str) -> Optional[Command]:
    """Close Gaps on one track: its clips move left until each touches the one before it (the
    first one to 0), keeping their order. Other tracks don't move -- that's Close Gaps on All
    Tracks' job, which keeps them in sync."""
    track = project.find_track(track_id)
    if track is None or track.locked:
        return None
    moves, at = [], 0.0
    for clip in sorted(track.clips, key=lambda c: c.timeline_start):
        if clip.timeline_start > at + _EPS:
            moves.append(MoveClipCommand(project, clip.id, at, track.id))
            at += clip.duration
        else:
            at = max(at, clip.timeline_end)
    return _gap_moves(project, moves, "Close Gaps")


def common_gaps(project: Project) -> List[tuple]:
    """``(start, end)`` stretches where no unlocked track has a clip, from 0 to the last clip's end."""
    spans = sorted((c.timeline_start, c.timeline_end) for t in project.tracks if not t.locked for c in t.clips)
    gaps, at = [], 0.0
    for start, end in spans:
        if start > at + _EPS:
            gaps.append((at, start))
        at = max(at, end)
    return gaps


def build_close_all_gaps_command(project: Project) -> Optional[Command]:
    """Close Gaps on All Tracks: removes only the stretches empty on every unlocked track, moving
    everything after each one left by its length -- so the tracks stay in sync, as Blender's
    "Remove Gaps (All)". Locked tracks don't move."""
    gaps = common_gaps(project)
    if not gaps:
        return None
    moves = []
    for track in project.tracks:
        if track.locked:
            continue
        for clip in track.clips:
            shift = sum(end - start for start, end in gaps if end <= clip.timeline_start + _EPS)
            if shift > _EPS:
                moves.append(MoveClipCommand(project, clip.id, clip.timeline_start - shift, track.id))
    return _gap_moves(project, moves, "Close Gaps on All Tracks")


def clips_beside_playhead(project: Project, t: float, side: str) -> List[str]:
    """Select Right/Left of Playhead: the clips on unlocked tracks that start at or after ``t``
    ("right"), or end at or before it ("left"). A clip under the playhead is in neither."""
    if side not in ("left", "right"):
        raise ValueError(side)
    return [c.id for track in project.tracks if not track.locked for c in track.clips
            if (c.timeline_start >= t - _EPS if side == "right" else c.timeline_end <= t + _EPS)]


def build_box_blade_command(project: Project, track_ids: Iterable[str], t: float) -> Optional[Command]:
    """Box blade: the Razor dragged down across tracks cuts every clip under ``t`` on those
    tracks (locked ones excepted), as one undo step."""
    tracks = [tr for tr in (project.find_track(tid) for tid in track_ids) if tr is not None and not tr.locked]
    return _plan_sequential(_split_steps_at(project, tracks, t), "Box Blade")
