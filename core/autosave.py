"""Per-session crash-recovery autosaves.

Each running editor writes its own ``<pid>-<id>.vproj`` plus a ``.lock``
holding its PID, and keeps the previous :data:`KEEP_GENERATIONS` autosaves
as ``.vproj.1``, ``.vproj.2``... On start-up, recovery is offered only for
sessions whose process is no longer running. There used to be a single
global ``autosave.vproj``: two instances overwrote each other's recovery
file, and starting a second instance offered to "recover" the first one's
live autosave as if it had crashed.

Stdlib only; ``ui/main_window.py`` is the caller.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.project import write_json_atomic

KEEP_GENERATIONS = 3
LEGACY_AUTOSAVE_NAME = "autosave.vproj"


def default_autosave_dir() -> str:
    path = os.path.join(os.path.expanduser("~"), ".video_editor", "autosave")
    os.makedirs(path, exist_ok=True)
    return path


def pid_alive(pid: int) -> bool:
    """Whether a process with ``pid`` is running (never signals or kills it).

    ``os.kill(pid, 0)`` is a liveness probe on POSIX, but on Windows it calls
    ``TerminateProcess`` -- so Windows asks the process table instead.
    """
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass
class OrphanedAutosave:
    """A recovery candidate left behind by a session that is no longer running."""

    path: str
    meta: Dict[str, Any]
    modified: float
    generations: List[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.meta.get("name") or "an unsaved project"

    @property
    def original_path(self) -> Optional[str]:
        return self.meta.get("original_path")

    def discard(self) -> None:
        stem = self.path.split(".vproj", 1)[0]
        for leftover in glob.glob(glob.escape(stem) + ".*"):
            try:
                os.remove(leftover)
            except OSError:
                pass


class AutosaveSession:
    """This process's autosave slot."""

    def __init__(self, directory: Optional[str] = None, pid: Optional[int] = None):
        self.directory = directory or default_autosave_dir()
        os.makedirs(self.directory, exist_ok=True)
        self.pid = os.getpid() if pid is None else pid
        self.session_id = f"{self.pid}-{uuid.uuid4().hex[:8]}"
        self.path = os.path.join(self.directory, f"{self.session_id}.vproj")
        self.meta_path = self.path + ".meta.json"
        self.lock_path = os.path.join(self.directory, f"{self.session_id}.lock")
        self._locked = False

    def _ensure_lock(self) -> None:
        if not self._locked:
            write_json_atomic(self.lock_path, {"pid": self.pid, "started": time.time()})
            self._locked = True

    def write(self, project_data: Dict[str, Any], meta: Dict[str, Any]) -> None:
        """Save a new autosave generation, keeping the previous ones as ``.1``..``.N``."""
        self._ensure_lock()
        pending_path = self.path + ".pending"
        write_json_atomic(pending_path, project_data)
        oldest = f"{self.path}.{KEEP_GENERATIONS}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for n in range(KEEP_GENERATIONS - 1, 0, -1):
            older = f"{self.path}.{n}"
            if os.path.exists(older):
                os.replace(older, f"{self.path}.{n + 1}")
        if os.path.exists(self.path):
            os.replace(self.path, f"{self.path}.1")
        os.replace(pending_path, self.path)
        write_json_atomic(self.meta_path, dict(meta, saved=time.time()))

    def generations(self) -> List[str]:
        """Existing autosave files of this session, newest first."""
        paths = [self.path + ".pending", self.path] + [
            f"{self.path}.{n}" for n in range(1, KEEP_GENERATIONS + 1)
        ]
        return [p for p in paths if os.path.exists(p)]

    def clear(self) -> None:
        """Remove this session's autosaves (after a save or a deliberate discard); keeps the lock."""
        for path in self.generations() + [self.meta_path]:
            try:
                os.remove(path)
            except OSError:
                pass

    def close(self) -> None:
        """Clean shutdown: remove everything, including the lock."""
        self.clear()
        try:
            os.remove(self.lock_path)
        except OSError:
            pass
        self._locked = False


def find_orphaned_autosaves(directory: Optional[str] = None, exclude_session: Optional[str] = None) -> List[OrphanedAutosave]:
    """Autosaves whose session lock is missing or names a dead process, newest first."""
    directory = directory or default_autosave_dir()
    found: List[OrphanedAutosave] = []
    grouped: Dict[str, List[str]] = {}
    for path in glob.glob(os.path.join(glob.escape(directory), "*.vproj*")):
        filename = os.path.basename(path)
        marker = filename.find(".vproj")
        if marker < 0:
            continue
        suffix = filename[marker + len(".vproj") :]
        if suffix and suffix != ".pending":
            if not suffix.startswith(".") or not suffix[1:].isdigit():
                continue
            if not 1 <= int(suffix[1:]) <= KEEP_GENERATIONS:
                continue
        stem = filename[:marker]
        base_path = os.path.join(directory, stem + ".vproj")
        grouped.setdefault(base_path, []).append(path)

    for base_path, paths in grouped.items():
        stem = os.path.basename(base_path)[: -len(".vproj")]
        if exclude_session and stem == exclude_session:
            continue
        lock = os.path.join(directory, f"{stem}.lock")
        alive = False
        if os.path.exists(lock):
            try:
                with open(lock, "r", encoding="utf-8") as f:
                    alive = pid_alive(int(json.load(f).get("pid", 0)))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                alive = False
        if alive:
            continue
        meta: Dict[str, Any] = {}
        try:
            with open(base_path + ".meta.json", "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
        paths.sort(key=os.path.getmtime, reverse=True)
        found.append(
            OrphanedAutosave(
                path=paths[0],
                meta=meta,
                modified=os.path.getmtime(paths[0]),
                generations=paths,
            )
        )
    found.sort(key=lambda o: o.modified, reverse=True)
    return found
