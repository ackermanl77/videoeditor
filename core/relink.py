"""Relinking offline media: point an asset (and every clip using it) at a moved file.

Pure model logic, testable without Qt; ``ui/relink_dialog.py`` is the UI.
Candidates are found by file name, then confirmed by probing: the duration
must match within :data:`DURATION_TOLERANCE_S` and the frame size must match
whenever both are known, so a different file that merely shares a name
(``C0001.MP4`` from another card) isn't linked by accident.
"""

from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional

from core.history import Command, CompositeCommand
from core.media_asset import MediaAsset
from core.project import Project

DURATION_TOLERANCE_S = 0.1
#: Folder search stops after this many files, so a drive root can't stall the UI.
MAX_FILES_SCANNED = 50_000


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


class RelinkMediaCommand(Command):
    """Change an asset's path and every clip that uses it, as one undo step.

    Clips are matched by ``asset_id``, or (projects saved before asset ids)
    by their old path.
    """

    def __init__(self, project: Project, asset_id: str, new_path: str):
        self.project = project
        self.asset_id = asset_id
        self.new_path = new_path
        asset = project.find_asset(asset_id)
        self.old_path = asset.path if asset else ""
        self.old_proxy = asset.proxy_path if asset else None
        old_norm = _norm(self.old_path) if self.old_path else None
        self.clip_ids = [
            c.id
            for t in project.tracks
            for c in t.clips
            if c.asset_id == asset_id or (not c.asset_id and old_norm and c.source_path and _norm(c.source_path) == old_norm)
        ]
        self.label = f"Relink {os.path.basename(new_path)}"

    def _apply(self, path: str, proxy: Optional[str]) -> None:
        asset = self.project.find_asset(self.asset_id)
        if asset is not None:
            asset.path = path
            asset.proxy_path = proxy
        for cid in self.clip_ids:
            clip = self.project.find_clip(cid)
            if clip is not None:
                clip.source_path = path
                clip.asset_id = self.asset_id

    def do(self) -> None:
        # A proxy belongs to the old file; the new one gets its own.
        self._apply(self.new_path, None)

    def undo(self) -> None:
        self._apply(self.old_path, self.old_proxy)


def find_candidates(asset: MediaAsset, folder: str, max_files: int = MAX_FILES_SCANNED) -> List[str]:
    """Files under ``folder`` (recursively) with the asset's file name, case-insensitively."""
    wanted = os.path.basename(asset.path).lower()
    found: List[str] = []
    scanned = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            scanned += 1
            if name.lower() == wanted:
                found.append(os.path.join(root, name))
            if scanned >= max_files:
                return found
    return found


def matches(asset: MediaAsset, candidate: MediaAsset) -> bool:
    """Whether a probed candidate is plausibly the same media as the offline asset."""
    if asset.duration > 0 and candidate.duration > 0:
        if abs(asset.duration - candidate.duration) > DURATION_TOLERANCE_S:
            return False
    if asset.width and asset.height and candidate.width and candidate.height:
        if (asset.width, asset.height) != (candidate.width, candidate.height):
            return False
    if asset.has_video != candidate.has_video and not asset.is_image:
        return False
    return True


def verify_candidate(asset: MediaAsset, path: str, probe: Optional[Callable[[str], MediaAsset]] = None) -> bool:
    """Probe ``path`` and check it against the asset (see :func:`matches`); False if unreadable."""
    if probe is None:
        from core.media_probe import probe_file as probe
    try:
        return matches(asset, probe(path))
    except Exception:  # noqa: BLE001 - an unreadable candidate is simply not a match
        return False


def relink_from_folder(
    project: Project,
    folder: str,
    probe: Optional[Callable[[str], MediaAsset]] = None,
    assets: Optional[List[MediaAsset]] = None,
) -> Dict[str, str]:
    """``{asset_id: new_path}`` for every offline asset (or ``assets``) found and verified in ``folder``."""
    found: Dict[str, str] = {}
    for asset in assets if assets is not None else project.offline_assets():
        for candidate in find_candidates(asset, folder):
            if verify_candidate(asset, candidate, probe):
                found[asset.id] = candidate
                break
    return found


def build_relink_command(project: Project, mapping: Dict[str, str]) -> Optional[Command]:
    """One undo step relinking every ``asset_id -> path`` in ``mapping``."""
    commands = [RelinkMediaCommand(project, asset_id, path) for asset_id, path in mapping.items()
                if project.find_asset(asset_id) is not None]
    if not commands:
        return None
    return commands[0] if len(commands) == 1 else CompositeCommand(commands, f"Relink {len(commands)} Files")
