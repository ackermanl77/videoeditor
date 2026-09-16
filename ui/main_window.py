"""The application's main window: menus, toolbar, panels and wiring."""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QThread, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QStatusBar,
    QToolBar,
    QWidget,
)

from core.clip import Clip
from core.history import History
from core.media_asset import MediaAsset
from core.project import Project
from core.track import TrackType
from core.waveform import load_waveform
from ui.export_dialog import ExportDialog
from ui.import_worker import ImportWorker
from ui.media_bin import MediaBinWidget
from ui.preview_player import PreviewPlayer
from ui.properties_panel import PropertiesPanel
from ui.widgets.timeline_widget import TimelineWidget

APP_NAME = "Open Video Editor"
PROJECT_FILE_FILTER = "Video Editor Project (*.vproj)"

AUTOSAVE_INTERVAL_MS = 2 * 60 * 1000

WaveformData = Tuple[List[float], float]


def _cache_dir() -> str:
    path = os.path.join(os.path.expanduser("~"), ".video_editor", "thumbnails")
    os.makedirs(path, exist_ok=True)
    return path


def _waveform_cache_dir() -> str:
    path = os.path.join(os.path.expanduser("~"), ".video_editor", "waveforms")
    os.makedirs(path, exist_ok=True)
    return path


def _autosave_dir() -> str:
    path = os.path.join(os.path.expanduser("~"), ".video_editor", "autosave")
    os.makedirs(path, exist_ok=True)
    return path


def _autosave_path() -> str:
    return os.path.join(_autosave_dir(), "autosave.vproj")


def _autosave_meta_path() -> str:
    return _autosave_path() + ".meta.json"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.project = Project()
        self.history = History(on_change=self._on_history_changed)
        self._dirty = False
        self._syncing_playhead = False
        self._thumb_pixmap_cache: Dict[str, QPixmap] = {}
        self._waveform_cache: Dict[str, Optional[WaveformData]] = {}
        self._icons_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "icons")

        self._import_thread: Optional[QThread] = None
        self._import_worker: Optional[ImportWorker] = None
        self._import_errors: List[str] = []

        icon_path = os.path.join(self._icons_dir, "app_icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.setMinimumSize(1200, 760)
        self._build_ui()
        self._build_menu()
        self._build_toolbar()
        self._connect_signals()
        self._load_project_into_panels()
        self._update_title()

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(AUTOSAVE_INTERVAL_MS)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start()

        # Deferred so the window is fully shown before a possible recovery dialog pops up.
        QTimer.singleShot(0, self._offer_autosave_recovery)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.media_bin = MediaBinWidget()
        self.preview = PreviewPlayer()
        self.properties = PropertiesPanel()
        self.timeline = TimelineWidget()

        # Give the four main panels a shared "card" identity (a bordered
        # panel, see ui/theme.py's QWidget#Panel rule) so the window reads
        # as a set of distinct panels rather than one flat gray surface.
        for panel in (self.media_bin, self.preview, self.properties, self.timeline):
            panel.setObjectName("Panel")
            # Plain QWidget subclasses don't paint stylesheet backgrounds/
            # borders by default (only QFrame does) — this attribute makes
            # the QWidget#Panel rule in ui/theme.py actually render.
            panel.setAttribute(Qt.WA_StyledBackground, True)

        top_splitter = QSplitter(Qt.Horizontal)
        top_splitter.addWidget(self.media_bin)
        top_splitter.addWidget(self.preview)
        top_splitter.addWidget(self.properties)
        top_splitter.setStretchFactor(0, 2)
        top_splitter.setStretchFactor(1, 4)
        top_splitter.setStretchFactor(2, 2)

        main_splitter = QSplitter(Qt.Vertical)
        main_splitter.addWidget(top_splitter)
        main_splitter.addWidget(self.timeline)
        main_splitter.setStretchFactor(0, 3)
        main_splitter.setStretchFactor(1, 2)

        self.setCentralWidget(main_splitter)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.import_progress = QProgressBar()
        self.import_progress.setFixedWidth(160)
        self.import_progress.setVisible(False)
        self.status_bar.addPermanentWidget(self.import_progress)
        self.time_status_label = QLabel("00:00:00 / 00:00:00")
        self.status_bar.addPermanentWidget(self.time_status_label)

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("&File")
        self._add_action(file_menu, "New Project", self.new_project, QKeySequence.New)
        self._add_action(file_menu, "Open Project...", self.open_project, QKeySequence.Open)
        self._add_action(file_menu, "Save Project", self.save_project, QKeySequence.Save, icon="save")
        self._add_action(file_menu, "Save Project As...", self.save_project_as, QKeySequence.SaveAs)
        file_menu.addSeparator()
        self._add_action(file_menu, "Import Media...", self.import_media_dialog, "Ctrl+I", icon="import")
        self._add_action(file_menu, "Export Video...", self.open_export_dialog, "Ctrl+E", icon="export")
        file_menu.addSeparator()
        self._add_action(file_menu, "Exit", self.close, "Ctrl+Q")

        edit_menu = menu_bar.addMenu("&Edit")
        self.undo_action = self._add_action(edit_menu, "Undo", self.undo, QKeySequence.Undo, icon="undo")
        self.redo_action = self._add_action(edit_menu, "Redo", self.redo, QKeySequence.Redo, icon="redo")
        edit_menu.addSeparator()
        self._add_action(edit_menu, "Split Clip at Playhead", self.timeline.split_selected, "S", icon="split")
        self._add_action(edit_menu, "Delete Selected Clip", self.timeline.delete_selected, QKeySequence.Delete, icon="delete")
        self._add_action(edit_menu, "Add Text Clip", self.add_text_clip, "T", icon="text")

        timeline_menu = menu_bar.addMenu("&Timeline")
        self._add_action(timeline_menu, "Add Marker at Playhead", self.timeline.add_marker_at_playhead, "M")
        timeline_menu.addSeparator()
        self._add_action(timeline_menu, "Add Video Track", lambda: self.timeline.add_track(TrackType.VIDEO))
        self._add_action(timeline_menu, "Add Audio Track", lambda: self.timeline.add_track(TrackType.AUDIO))
        self._add_action(timeline_menu, "Zoom In", self.timeline.zoom_in, "Ctrl+=")
        self._add_action(timeline_menu, "Zoom Out", self.timeline.zoom_out, "Ctrl+-")

        help_menu = menu_bar.addMenu("&Help")
        self._add_action(help_menu, "About", self.show_about)

    def _icon(self, name: str) -> QIcon:
        path = os.path.join(self._icons_dir, "toolbar", f"{name}.png")
        return QIcon(path) if os.path.exists(path) else QIcon()

    def _add_action(self, menu, text: str, slot, shortcut=None, icon: Optional[str] = None) -> QAction:
        action = QAction(self._icon(icon), text, self) if icon else QAction(text, self)
        if shortcut:
            action.setShortcut(shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _build_brand_widget(self) -> QWidget:
        """Small app-icon + name lockup shown at the left of the toolbar."""
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(6, 0, 12, 0)
        layout.setSpacing(6)

        icon_label = QLabel()
        icon_path = os.path.join(self._icons_dir, "app_icon.png")
        if os.path.exists(icon_path):
            icon_label.setPixmap(QPixmap(icon_path).scaled(22, 22, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(icon_label)

        name_label = QLabel(APP_NAME)
        name_label.setStyleSheet("font-weight: 600; font-size: 13px; color: #e6e8ec;")
        layout.addWidget(name_label)
        return holder

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main Toolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(toolbar.iconSize() * 1.1)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        toolbar.addWidget(self._build_brand_widget())
        toolbar.addSeparator()
        toolbar.addAction(self._icon("import"), "Import", self.import_media_dialog)
        toolbar.addSeparator()
        toolbar.addAction(self._icon("save"), "Save", self.save_project)
        toolbar.addSeparator()
        self.toolbar_undo = toolbar.addAction(self._icon("undo"), "Undo", self.undo)
        self.toolbar_redo = toolbar.addAction(self._icon("redo"), "Redo", self.redo)
        toolbar.addSeparator()
        toolbar.addAction(self._icon("split"), "Split", self.timeline.split_selected)
        toolbar.addAction(self._icon("delete"), "Delete", self.timeline.delete_selected)
        toolbar.addAction(self._icon("text"), "Add Text", self.add_text_clip)
        toolbar.addSeparator()
        self.play_action = toolbar.addAction(self._icon("play"), "Play", self.preview.toggle_play)
        self.play_action.setShortcut("Space")
        toolbar.addSeparator()
        export_action = toolbar.addAction(self._icon("export"), "Export Video", self.open_export_dialog)
        export_action.setShortcut("Ctrl+E")

    def _on_playing_changed(self, is_playing: bool) -> None:
        self.play_action.setIcon(self._icon("pause" if is_playing else "play"))
        self.play_action.setText("Pause" if is_playing else "Play")

    def _connect_signals(self) -> None:
        self.media_bin.import_requested.connect(self.import_media_paths)
        self.media_bin.add_to_timeline_requested.connect(self._add_asset_to_timeline_end)

        self.timeline.selection_changed.connect(self._on_selection_changed)
        self.timeline.project_modified.connect(self._on_project_modified)
        self.timeline.playhead_changed.connect(self._on_timeline_playhead_changed)
        self.timeline.get_thumbnail_for_clip = self._thumbnail_for_clip
        self.timeline.get_waveform_for_clip = self._waveform_for_clip

        self.properties.project_modified.connect(self._on_project_modified)

        self.preview.time_changed.connect(self._on_preview_time_changed)
        self.preview.playing_changed.connect(self._on_playing_changed)

    # ------------------------------------------------------------------
    def _load_project_into_panels(self) -> None:
        self.timeline.set_project(self.project, self.history)
        self.preview.set_project(self.project)
        self.properties.set_project(self.project, self.history)
        self.media_bin.set_assets(self.project.media_pool, self._thumbnail_for_asset)
        self.history.clear()
        self._dirty = False
        self._update_title()

    # ------------------------------------------------------------------
    # Media import
    # ------------------------------------------------------------------
    def import_media_dialog(self) -> None:
        from ui.media_bin import SUPPORTED_EXTENSIONS

        paths, _ = QFileDialog.getOpenFileNames(self, "Import Media", "", f"Media Files ({SUPPORTED_EXTENSIONS});;All Files (*)")
        if paths:
            self.import_media_paths(paths)

    def import_media_paths(self, paths) -> None:
        """Probe/thumbnail/waveform each path on a background thread.

        This used to run inline on the UI thread (papered over with
        ``processEvents()``), which visibly froze the app on a batch of
        large files. The heavy lifting now happens in :class:`ImportWorker`
        on its own ``QThread``; only the final project/UI updates happen
        back on the main thread via the signal handlers below.
        """
        new_paths = [p for p in paths if self.project.find_asset_by_path(p) is None]
        if not new_paths:
            return
        if self._import_thread is not None and self._import_thread.isRunning():
            QMessageBox.information(self, "Import Media", "An import is already in progress. Please wait for it to finish.")
            return

        self._import_errors = []
        self.import_progress.setRange(0, len(new_paths))
        self.import_progress.setValue(0)
        self.import_progress.setVisible(True)
        self.status_bar.showMessage(f"Importing {len(new_paths)} file(s)...")

        self._import_thread = QThread(self)
        self._import_worker = ImportWorker(new_paths, _cache_dir(), _waveform_cache_dir())
        self._import_worker.moveToThread(self._import_thread)

        self._import_thread.started.connect(self._import_worker.run)
        self._import_worker.asset_ready.connect(self._on_asset_imported)
        self._import_worker.failed.connect(self._on_import_failed)
        self._import_worker.finished.connect(self._on_import_finished)
        self._import_worker.finished.connect(self._import_thread.quit)

        self._import_thread.start()

    def _on_asset_imported(self, asset: MediaAsset) -> None:
        self.project.add_media_asset(asset)
        self.import_progress.setValue(self.import_progress.value() + 1)
        self.media_bin.set_assets(self.project.media_pool, self._thumbnail_for_asset)
        self._mark_dirty()

    def _on_import_failed(self, path: str, message: str) -> None:
        self._import_errors.append(f"{os.path.basename(path)}: {message}")
        self.import_progress.setValue(self.import_progress.value() + 1)

    def _on_import_finished(self) -> None:
        self.import_progress.setVisible(False)
        self.status_bar.showMessage("Import complete.", 3000)
        self._import_thread = None
        self._import_worker = None
        if self._import_errors:
            QMessageBox.warning(
                self,
                "Import Media",
                "Some files could not be imported:\n\n" + "\n".join(self._import_errors),
            )
            self._import_errors = []

    def _add_asset_to_timeline_end(self, asset_id: str) -> None:
        asset = self.project.find_asset(asset_id)
        if asset is not None:
            self.timeline.add_media_to_timeline(asset)

    def _thumbnail_for_asset(self, asset: MediaAsset) -> Optional[QPixmap]:
        if asset.id in self._thumb_pixmap_cache:
            return self._thumb_pixmap_cache[asset.id]
        if not asset.thumbnail_path or not os.path.exists(asset.thumbnail_path):
            return None
        pixmap = QPixmap(asset.thumbnail_path)
        self._thumb_pixmap_cache[asset.id] = pixmap
        return pixmap

    def _thumbnail_for_clip(self, clip: Clip) -> Optional[QPixmap]:
        asset = self.project.find_asset_by_path(clip.source_path)
        if asset is None:
            return None
        return self._thumbnail_for_asset(asset)

    def _waveform_for_clip(self, clip: Clip) -> Optional[WaveformData]:
        asset = self.project.find_asset_by_path(clip.source_path)
        if asset is None or not asset.waveform_path:
            return None
        if asset.id in self._waveform_cache:
            return self._waveform_cache[asset.id]
        data = load_waveform(asset.waveform_path)
        self._waveform_cache[asset.id] = data
        return data

    def add_text_clip(self) -> None:
        # TimelineWidget.add_text_clip() already selects and rebuilds; the
        # selection_changed signal (connected in _connect_signals) updates
        # the properties panel for us.
        self.timeline.add_text_clip()

    # ------------------------------------------------------------------
    # Selection / sync
    # ------------------------------------------------------------------
    def _on_selection_changed(self, clip_id) -> None:
        self.properties.set_selected_clip(clip_id)

    def _on_project_modified(self) -> None:
        self._mark_dirty()
        self.preview.seek_to(self.preview.current_time)
        self._update_time_status()

    def _on_timeline_playhead_changed(self, t: float) -> None:
        if self._syncing_playhead:
            return
        self._syncing_playhead = True
        self.preview.seek_to(t)
        self._syncing_playhead = False
        self._update_time_status()

    def _on_preview_time_changed(self, t: float) -> None:
        if self._syncing_playhead:
            return
        self._syncing_playhead = True
        self.timeline.set_playhead(t)
        self._syncing_playhead = False
        self._update_time_status()

    def _update_time_status(self) -> None:
        self.time_status_label.setText(
            f"{PreviewPlayer.format_time(self.preview.current_time)} / {PreviewPlayer.format_time(self.project.total_duration)}"
        )

    # ------------------------------------------------------------------
    # Undo / redo
    # ------------------------------------------------------------------
    def undo(self) -> None:
        label = self.history.undo()
        self.timeline.rebuild()
        self.properties.set_selected_clip(self.timeline.selected_clip_id)
        self._on_project_modified()
        if label:
            self.status_bar.showMessage(f"Undo: {label}", 2000)

    def redo(self) -> None:
        label = self.history.redo()
        self.timeline.rebuild()
        self.properties.set_selected_clip(self.timeline.selected_clip_id)
        self._on_project_modified()
        if label:
            self.status_bar.showMessage(f"Redo: {label}", 2000)

    def _on_history_changed(self) -> None:
        self.undo_action.setEnabled(self.history.can_undo)
        self.redo_action.setEnabled(self.history.can_redo)
        self.toolbar_undo.setEnabled(self.history.can_undo)
        self.toolbar_redo.setEnabled(self.history.can_redo)
        self._mark_dirty()

    # ------------------------------------------------------------------
    # Project file management
    # ------------------------------------------------------------------
    def _mark_dirty(self) -> None:
        self._dirty = True
        self._update_title()

    def _update_title(self) -> None:
        name = self.project.name
        star = "*" if self._dirty else ""
        path = f" — {self.project.file_path}" if self.project.file_path else ""
        self.setWindowTitle(f"{star}{name}{path} — {APP_NAME}")

    def _confirm_discard_if_dirty(self) -> bool:
        if not self._dirty:
            return True
        result = QMessageBox.question(
            self,
            "Unsaved Changes",
            "You have unsaved changes. Discard them and continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        return result == QMessageBox.Yes

    def new_project(self) -> None:
        if not self._confirm_discard_if_dirty():
            return
        self._clear_autosave()
        self.project = Project()
        self.history = History(on_change=self._on_history_changed)
        self._thumb_pixmap_cache.clear()
        self._waveform_cache.clear()
        self._load_project_into_panels()

    def open_project(self) -> None:
        if not self._confirm_discard_if_dirty():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open Project", "", PROJECT_FILE_FILTER)
        if not path:
            return
        try:
            project = Project.load(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Open Project", f"Could not open project:\n{exc}")
            return
        self._clear_autosave()
        self.project = project
        self.history = History(on_change=self._on_history_changed)
        self._thumb_pixmap_cache.clear()
        self._waveform_cache.clear()
        self._load_project_into_panels()
        self._warn_about_missing_media()

    def _warn_about_missing_media(self) -> None:
        missing = sorted({a.path for a in self.project.media_pool if not os.path.isfile(a.path)})
        if not missing:
            return
        preview = "\n".join(missing[:10])
        if len(missing) > 10:
            preview += f"\n... and {len(missing) - 10} more"
        QMessageBox.warning(
            self,
            "Missing Media",
            "The following media files referenced by this project could not be found:\n\n"
            f"{preview}\n\n"
            "Affected clips will appear blank/silent until the files are restored at their "
            "original paths.",
        )

    def save_project(self) -> None:
        if self.project.file_path is None:
            self.save_project_as()
            return
        self._save_to(self.project.file_path)

    def save_project_as(self) -> None:
        default = f"{self.project.name}.vproj"
        path, _ = QFileDialog.getSaveFileName(self, "Save Project As", default, PROJECT_FILE_FILTER)
        if not path:
            return
        if not path.endswith(".vproj"):
            path += ".vproj"
        self._save_to(path)

    def _save_to(self, path: str) -> None:
        try:
            self.project.save(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Save Project", f"Could not save project:\n{exc}")
            return
        self._dirty = False
        self._update_title()
        self._clear_autosave()
        self.status_bar.showMessage(f"Saved to {path}", 3000)

    # ------------------------------------------------------------------
    # Autosave / crash recovery
    # ------------------------------------------------------------------
    def _autosave(self) -> None:
        """Best-effort periodic snapshot so a crash doesn't lose everything.

        Writes to a fixed recovery slot rather than the project's own file
        (which is only ever touched by an explicit Save), so autosaving
        never overwrites the user's last deliberately-saved version.
        """
        if not self._dirty:
            return
        try:
            with open(_autosave_path(), "w", encoding="utf-8") as f:
                json.dump(self.project.to_dict(), f)
            with open(_autosave_meta_path(), "w", encoding="utf-8") as f:
                json.dump({"original_path": self.project.file_path, "name": self.project.name}, f)
        except OSError:
            pass  # autosaving is a courtesy; never interrupt the user for it

    def _clear_autosave(self) -> None:
        for path in (_autosave_path(), _autosave_meta_path()):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    def _offer_autosave_recovery(self) -> None:
        autosave_path = _autosave_path()
        if not os.path.exists(autosave_path):
            return
        original_path = None
        name = "an unsaved project"
        try:
            with open(_autosave_meta_path(), "r", encoding="utf-8") as f:
                meta = json.load(f)
            original_path = meta.get("original_path")
            name = meta.get("name") or name
        except (OSError, json.JSONDecodeError):
            pass

        result = QMessageBox.question(
            self,
            "Recover Unsaved Work",
            f"It looks like {APP_NAME} closed unexpectedly while editing "
            f"'{name}' with unsaved changes.\n\nRecover that work now?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if result != QMessageBox.Yes:
            self._clear_autosave()
            return

        try:
            with open(autosave_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            project = Project.from_dict(data)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Recover Unsaved Work", f"Could not recover the autosave:\n{exc}")
            self._clear_autosave()
            return

        project.file_path = original_path
        self.project = project
        self.history = History(on_change=self._on_history_changed)
        self._thumb_pixmap_cache.clear()
        self._waveform_cache.clear()
        self._load_project_into_panels()
        self._mark_dirty()  # recovered state doesn't exist on disk at `original_path` yet
        self._warn_about_missing_media()

    # ------------------------------------------------------------------
    def open_export_dialog(self) -> None:
        if not self.project.tracks or self.project.total_duration <= 0:
            QMessageBox.information(self, "Export Video", "Add some clips to the timeline before exporting.")
            return
        dialog = ExportDialog(self.project, self)
        dialog.exec()

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<h3>{APP_NAME}</h3>"
            "<p>A lightweight, extensible non-linear video editor built with "
            "Python, PySide6 and FFmpeg.</p>"
            "<p>Import media, arrange it on a multi-track timeline, add text "
            "overlays, and export a finished video.</p>",
        )

    def closeEvent(self, event) -> None:
        if self._import_thread is not None and self._import_thread.isRunning():
            result = QMessageBox.question(
                self,
                "Import in Progress",
                "Media is still being imported. Quit anyway?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if result != QMessageBox.Yes:
                event.ignore()
                return

        if self._confirm_discard_if_dirty():
            self._autosave_timer.stop()
            # Either everything is saved, or the user just chose to discard
            # their changes — in both cases there's nothing to recover next launch.
            self._clear_autosave()
            event.accept()
        else:
            event.ignore()
