"""Subclips (F29): a named In/Out range of a file in the Media Bin.

A subclip is listed in the Media Bin under its file and behaves like it -- drag it onto the timeline,
double-click to append, Insert (,) or Overwrite (.) at the playhead -- but the clip it makes starts
and ends at the subclip's own source In/Out instead of the whole file. Made with Edit > Make Subclip
from the selected clip (its trimmed range, or the part of it inside the timeline's In/Out). Saved
with the project (``Project.subclips``); renaming/deleting one is undoable.

Media Bin items carry an asset id, or ``"sub:<subclip id>"`` for a subclip -- ``media_item()``
resolves either to ``(asset, source_in, source_out)``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from core.history import Command

PREFIX = "sub:"
MIN_SUBCLIP_SECONDS = 0.1


@dataclass
class Subclip:
    asset_id: str
    source_in: float
    source_out: float
    name: str = "Subclip"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def item_id(self) -> str:
        return PREFIX + self.id

    @property
    def duration(self) -> float:
        return self.source_out - self.source_in

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "asset_id": self.asset_id, "name": self.name,
                "source_in": self.source_in, "source_out": self.source_out}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Subclip":
        source_in, source_out = float(data["source_in"]), float(data["source_out"])
        if source_out - source_in < MIN_SUBCLIP_SECONDS or source_in < 0:
            raise ValueError("a subclip needs a positive range")
        return cls(asset_id=str(data["asset_id"]), source_in=source_in, source_out=source_out,
                   name=str(data.get("name") or "Subclip"), id=str(data.get("id") or uuid.uuid4().hex[:12]))


def find_subclip(project, item_id: str) -> Optional[Subclip]:
    key = item_id[len(PREFIX):] if item_id.startswith(PREFIX) else item_id
    return next((s for s in project.subclips if s.id == key), None)


def media_item(project, item_id: str) -> Tuple[Optional[Any], float, Optional[float]]:
    """``(asset, source_in, source_out)`` of a Media Bin item; source_out None = the whole file."""
    if item_id and item_id.startswith(PREFIX):
        subclip = find_subclip(project, item_id)
        if subclip is None:
            return None, 0.0, None
        return project.find_asset(subclip.asset_id), subclip.source_in, subclip.source_out
    return project.find_asset(item_id), 0.0, None


def subclip_range(clip, in_point: Optional[float] = None, out_point: Optional[float] = None) -> Optional[Tuple[float, float]]:
    """The source range for a subclip of `clip`: the part inside the timeline In/Out when both are set
    and overlap it, else the clip's whole trimmed range (lowest and highest source time shown)."""
    start, end = clip.timeline_start, clip.timeline_end - getattr(clip, "freeze_frame_duration", 0.0)
    if in_point is not None and out_point is not None and out_point > in_point:
        start, end = max(start, in_point), min(end, out_point)
    if end - start < MIN_SUBCLIP_SECONDS:
        return None
    a = clip.source_in + clip.source_offset_at(start - clip.timeline_start)
    b = clip.source_in + clip.source_offset_at(end - clip.timeline_start)
    if clip.reverse:
        a, b = clip.source_out - (a - clip.source_in), clip.source_out - (b - clip.source_in)
    low, high = sorted((a, b))
    return (max(0.0, low), high) if high - low >= MIN_SUBCLIP_SECONDS else None


class AddSubclipCommand(Command):
    def __init__(self, project, subclip: Subclip, label: str = "Make Subclip"):
        self.project, self.subclip, self.label = project, subclip, label

    def do(self) -> None:
        self.project.subclips.append(self.subclip)

    def undo(self) -> None:
        self.project.subclips = [s for s in self.project.subclips if s.id != self.subclip.id]


class RemoveSubclipCommand(Command):
    def __init__(self, project, subclip_id: str):
        self.project, self.subclip_id, self.label = project, subclip_id, "Delete Subclip"
        self._index, self._subclip = None, None

    def do(self) -> None:
        for i, subclip in enumerate(self.project.subclips):
            if subclip.id == self.subclip_id:
                self._index, self._subclip = i, self.project.subclips.pop(i)
                return

    def undo(self) -> None:
        if self._subclip is not None:
            self.project.subclips.insert(self._index, self._subclip)


class RenameSubclipCommand(Command):
    def __init__(self, project, subclip_id: str, name: str):
        self.project, self.subclip_id, self.name, self.label = project, subclip_id, name, "Rename Subclip"
        subclip = find_subclip(project, subclip_id)
        self.old = subclip.name if subclip else ""

    def _set(self, name: str) -> None:
        subclip = find_subclip(self.project, self.subclip_id)
        if subclip is not None:
            subclip.name = name

    def do(self) -> None:
        self._set(self.name)

    def undo(self) -> None:
        self._set(self.old)
