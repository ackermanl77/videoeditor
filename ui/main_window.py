"""The application's main window: menus, toolbar, panels and wiring."""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QByteArray, QSettings, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QIcon, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QToolBar,
    QWidget,
)

from core import __version__
from core.autosave import AutosaveSession, find_orphaned_autosaves
from core.clip import Clip, MediaType, ReframeMode
from core.commands import AddClipCommand, AddTrackCommand, UpdateClipPropertyCommand
from core.ffmpeg_utils import (
    DECODE_HWACCEL_CHOICES,
    LAUNCH_DECODE_HWACCEL,
    binary_version,
    encoder_supports_10bit,
    nvenc_available,
    set_decode_hwaccel,
)
from core.frame_render import FrameRenderError, full_size, render_frame
from core.history import CompositeCommand, History
from core.media_asset import MediaAsset
from core.project import Project, write_backup_copy
from core.proxy import generate_proxy, needs_proxy
from core.subtitles import SubtitleParseError, cues_to_text_clips, is_subtitle_path, parse_subtitle_file
from core.timeline_nav import format_timecode
from core.track import TrackType
from core.waveform import WaveformData, load_waveform
from ui.export_dialog import ExportDialog
from ui.help_dialogs import ShortcutsDialog, UserGuideDialog
from ui.import_worker import ImportWorker
from ui.relink_dialog import RelinkDialog
from ui.render_queue import RenderQueueWidget
from ui.media_bin import MediaBinWidget
from ui.preview_player import PreviewPlayer
from ui.properties_panel import PropertiesPanel
from ui.widgets.timeline_widget import TimelineWidget

APP_NAME = "Open Video Editor"
PROJECT_FILE_FILTER = "Video Editor Project (*.vproj)"

AUTOSAVE_INTERVAL_MS = 2 * 60 * 1000

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


AUTO_PROXY_SETTING = "media/auto_proxies"
GPU_DECODING_SETTING = "decode/hwaccel"


def _strict_mode() -> bool:
    """``VIDEO_EDITOR_STRICT=1``: validate the project after every edit (see core.history.History)."""
    return os.environ.get("VIDEO_EDITOR_STRICT", "") not in ("", "0")


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
        with open(_recent_projects_path(), "w", encoding="utf-8") as f:
            json.dump(paths, f, indent=2)
    except OSError:
        pass


class MainWindow(QMainWindow):
    # (asset id, proxy path or None) from a proxy-encoding thread.
    _proxy_finished = Signal(str, object)

    def __init__(self):
        super().__init__()
        self.project = Project()
        self.history = self._new_history()
        # Changes outside History (imported media, proxies, recovered work).
        self._media_dirty = False
        self._autosave_session = AutosaveSession(_autosave_dir())
        self._export_dialogs: List[ExportDialog] = []
        self._proxy_jobs: set = set()
        self._syncing_playhead = False
        self._thumb_pixmap_cache: Dict[str, QPixmap] = {}
        self._filmstrip_pixmap_cache: Dict[str, QPixmap] = {}
        self._waveform_cache: Dict[str, Optional[WaveformData]] = {}
        self._icons_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "icons")

        self._import_thread: Optional[QThread] = None
        self._import_worker: Optional[ImportWorker] = None
        self._import_errors: List[str] = []
        self._proxy_finished.connect(self._on_proxy_finished)

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

        # Warm core.ffmpeg_utils.nvenc_available()'s cache off the UI thread:
        # it now does a real (~1-2 s) NVENC test encode, and the Export
        # dialog would otherwise block on it the first time it opens.
        threading.Thread(target=self._warm_encoder_probes, name="encoder-probe", daemon=True).start()

        self._restore_window_state()

        # Deferred so the window is fully shown before a possible recovery dialog pops up.
        QTimer.singleShot(0, self._offer_autosave_recovery)

    @staticmethod
    def _warm_encoder_probes() -> None:
        """Fill the NVENC / 10-bit encoder probe caches the Export dialog reads (each is a real test encode)."""
        nvenc_available()
        for encoder in ("libx265", "libaom-av1", "prores_ks", "dnxhd", "hevc_nvenc", "av1_nvenc"):
            encoder_supports_10bit(encoder)

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

        self.top_splitter = QSplitter(Qt.Horizontal)
        self.top_splitter.addWidget(self.media_bin)
        self.top_splitter.addWidget(self.preview)
        self.top_splitter.addWidget(self.properties)
        self.top_splitter.setStretchFactor(0, 2)
        self.top_splitter.setStretchFactor(1, 4)
        self.top_splitter.setStretchFactor(2, 2)
        # A splitter's default lets a handle be dragged all the way to an
        # edge, shrinking a panel to 0px and effectively hiding it with no
        # menu item or shortcut to bring it back. Panels can still be made
        # very small, just never fully collapsed/lost.
        self.top_splitter.setChildrenCollapsible(False)

        self.main_splitter = QSplitter(Qt.Vertical)
        self.main_splitter.addWidget(self.top_splitter)
        self.main_splitter.addWidget(self.timeline)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 2)
        self.main_splitter.setChildrenCollapsible(False)

        self.setCentralWidget(self.main_splitter)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.import_progress = QProgressBar()
        self.import_progress.setFixedWidth(160)
        self.import_progress.setVisible(False)
        self.status_bar.addPermanentWidget(self.import_progress)
        self.import_cancel_btn = QPushButton("Cancel Import")
        self.import_cancel_btn.setVisible(False)
        self.import_cancel_btn.clicked.connect(self.cancel_import)
        self.status_bar.addPermanentWidget(self.import_cancel_btn)
        self.time_status_label = QLabel("00:00:00:00 / 00:00:00:00")
        self.status_bar.addPermanentWidget(self.time_status_label)

        # Render queue (F4): a dock, hidden until something is queued.
        self.render_queue = RenderQueueWidget()
        self.render_queue.job_finished.connect(self._on_queue_job_finished)
        self.render_queue_dock = QDockWidget("Render Queue", self)
        self.render_queue_dock.setObjectName("RenderQueueDock")
        self.render_queue_dock.setWidget(self.render_queue)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.render_queue_dock)
        self.render_queue_dock.hide()

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        # Actions that also appear on the toolbar are created once, here, and
        # the toolbar reuses the same QAction objects (see _build_toolbar):
        # two actions with one key sequence (Export's Ctrl+E used to be on
        # both) is an "ambiguous shortcut" in Qt and neither fires.
        file_menu = menu_bar.addMenu("&File")
        self._add_action(file_menu, "New Project", self.new_project, QKeySequence.New)
        self._add_action(file_menu, "Open Project...", self.open_project, QKeySequence.Open)
        self.recent_projects_menu = file_menu.addMenu("Open Recent")
        self._refresh_recent_projects_menu()
        self.save_action = self._add_action(file_menu, "Save Project", self.save_project, QKeySequence.Save, icon="save")
        self._add_action(file_menu, "Save Project As...", self.save_project_as, QKeySequence.SaveAs)
        file_menu.addSeparator()
        self.import_action = self._add_action(file_menu, "Import Media...", self.import_media_dialog, "Ctrl+I", icon="import")
        self.import_subtitles_action = self._add_action(file_menu, "Import Subtitles...", self.import_subtitles_dialog)
        self.relink_action = self._add_action(file_menu, "Relink Media...", self.open_relink_dialog)
        self.export_action = self._add_action(file_menu, "Export Video...", self.open_export_dialog, "Ctrl+E", icon="export")
        self.export_frame_action = self._add_action(file_menu, "Export Frame...", self.export_frame_dialog, "Ctrl+Alt+E")
        self.render_queue_action = self.render_queue_dock.toggleViewAction()
        self.render_queue_action.setText("Render Queue")
        self.render_queue_action.setShortcut(QKeySequence("Ctrl+Shift+E"))
        file_menu.addAction(self.render_queue_action)
        file_menu.addSeparator()
        self._add_action(file_menu, "Exit", self.close, "Ctrl+Q")

        edit_menu = menu_bar.addMenu("&Edit")
        self.undo_action = self._add_action(edit_menu, "Undo", self.undo, QKeySequence.Undo, icon="undo")
        self.redo_action = self._add_action(edit_menu, "Redo", self.redo, QKeySequence.Redo, icon="redo")
        edit_menu.addSeparator()
        self._add_action(edit_menu, "Cut", self.timeline.cut_selected, QKeySequence.Cut)
        self._add_action(edit_menu, "Copy", self.timeline.copy_selected, QKeySequence.Copy)
        self._add_action(edit_menu, "Paste at Playhead", self.timeline.paste_at_playhead, QKeySequence.Paste)
        self._add_action(edit_menu, "Paste Attributes", self.timeline.paste_attributes, "Ctrl+Alt+V")
        self._add_action(edit_menu, "Duplicate", self.timeline.duplicate_selected, "Ctrl+D")
        self._add_action(edit_menu, "Select All Clips", self.timeline.select_all, QKeySequence.SelectAll)
        edit_menu.addSeparator()
        self.split_action = self._add_action(edit_menu, "Split Clip at Playhead", self.timeline.split_selected, "S", icon="split")
        self.delete_action = self._add_action(edit_menu, "Delete Selected Clips", self.timeline.delete_selected, QKeySequence.Delete, icon="delete")
        self.text_action = self._add_action(edit_menu, "Add Text Clip", self.add_text_clip, "T", icon="text")
        edit_menu.addSeparator()
        self._add_action(edit_menu, "Insert Selected Media at Playhead", lambda: self._place_selected_asset(False), ",")
        self._add_action(edit_menu, "Overwrite with Selected Media at Playhead", lambda: self._place_selected_asset(True), ".")
        self._add_action(edit_menu, "Detach Audio", self.timeline.detach_audio_selected, "Ctrl+Shift+D")
        self._add_action(edit_menu, "Roll Edit to Playhead", self.timeline.roll_selected_to_playhead, "Ctrl+R")
        self._add_action(edit_menu, "Slip Clip...", self.timeline.slip_selected)
        self._add_action(edit_menu, "Slide Clip...", self.timeline.slide_selected)
        self._add_action(edit_menu, "Ripple Move Clip...", self.timeline.ripple_move_selected)
        self.razor_action = self._add_action(edit_menu, "Razor Tool", self.timeline.set_razor_mode, "C")
        self.razor_action.setCheckable(True)

        timeline_menu = menu_bar.addMenu("&Timeline")
        self._add_action(timeline_menu, "Add Marker at Playhead", self.timeline.add_marker_at_playhead, "M")
        timeline_menu.addSeparator()
        self._add_action(timeline_menu, "Add Video Track", lambda: self.timeline.add_track(TrackType.VIDEO))
        self._add_action(timeline_menu, "Add Audio Track", lambda: self.timeline.add_track(TrackType.AUDIO))
        self._add_action(timeline_menu, "Zoom In", self.timeline.zoom_in, "Ctrl+=")
        self._add_action(timeline_menu, "Zoom Out", self.timeline.zoom_out, "Ctrl+-")
        self._add_action(timeline_menu, "Zoom to Fit", self.timeline.zoom_to_fit, "Shift+Z")

        self._build_playback_menu(menu_bar.addMenu("&Playback"))

        media_menu = menu_bar.addMenu("&Media")
        self._add_action(media_menu, "Generate Proxy for Selected Media", self._proxy_for_selected_asset)
        self.auto_proxy_action = self._add_action(
            media_menu, "Generate Proxies Automatically (>1080p, 10-bit, HDR)", self._on_auto_proxy_toggled
        )
        self.auto_proxy_action.setCheckable(True)
        self.auto_proxy_action.setChecked(self._auto_proxies_enabled())
        self._add_action(media_menu, "Cancel Import", self.cancel_import)

        help_menu = menu_bar.addMenu("&Help")
        self._add_action(help_menu, "Keyboard Shortcuts", self.show_shortcuts, "F1")
        self._add_action(help_menu, "User Guide", self.show_user_guide)
        self._add_action(help_menu, "About", self.show_about)

    def _build_playback_menu(self, menu) -> None:
        """Keyboard transport: J/K/L shuttle, frame/second steps, edit/marker navigation, In/Out.

        Letter keys are window-wide (text fields still receive typed
        letters -- they claim printable keys first). Arrow/Home/End/Up/Down
        are scoped to the timeline and preview (``WidgetWithChildrenShortcut``)
        so they don't steal arrow keys from the Properties panel's sliders and
        spin boxes; their menu entries show the key but don't register it.
        """
        self._add_action(menu, "Shuttle Reverse", lambda: self.preview.shuttle(-1), "J")
        self._add_action(menu, "Stop Shuttle", self.preview.stop_shuttle, "K")
        self._add_action(menu, "Shuttle Forward", lambda: self.preview.shuttle(1), "L")
        menu.addSeparator()
        scoped = (
            ("Previous Frame", "Left", lambda: self.timeline.step_frames(-1)),
            ("Next Frame", "Right", lambda: self.timeline.step_frames(1)),
            ("Back 1 Second", "Shift+Left", lambda: self.timeline.step_seconds(-1.0)),
            ("Forward 1 Second", "Shift+Right", lambda: self.timeline.step_seconds(1.0)),
            ("Go to Start", "Home", self.timeline.go_to_start),
            ("Go to End", "End", self.timeline.go_to_end),
            ("Previous Edit Point", "Up", lambda: self.timeline.go_to_edit_point(-1)),
            ("Next Edit Point", "Down", lambda: self.timeline.go_to_edit_point(1)),
            ("Nudge Selection Left 1 Frame", "Alt+Left", lambda: self.timeline.nudge_selected(-1)),
            ("Nudge Selection Right 1 Frame", "Alt+Right", lambda: self.timeline.nudge_selected(1)),
            ("Select Next Clip", "Tab", lambda: self.timeline.select_adjacent_clip(1)),
            ("Select Previous Clip", "Shift+Tab", lambda: self.timeline.select_adjacent_clip(-1)),
            ("Edit Selected Clip's Properties", "Return", self.focus_properties),
        )
        self.transport_actions: List[QAction] = []
        for text, key, slot in scoped:
            shortcut_action = QAction(text, self)
            shortcut_action.setShortcut(QKeySequence(key))
            shortcut_action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            shortcut_action.triggered.connect(slot)
            self.timeline.addAction(shortcut_action)
            self.preview.addAction(shortcut_action)
            self.transport_actions.append(shortcut_action)
            menu_action = menu.addAction(f"{text}\t{key}")  # display-only key hint
            menu_action.triggered.connect(slot)
        menu.addSeparator()
        self._add_action(menu, "Previous Marker", lambda: self.timeline.go_to_marker(-1), "Shift+M")
        self._add_action(menu, "Next Marker", lambda: self.timeline.go_to_marker(1), "Ctrl+Shift+M")
        self._add_action(menu, "Previous Keyframe", lambda: self.timeline.go_to_keyframe(-1), "Shift+K")
        self._add_action(menu, "Next Keyframe", lambda: self.timeline.go_to_keyframe(1), "Ctrl+Shift+K")
        menu.addSeparator()
        self._add_action(menu, "Set In Point", self.timeline.set_in_point, "I")
        self._add_action(menu, "Set Out Point", self.timeline.set_out_point, "O")
        self._add_action(menu, "Clear In/Out", self.timeline.clear_in_out, "Alt+X")
        menu.addSeparator()
        self._build_gpu_decoding_menu(menu.addMenu("GPU Decoding"))

    def _build_gpu_decoding_menu(self, menu) -> None:
        """PG2: GPU (hardware) decoding for export, preview frames, proxies and thumbnails --
        opt-in, always falling back to software. Remembered; VIDEO_EDITOR_HWACCEL set outside
        the app wins (the menu then says so and is disabled)."""
        self.gpu_decoding_actions = {}
        group = QActionGroup(self)
        group.setExclusive(True)
        external = LAUNCH_DECODE_HWACCEL is not None
        current = (LAUNCH_DECODE_HWACCEL if external else QSettings().value(GPU_DECODING_SETTING, "off", type=str)) or "off"
        current = current.strip().lower()
        if current not in DECODE_HWACCEL_CHOICES:
            current = "off"
        if not external:
            set_decode_hwaccel(current)
        for mode, label in DECODE_HWACCEL_CHOICES.items():
            if mode == "d3d11va" and not sys.platform.startswith("win"):
                continue
            action = QAction(label, self, checkable=True)
            action.setChecked(mode == current)
            action.setEnabled(not external)
            action.triggered.connect(lambda _checked=False, m=mode: self.set_gpu_decoding(m))
            group.addAction(action)
            menu.addAction(action)
            self.gpu_decoding_actions[mode] = action
        menu.setToolTipsVisible(True)
        menu.setToolTip(
            f"Set by VIDEO_EDITOR_HWACCEL={LAUNCH_DECODE_HWACCEL} outside the app." if external else
            "Decode video on the GPU first (heavy HEVC/AV1 sources); software decoding is always the fallback."
        )

    def set_gpu_decoding(self, mode: str) -> None:
        set_decode_hwaccel(mode)
        QSettings().setValue(GPU_DECODING_SETTING, mode)
        if mode in self.gpu_decoding_actions:
            self.gpu_decoding_actions[mode].setChecked(True)
        self.status_bar.showMessage(f"GPU decoding: {DECODE_HWACCEL_CHOICES[mode]}", 4000)

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
        # The same QAction objects as the menus (one shortcut registration
        # each); iconText is the shorter toolbar label.
        for action, icon_text, tooltip in (
            (self.import_action, "Import", "Import media files (Ctrl+I)"),
            (None, None, None),
            (self.save_action, "Save", "Save the current project (Ctrl+S)"),
            (None, None, None),
            (self.undo_action, "Undo", "Undo the last action (Ctrl+Z)"),
            (self.redo_action, "Redo", "Redo the last undone action (Ctrl+Y)"),
            (None, None, None),
            (self.split_action, "Split", "Split the selected clip at the playhead (S)"),
            (self.delete_action, "Delete", "Delete the selected clip (Delete)"),
            (self.text_action, "Add Text", "Add a text clip at the playhead (T)"),
        ):
            if action is None:
                toolbar.addSeparator()
                continue
            action.setIconText(icon_text)
            action.setToolTip(tooltip)
            toolbar.addAction(action)
        toolbar.addSeparator()
        self.play_action = toolbar.addAction(self._icon("play"), "Play", self.preview.toggle_play)
        self.play_action.setShortcut("Space")
        self.play_action.setToolTip("Play (Space)")
        toolbar.addSeparator()
        self.export_action.setIconText("Export Video")
        self.export_action.setToolTip("Export the finished video (Ctrl+E)")
        toolbar.addAction(self.export_action)

    def _on_playing_changed(self, is_playing: bool) -> None:
        self.play_action.setIcon(self._icon("pause" if is_playing else "play"))
        self.play_action.setText("Pause" if is_playing else "Play")
        self.play_action.setToolTip("Pause (Space)" if is_playing else "Play (Space)")

    def _connect_signals(self) -> None:
        self.media_bin.import_requested.connect(self.import_media_paths)
        self.media_bin.add_to_timeline_requested.connect(self._add_asset_to_timeline_end)
        self.media_bin.proxy_requested.connect(self.generate_proxy_for)
        self.media_bin.relink_requested.connect(self.open_relink_dialog)
        self.timeline.status_message.connect(lambda message: self.status_bar.showMessage(message, 5000))
        self.timeline.reveal_asset_requested.connect(self._reveal_asset)

        self.timeline.selection_changed.connect(self._on_selection_changed)
        self.timeline.project_modified.connect(self._on_project_modified)
        self.timeline.playhead_changed.connect(self._on_timeline_playhead_changed)
        self.timeline.get_thumbnail_for_clip = self._thumbnail_for_clip
        self.timeline.get_filmstrip_for_clip = self._filmstrip_for_clip
        self.timeline.get_waveform_for_clip = self._waveform_for_clip

        self.properties.project_modified.connect(self._on_project_modified)
        self.properties.clip_changed.connect(self.timeline.refresh_clip)
        self.properties.status_message.connect(lambda message: self.status_bar.showMessage(message, 5000))

        self.preview.time_changed.connect(self._on_preview_time_changed)
        self.preview.playing_changed.connect(self._on_playing_changed)
        self.preview.transform_edited.connect(self._apply_canvas_transform)

    # ------------------------------------------------------------------
    def _load_project_into_panels(self) -> None:
        self.timeline.set_project(self.project, self.history)
        self.preview.set_project(self.project)
        self.properties.set_project(self.project, self.history)
        self.timeline.get_thumbnail_for_clip = self._thumbnail_for_clip
        self.timeline.get_filmstrip_for_clip = self._filmstrip_for_clip
        self.timeline.get_waveform_for_clip = self._waveform_for_clip
        self.media_bin.set_assets(self.project.media_pool, self._thumbnail_for_asset)
        self.history.clear()
        self.history.mark_clean()
        self._media_dirty = False
        self._update_title()

    def _new_history(self) -> History:
        validator = (lambda: self.project.validate()) if _strict_mode() else None
        return History(on_change=self._on_history_changed, validator=validator)

    @property
    def _dirty(self) -> bool:
        """Unsaved changes: an undo position other than the saved one, or media changes."""
        return self._media_dirty or not self.history.is_clean

    @_dirty.setter
    def _dirty(self, value: bool) -> None:
        self._media_dirty = bool(value)
        if not value:
            self.history.mark_clean()

    # ------------------------------------------------------------------
    # Media import
    # ------------------------------------------------------------------
    def import_media_dialog(self) -> None:
        from ui.media_bin import SUPPORTED_EXTENSIONS

        paths, _ = QFileDialog.getOpenFileNames(self, "Import Media", "", f"Media Files ({SUPPORTED_EXTENSIONS});;All Files (*)")
        if paths:
            self.import_media_paths(paths)

    def import_subtitles_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Subtitles",
            "",
            "Subtitle Files (*.srt *.vtt *.ass *.ssa);;All Files (*)",
        )
        if path:
            self.import_subtitle_file(path)

    def import_subtitle_file(self, path: str) -> int:
        """Add an SRT/WebVTT file's cues as text clips on a "Subtitles" video track (one undo step).

        Reuses the existing "Subtitles" track when the new cues fit on it
        (no overlap, not locked); otherwise adds "Subtitles 2", "Subtitles 3"...
        Returns the number of clips added (0 on an empty or unreadable file).
        """
        if self.history is None:
            return 0
        try:
            cues = parse_subtitle_file(path)
        except (OSError, SubtitleParseError) as exc:
            QMessageBox.warning(self, "Import Subtitles", f"Could not read subtitles from\n{path}\n\n{exc}")
            return 0
        clips = cues_to_text_clips(cues)
        if not clips:
            self.status_bar.showMessage("No subtitle cues were found in that file.", 4000)
            return 0
        track = self._subtitle_track_for(clips)
        commands = []
        if track is None:
            existing = sum(1 for t in self.project.tracks if t.name.startswith("Subtitles"))
            add_track = AddTrackCommand(
                self.project, TrackType.VIDEO, name="Subtitles" if existing == 0 else f"Subtitles {existing + 1}"
            )
            commands.append(add_track)
            track_id = add_track.track_id
        else:
            track_id = track.id
        commands.extend(AddClipCommand(self.project, track_id, clip) for clip in clips)
        self.history.execute(CompositeCommand(commands, "Import Subtitles"))
        self.timeline.rebuild()
        self._on_project_modified()
        self.status_bar.showMessage(f"Imported {len(clips)} subtitle cue(s).", 4000)
        return len(clips)

    def _subtitle_track_for(self, clips) -> Optional["Track"]:
        """An existing unlocked "Subtitles..." video track all of ``clips`` fit on, else None."""
        for track in self.project.tracks:
            if track.track_type != TrackType.VIDEO or not track.name.startswith("Subtitles") or track.locked:
                continue
            if not any(track.has_overlap(clip) for clip in clips):
                return track
        return None

    def import_media_paths(self, paths) -> None:
        """Probe/thumbnail/waveform each path on a background thread.

        This used to run inline on the UI thread (papered over with
        ``processEvents()``), which visibly froze the app on a batch of
        large files. The heavy lifting now happens in :class:`ImportWorker`
        on its own ``QThread``; only the final project/UI updates happen
        back on the main thread via the signal handlers below.
        """
        # Subtitle files (dropped or picked with media) become text clips, not media.
        paths = list(paths)
        for path in [p for p in paths if is_subtitle_path(p)]:
            self.import_subtitle_file(path)
        paths = [p for p in paths if not is_subtitle_path(p)]
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
        self.import_cancel_btn.setVisible(True)
        self.status_bar.showMessage(f"Importing {len(new_paths)} file(s)...")

        self._import_thread = QThread(self)
        self._import_worker = ImportWorker(new_paths, _cache_dir(), _waveform_cache_dir())
        self._import_worker.moveToThread(self._import_thread)

        self._import_thread.started.connect(self._import_worker.run)
        self._import_worker.asset_ready.connect(self._on_asset_imported)
        self._import_worker.asset_updated.connect(self._on_asset_details)
        self._import_worker.failed.connect(self._on_import_failed)
        self._import_worker.finished.connect(self._on_import_finished)
        self._import_worker.finished.connect(self._import_thread.quit)

        self._import_thread.start()

    def cancel_import(self) -> None:
        if self._import_worker is not None:
            self._import_worker.cancel()
            self.status_bar.showMessage("Cancelling import...")

    def _on_asset_imported(self, asset: MediaAsset) -> None:
        self.project.add_media_asset(asset)
        self.import_progress.setValue(self.import_progress.value() + 1)
        self.media_bin.set_assets(self.project.media_pool, self._thumbnail_for_asset)
        self._mark_dirty()
        if self._auto_proxies_enabled() and needs_proxy(asset):
            self.generate_proxy_for(asset.id)

    def _on_asset_details(self, asset_id: str, thumbnail_path, filmstrip_path, waveform_path) -> None:
        """Second import stage: a thumbnail/waveform arrived for an already-listed asset."""
        asset = self.project.find_asset(asset_id)
        if asset is None:
            return
        asset.thumbnail_path = thumbnail_path
        asset.filmstrip_path = filmstrip_path
        asset.waveform_path = waveform_path
        self._thumb_pixmap_cache.pop(asset_id, None)
        self._filmstrip_pixmap_cache.pop(asset_id, None)
        self._waveform_cache.pop(asset_id, None)
        self.media_bin.set_assets(self.project.media_pool, self._thumbnail_for_asset)
        self.timeline.rebuild()

    def _on_import_failed(self, path: str, message: str) -> None:
        self._import_errors.append(f"{os.path.basename(path)}: {message}")
        self.import_progress.setValue(self.import_progress.value() + 1)

    def _on_import_finished(self) -> None:
        cancelled = self._import_worker is not None and self._import_worker.cancelled
        self.import_progress.setVisible(False)
        self.import_cancel_btn.setVisible(False)
        self.status_bar.showMessage("Import cancelled." if cancelled else "Import complete.", 3000)
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
        asset = self.project.asset_for_clip(clip)
        if asset is None:
            return None
        return self._thumbnail_for_asset(asset)

    def _filmstrip_for_clip(self, clip: Clip) -> Optional[QPixmap]:
        asset = self.project.asset_for_clip(clip)
        if asset is None or not asset.filmstrip_path or not os.path.exists(asset.filmstrip_path):
            return None
        if asset.id in self._filmstrip_pixmap_cache:
            return self._filmstrip_pixmap_cache[asset.id]
        pixmap = QPixmap(asset.filmstrip_path)
        self._filmstrip_pixmap_cache[asset.id] = pixmap
        return pixmap

    def _waveform_for_clip(self, clip: Clip) -> Optional[WaveformData]:
        asset = self.project.asset_for_clip(clip)
        if asset is None or not asset.waveform_path:
            return None
        if asset.id in self._waveform_cache:
            return self._waveform_cache[asset.id]
        data = load_waveform(asset.waveform_path)
        self._waveform_cache[asset.id] = data
        return data

    def _place_selected_asset(self, overwrite: bool) -> None:
        asset_id = self.media_bin.selected_asset_id()
        asset = self.project.find_asset(asset_id) if asset_id else None
        if asset is None:
            self.status_bar.showMessage("Select a file in the Media Bin first.", 4000)
            return
        self.timeline.insert_asset_at_playhead(asset, overwrite=overwrite)

    def _reveal_asset(self, asset_id: str) -> None:
        widget = self.media_bin.list_widget
        for i in range(widget.count()):
            if str(widget.item(i).data(Qt.UserRole)) == asset_id:
                widget.setCurrentRow(i)
                widget.scrollToItem(widget.item(i))
                return

    # ------------------------------------------------------------------
    # Proxies (B10)
    # ------------------------------------------------------------------
    def _auto_proxies_enabled(self) -> bool:
        return QSettings().value(AUTO_PROXY_SETTING, False, type=bool)

    def _on_auto_proxy_toggled(self, checked: bool) -> None:
        QSettings().setValue(AUTO_PROXY_SETTING, bool(checked))

    def _proxy_for_selected_asset(self) -> None:
        asset_id = self.media_bin.selected_asset_id()
        if asset_id:
            self.generate_proxy_for(asset_id)
        else:
            self.status_bar.showMessage("Select a video in the Media Bin first.", 4000)

    def generate_proxy_for(self, asset_id: str) -> None:
        """Encode a 540p preview proxy in the background (export keeps using the original)."""
        asset = self.project.find_asset(asset_id)
        if asset is None or not asset.has_video or asset_id in self._proxy_jobs:
            return
        self._proxy_jobs.add(asset_id)
        self.status_bar.showMessage(f"Generating preview proxy for {asset.display_name}...")
        snapshot = MediaAsset.from_dict(asset.to_dict())

        def work() -> None:
            try:
                path = generate_proxy(snapshot)
            except Exception:  # noqa: BLE001 - reported as a failed proxy
                path = None
            self._proxy_finished.emit(asset_id, path)

        threading.Thread(target=work, name="proxy", daemon=True).start()

    def _on_proxy_finished(self, asset_id: str, path) -> None:
        self._proxy_jobs.discard(asset_id)
        asset = self.project.find_asset(asset_id)
        if asset is None:
            return
        if path:
            asset.proxy_path = path
            self._mark_dirty()
            self.status_bar.showMessage(f"Preview proxy ready: {asset.display_name}", 4000)
            self.media_bin.set_assets(self.project.media_pool, self._thumbnail_for_asset)
            self.preview.refresh_after_edit()
        else:
            self.status_bar.showMessage(f"Couldn't create a proxy for {asset.display_name}.", 6000)

    def add_text_clip(self) -> None:
        # TimelineWidget.add_text_clip() already selects and rebuilds; the
        # selection_changed signal (connected in _connect_signals) updates
        # the properties panel for us.
        self.timeline.add_text_clip()

    # ------------------------------------------------------------------
    # Selection / sync
    # ------------------------------------------------------------------
    def _on_selection_changed(self, clip_id) -> None:
        self.properties.set_playhead_time(self.timeline.playhead_time)
        self.properties.set_selected_clip(clip_id)
        self._refresh_transform_target()

    def _transform_target(self) -> Optional[dict]:
        """What the preview's on-canvas Transform edits (F15): the selected video/image clip,
        when it's under the playhead, in Fit mode and its source size is known."""
        clip = self.project.find_clip(self.timeline.selected_clip_id) if self.timeline.selected_clip_id else None
        if clip is None or clip.media_type not in (MediaType.VIDEO, MediaType.IMAGE) or clip.reframe_mode != ReframeMode.FIT:
            return None
        if not clip.contains_time(self.timeline.playhead_time):
            return None
        asset = self.project.asset_for_clip(clip)
        if asset is None or asset.width <= 0 or asset.height <= 0:
            return None
        return {
            "clip_id": clip.id, "x": clip.transform_x, "y": clip.transform_y, "scale": clip.transform_scale,
            "rotation": clip.transform_rotation, "source_size": (asset.width, asset.height),
            # Keyframed position: the static values have no effect, so moving is off (scaling still works).
            "movable": len(clip.position_keyframes) < 2,
        }

    def _refresh_transform_target(self) -> None:
        self.preview.set_transform_target(self._transform_target())

    def _apply_canvas_transform(self, clip_id: str, x: float, y: float, scale: float) -> None:
        """One drag on the preview = one undo step for the Transform values it changed."""
        clip = self.project.find_clip(clip_id)
        if clip is None:
            return
        changes = [("transform_x", round(x, 4)), ("transform_y", round(y, 4)), ("transform_scale", round(scale, 4))]
        commands = [UpdateClipPropertyCommand(self.project, clip_id, name, value, "Transform on Canvas")
                    for name, value in changes if abs(getattr(clip, name) - value) > 1e-6]
        if not commands:
            return
        self.history.execute(CompositeCommand(commands, "Transform on Canvas"))
        self.timeline.refresh_clip(clip_id)
        self.properties.set_selected_clip(clip_id)
        self._on_project_modified()

    def _on_project_modified(self) -> None:
        self._mark_dirty()
        self._refresh_transform_target()
        # Reseeks only if the clip under the playhead changed in a way the
        # preview shows (not on every colour/opacity slider tick).
        self.preview.refresh_after_edit()
        self._update_time_status()

    def _on_timeline_playhead_changed(self, t: float) -> None:
        if self._syncing_playhead:
            return
        self._syncing_playhead = True
        self.preview.seek_to(t)
        self._syncing_playhead = False
        self.properties.set_playhead_time(t)
        self._update_time_status()
        self._refresh_transform_target()

    def _on_preview_time_changed(self, t: float) -> None:
        if self._syncing_playhead:
            return
        self._syncing_playhead = True
        self.timeline.set_playhead(t)
        self._syncing_playhead = False
        self.properties.set_playhead_time(t)
        self._update_time_status()

    def _update_time_status(self) -> None:
        fps = self.project.settings.fps
        self.time_status_label.setText(
            f"{format_timecode(self.preview.current_time, fps)} / {format_timecode(self.project.total_duration, fps)}"
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
        if not hasattr(self, "undo_action"):
            return  # History notifies during construction, before the menus exist
        self.undo_action.setEnabled(self.history.can_undo)
        self.redo_action.setEnabled(self.history.can_redo)
        # The title star follows the undo position (R4): undoing back to the
        # saved state clears it again.
        self._update_title()

    # ------------------------------------------------------------------
    # Project file management
    # ------------------------------------------------------------------
    def _mark_dirty(self) -> None:
        """A change outside History (media import, proxy, recovered work)."""
        self._media_dirty = True
        self._update_title()

    def _update_title(self) -> None:
        name = self.project.name
        star = "*" if self._dirty else ""
        path = f" — {self.project.file_path}" if self.project.file_path else ""
        self.setWindowTitle(f"{star}{name}{path} — {APP_NAME}")

    def _confirm_discard_if_dirty(self) -> bool:
        """Save / Discard / Cancel prompt before losing unsaved changes.

        Returns True if it's OK to continue (saved successfully, or the user
        chose Discard). The prompt used to offer only Discard or Cancel, so
        saving first meant cancelling, saving, and repeating the action.
        """
        if not self._dirty:
            return True
        result = QMessageBox.warning(
            self,
            "Unsaved Changes",
            f'Do you want to save the changes to "{self.project.name}"?',
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if result == QMessageBox.Save:
            return self.save_project()
        return result == QMessageBox.Discard

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
        self._add_recent_project(path)

    def _report_load_problems(self) -> None:
        """Show, once, what the loader had to default or skip and any timeline problems (B8, R6)."""
        problems = list(self.project.load_warnings) + self.project.validate()
        if not problems:
            return
        shown = "\n".join(f"- {p}" for p in problems[:15])
        more = f"\n...and {len(problems) - 15} more" if len(problems) > 15 else ""
        QMessageBox.warning(
            self, "Project Opened With Problems",
            "The project opened, but needs attention:\n\n" + shown + more
            + "\n\nOverlapping clips render in an unpredictable order; move or trim them apart.",
        )

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

    def _warn_about_missing_media(self) -> None:
        missing = sorted({a.path for a in self.project.media_pool if not os.path.isfile(a.path)})
        if not missing:
            return
        preview = "\n".join(missing[:10])
        if len(missing) > 10:
            preview += f"\n... and {len(missing) - 10} more"
        answer = QMessageBox.warning(
            self,
            "Missing Media",
            "The following media files referenced by this project could not be found:\n\n"
            f"{preview}\n\n"
            "Affected clips are marked offline on the timeline. Relink them now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self.open_relink_dialog()

    def open_relink_dialog(self) -> None:
        dialog = RelinkDialog(self.project, self.history, self)
        dialog.relinked.connect(self._on_media_relinked)
        dialog.exec()

    def _on_media_relinked(self, count: int) -> None:
        self._thumb_pixmap_cache.clear()
        self._filmstrip_pixmap_cache.clear()
        self._waveform_cache.clear()
        self.media_bin.set_assets(self.project.media_pool, self._thumbnail_for_asset)
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

    def _clear_autosave(self) -> None:
        self._autosave_session.clear()

    def _offer_autosave_recovery(self) -> None:
        """Offer the newest autosave left by an editor that is no longer running.

        Autosaves of editors that are still running (a second window) are
        never offered; older crashed sessions stay for the next launch.
        """
        orphans = find_orphaned_autosaves(_autosave_dir(), exclude_session=self._autosave_session.session_id)
        if not orphans:
            return
        orphan = orphans[0]
        result = QMessageBox.question(
            self,
            "Recover Unsaved Work",
            f"It looks like {APP_NAME} closed unexpectedly while editing "
            f"'{orphan.name}' with unsaved changes.\n\nRecover that work now?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if result != QMessageBox.Yes:
            orphan.discard()
            return

        try:
            with open(orphan.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            project = Project.from_dict(data)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Recover Unsaved Work", f"Could not recover the autosave:\n{exc}")
            orphan.discard()
            return
        orphan.discard()
        original_path = orphan.original_path

        project.file_path = original_path
        self.project = project
        self.history = self._new_history()
        self._thumb_pixmap_cache.clear()
        self._filmstrip_pixmap_cache.clear()
        self._waveform_cache.clear()
        self._load_project_into_panels()
        self._mark_dirty()  # recovered state doesn't exist on disk at `original_path` yet
        self._warn_about_missing_media()

    # ------------------------------------------------------------------
    # Window / layout persistence
    # ------------------------------------------------------------------
    def _restore_window_state(self) -> None:
        """Restore window size/position and panel proportions from the last session.

        Uses ``QSettings`` (backed by the registry on Windows, a plist on
        macOS, an ini file on Linux) under the organization/application
        name set in ``main.py``. Absent on first run, in which case the
        layout built in ``_build_ui`` (stretch factors, ``setMinimumSize``)
        stands as the default.
        """
        # Explicit type=QByteArray: some QSettings backends (notably the
        # .ini format) don't round-trip a QByteArray's type without a hint,
        # handing back a plain string that restoreGeometry()/restoreState()
        # silently reject.
        settings = QSettings()
        geometry = settings.value("window/geometry", type=QByteArray)
        if geometry:
            self.restoreGeometry(geometry)
        top_state = settings.value("window/top_splitter", type=QByteArray)
        if top_state:
            self.top_splitter.restoreState(top_state)
        main_state = settings.value("window/main_splitter", type=QByteArray)
        if main_state:
            self.main_splitter.restoreState(main_state)

    def _save_window_state(self) -> None:
        settings = QSettings()
        settings.setValue("window/geometry", self.saveGeometry())
        settings.setValue("window/top_splitter", self.top_splitter.saveState())
        settings.setValue("window/main_splitter", self.main_splitter.saveState())

    # ------------------------------------------------------------------
    def open_export_dialog(self) -> None:
        """Open a non-modal Export dialog: the editor stays usable while it renders a snapshot."""
        if not self.project.tracks or self.project.total_duration <= 0:
            QMessageBox.information(self, "Export Video", "Add some clips to the timeline before exporting.")
            return
        dialog = ExportDialog(self.project, self, in_out_range=self.timeline.in_out_range(), queue_available=True)
        dialog.queue_requested.connect(self.queue_export)
        dialog.setModal(False)
        self._export_dialogs.append(dialog)
        dialog.destroyed.connect(lambda _obj=None, d=dialog: self._forget_export_dialog(d))
        dialog.show()

    def export_frame_dialog(self, path: str | None = None) -> str | None:
        """Save the frame under the playhead -- composited as the export renders it, at the
        project's full resolution -- as PNG or JPEG (F15). `path` skips the file dialog."""
        if not self.project.tracks or self.project.total_duration <= 0:
            QMessageBox.information(self, "Export Frame", "Add some clips to the timeline first.")
            return None
        t = self.timeline.playhead_time
        if path is None:
            stamp = format_timecode(t, self.project.settings.fps).replace(":", "-").replace(";", "-")
            path, _ = QFileDialog.getSaveFileName(
                self, "Export Frame", f"{self.project.name}_{stamp}.png", "PNG image (*.png);;JPEG image (*.jpg *.jpeg)"
            )
            if not path:
                return None
        if not path.lower().endswith((".png", ".jpg", ".jpeg")):
            path += ".png"
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            data = render_frame(self.project, t, full_size(self.project), timeout=80.0)
            if path.lower().endswith(".png"):
                with open(path, "wb") as handle:
                    handle.write(data)
            elif not QImage.fromData(data, "PNG").save(path, "JPG", 95):
                raise OSError(f"couldn't write {path}")
        except (FrameRenderError, OSError) as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Export Frame", f"The frame couldn't be exported:\n{exc}")
            return None
        QApplication.restoreOverrideCursor()
        self.statusBar().showMessage(f"Frame at {format_timecode(t, self.project.settings.fps)} saved to {path}", 6000)
        return path

    def queue_export(self, settings) -> None:
        """Queue an export of the project as it is right now (a snapshot) and show the queue."""
        job = self.render_queue.add_job(self.project, settings)
        self.render_queue_dock.show()
        self.render_queue_dock.raise_()
        self.status_bar.showMessage(f"Queued: {job.name}", 5000)

    def _on_queue_job_finished(self, job_id: str, status: str) -> None:
        job = self.render_queue.queue.find(job_id)
        if job is not None:
            self.status_bar.showMessage(f"Render queue: {job.name} -- {status}", 8000)

    def _forget_export_dialog(self, dialog) -> None:
        if dialog in self._export_dialogs:
            self._export_dialogs.remove(dialog)

    def running_exports(self) -> List[ExportDialog]:
        alive = []
        for dialog in list(self._export_dialogs):
            try:
                if dialog.is_running():
                    alive.append(dialog)
            except RuntimeError:  # already deleted
                self._forget_export_dialog(dialog)
        return alive

    def focus_properties(self) -> None:
        """Enter on the timeline: move keyboard focus into the Properties panel for the selected clip."""
        if self.timeline.selected_clip_id is None:
            return
        self.properties.set_selected_clip(self.timeline.selected_clip_id)
        for widget in self.properties.findChildren(QWidget):
            if widget.isVisible() and widget.isEnabled() and widget.focusPolicy() & Qt.TabFocus:
                widget.setFocus(Qt.TabFocusReason)
                return

    def show_shortcuts(self) -> None:
        ShortcutsDialog(self.findChildren(QAction), self).exec()

    def show_user_guide(self) -> None:
        UserGuideDialog(self).exec()

    def about_text(self) -> str:
        ffmpeg = binary_version("ffmpeg") or "not found"
        try:
            gpu = "available" if nvenc_available() else "not available"
        except Exception:  # noqa: BLE001
            gpu = "unknown"
        return (
            f"<h3>{APP_NAME} {__version__}</h3>"
            "<p>A lightweight, extensible non-linear video editor built with "
            "Python, PySide6 and FFmpeg.</p>"
            f"<p>FFmpeg: {ffmpeg}<br>NVENC GPU encoding: {gpu}</p>"
        )

    def show_about(self) -> None:
        QMessageBox.about(self, f"About {APP_NAME}", self.about_text())

    def closeEvent(self, event) -> None:
        if self.render_queue.queue.pending_count():
            result = QMessageBox.question(
                self, "Render Queue", "Queued exports haven't finished. Cancel them and quit?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if result != QMessageBox.Yes or not self.render_queue.cancel_all_and_wait():
                event.ignore()
                return
        running = self.running_exports()
        if running:
            result = QMessageBox.question(
                self, "Export in Progress", "An export is still running. Cancel it and quit?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if result != QMessageBox.Yes or not all(d.cancel_and_wait() for d in running):
                event.ignore()
                return

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
            self._autosave_session.close()
            self._save_window_state()
            event.accept()
        else:
            event.ignore()
