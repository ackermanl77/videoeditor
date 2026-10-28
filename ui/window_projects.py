"""New, open and save: the project file, the recent-projects menu, missing-media relinking and
the autosave timer's writes (R8: split out of main_window.py). A mixin of MainWindow; the autosave
recovery offer, the unsaved-changes prompt and the load-problem reports (which OTIO import
shares, and whose QMessageBox the tests patch) stay in main_window.py."""

from __future__ import annotations

import json
import os
from typing import List

from PySide6.QtWidgets import (
    QFileDialog,
    QMessageBox,
)

from core.project import Project, write_backup_copy, write_json_atomic
from ui.relink_dialog import RelinkDialog


PROJECT_FILE_FILTER = "Video Editor Project (*.vproj)"

MAX_RECENT_PROJECTS = 10

def _recent_projects_path() -> str:
    path = os.path.join(os.path.expanduser("~"), ".video_editor")
    os.makedirs(path, exist_ok=True)
    return os.path.join(path, "recent_projects.json")

def _load_recent_projects() -> List[str]:
    path = _recent_projects_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []

def _save_recent_projects(paths: List[str]) -> None:
    try:
        write_json_atomic(_recent_projects_path(), paths, indent=2)
    except (OSError, TypeError, ValueError):
        pass


class ProjectsMixin:
    def new_project(self) -> None:
        if not self._confirm_discard_if_dirty():
            return
        self._clear_autosave()
        self.project = Project()
        self.history = self._new_history()
        self._thumb_pixmap_cache.clear()
        self._filmstrip_pixmap_cache.clear()
        self._waveform_cache.clear()
        self._load_project_into_panels()

    def open_project(self) -> None:
        if not self._confirm_discard_if_dirty():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open Project", "", PROJECT_FILE_FILTER)
        if not path:
            return
        self._open_project_from_path(path)

    def open_project_file(self, path: str) -> None:
        """Open a .vproj passed on the command line (double-click via the installer's association)."""
        if not self._confirm_discard_if_dirty():
            return
        if not os.path.isfile(path):
            QMessageBox.warning(self, "Open Project", f"This file could not be found:\n{path}")
            return
        self._open_project_from_path(path)

    def _open_recent_project(self, path: str) -> None:
        if not self._confirm_discard_if_dirty():
            return
        if not os.path.isfile(path):
            QMessageBox.warning(self, "Open Project", f"This file could not be found:\n{path}")
            self._remove_recent_project(path)
            return
        self._open_project_from_path(path)

    def _open_project_from_path(self, path: str) -> None:
        try:
            project = Project.load(path)
        except Exception as exc:  # noqa: BLE001 - includes ProjectVersionError's "newer version" message
            QMessageBox.critical(self, "Open Project", f"Could not open project:\n{exc}")
            return
        self._clear_autosave()
        self.project = project
        self.history = self._new_history()
        self._thumb_pixmap_cache.clear()
        self._filmstrip_pixmap_cache.clear()
        self._waveform_cache.clear()
        self._load_project_into_panels()
        self._report_load_problems()
        self._warn_about_missing_media()
        self._render_compound_clips()
        self._add_recent_project(path)

    # ------------------------------------------------------------------
    # Recent Projects
    # ------------------------------------------------------------------
    def _add_recent_project(self, path: str) -> None:
        path = os.path.abspath(path)
        paths = [p for p in _load_recent_projects() if os.path.abspath(p) != path]
        paths.insert(0, path)
        _save_recent_projects(paths[:MAX_RECENT_PROJECTS])
        self._refresh_recent_projects_menu()

    def _remove_recent_project(self, path: str) -> None:
        target = os.path.abspath(path)
        paths = [p for p in _load_recent_projects() if os.path.abspath(p) != target]
        _save_recent_projects(paths)
        self._refresh_recent_projects_menu()

    def _clear_recent_projects(self) -> None:
        _save_recent_projects([])
        self._refresh_recent_projects_menu()

    def _refresh_recent_projects_menu(self) -> None:
        menu = self.recent_projects_menu
        menu.clear()
        paths = _load_recent_projects()
        if not paths:
            empty_action = menu.addAction("No Recent Projects")
            empty_action.setEnabled(False)
            return
        for path in paths:
            exists = os.path.isfile(path)
            label = os.path.basename(path) if exists else f"{os.path.basename(path)} (missing)"
            action = menu.addAction(label)
            action.setToolTip(path)
            action.setEnabled(exists)
            if exists:
                action.triggered.connect(lambda checked=False, p=path: self._open_recent_project(p))
        menu.addSeparator()
        clear_action = menu.addAction("Clear Recent Projects")
        clear_action.triggered.connect(self._clear_recent_projects)

    def open_relink_dialog(self) -> None:
        dialog = RelinkDialog(self.project, self.history, self)
        dialog.relinked.connect(self._on_media_relinked)
        dialog.exec()

    def _on_media_relinked(self, count: int) -> None:
        self._thumb_pixmap_cache.clear()
        self._filmstrip_pixmap_cache.clear()
        self._waveform_cache.clear()
        self._refresh_media_bin()
        self.timeline._layout_key = None
        self.timeline.rebuild()
        self.preview.refresh_after_edit()
        self.status_bar.showMessage(f"Relinked {count} file(s).", 4000)

    def save_project(self) -> bool:
        """Save to the current file (or ask where, first time). Returns True if saved."""
        if self.project.file_path is None:
            return self.save_project_as()
        return self._save_to(self.project.file_path)

    def save_project_as(self) -> bool:
        default = f"{self.project.name}.vproj"
        path, _ = QFileDialog.getSaveFileName(self, "Save Project As", default, PROJECT_FILE_FILTER)
        if not path:
            return False
        if not path.endswith(".vproj"):
            path += ".vproj"
        return self._save_to(path)

    def _save_to(self, path: str) -> bool:
        # The previous version of the file survives as <name>.vproj.bak (R2).
        write_backup_copy(path)
        try:
            self.project.save(path)  # atomic: temp file + os.replace
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Save Project", f"Could not save project:\n{exc}")
            return False
        self._dirty = False
        self._update_title()
        self._clear_autosave()
        self._add_recent_project(path)
        self.status_bar.showMessage(f"Saved to {path}", 3000)
        return True

    # ------------------------------------------------------------------
    # Autosave / crash recovery
    # ------------------------------------------------------------------
    def _autosave(self) -> None:
        """Best-effort periodic snapshot so a crash doesn't lose everything.

        Writes to this session's own recovery slot (core/autosave.py: one per
        running editor, PID-locked, last 3 generations kept) rather than the
        project's own file, so autosaving never overwrites the user's
        deliberately saved version and two running editors never clobber
        each other's recovery files.
        """
        if not self._dirty:
            return
        try:
            self._autosave_session.write(
                self.project.to_dict(), {"original_path": self.project.file_path, "name": self.project.name}
            )
        except (OSError, TypeError, ValueError):
            pass  # autosaving is a courtesy; never interrupt the user for it
        else:
            self._discard_recovery_source()

    def _clear_autosave(self) -> None:
        self._autosave_session.clear()
        self._discard_recovery_source()

    def _discard_recovery_source(self) -> None:
        source = getattr(self, "_recovery_source", None)
        if source is not None:
            source.discard()
            self._recovery_source = None
