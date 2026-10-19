"""The multi-track timeline editor: the heart of the editing UI."""

from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional, Set

from PySide6.QtCore import QMimeData, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGraphicsLineItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QMenu,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from core.clip import Clip, MediaType
from core.commands import (
    AddClipCommand,
    AddMarkerCommand,
    AddTrackCommand,
    MoveClipCommand,
    MoveTrackCommand,
    RemoveClipCommand,
    RemoveMarkerCommand,
    RemoveTrackCommand,
    RecolorMarkerCommand,
    RenameMarkerCommand,
    SplitClipCommand,
    TrimClipCommand,
    UpdateTrackPropertyCommand,
    build_add_clip_command,
    build_ripple_delete_command,
    build_ripple_trim_command,
)
from core.edit_ops import (
    build_add_edit_all_tracks,
    build_extend_edit_command,
    build_extract_command,
    build_lift_command,
    build_delete_clips_command,
    build_detach_audio_command,
    build_duplicate_command,
    build_insert_command,
    build_move_clips_command,
    build_overwrite_command,
    build_paste_attributes_command,
    build_paste_command,
    build_ripple_move_command,
    build_roll_at_edge,
    build_roll_command,
    build_slide_command,
    build_slip_command,
    copy_clips,
    linked_clip_ids,
)
from core.filter_graph import track_transition_issues
from core.history import CompositeCommand, History
from core.media_asset import MediaAsset
from core.project import Project
from core.timeline_nav import (
    adjacent_point,
    clip_keyframe_points,
    edit_points,
    marker_points,
    snap_to_frame,
    step_frames,
)
from core.track import TRACK_HEIGHT_PRESETS, Track, TrackType, normalize_track_height
from ui import theme
from ui.media_bin import MEDIA_MIME_TYPE
from ui.widgets import timeline_accessibility
from ui.widgets.timeline_items import (
    HEADER_WIDTH,
    RULER_HEIGHT,
    TRACK_HEIGHT,
    ClipItem,
    CornerItem,
    MarkerItem,
    PlayheadItem,
    RowLayout,
    RulerItem,
    TimelineSignals,
    TrackHeaderItem,
    row_at_scene_y,
)

DEFAULT_PIXELS_PER_SECOND = 40.0
MIN_PPS = 5.0
MAX_PPS = 400.0
# Minimum spacing between preview seeks while drag-scrubbing the ruler
# (~30 Hz); the playhead line itself follows the mouse on every move.
SCRUB_SEEK_INTERVAL_S = 1.0 / 30.0


CLIPBOARD_MIME = "application/x-video-editor-clips"


class DuckingDialog(QDialog):
    """Ducking settings for one track: which track it ducks under, and the compressor's response.

    Threshold, ratio, attack and release used to be fixed Track defaults
    with no UI at all.
    """

    def __init__(self, track: Track, other_tracks: List[Track], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(f"Audio Ducking: {track.name}")
        form = QFormLayout(self)
        self.trigger_combo = QComboBox()
        self.trigger_combo.addItem("None (no ducking)", None)
        for other in other_tracks:
            self.trigger_combo.addItem(other.name, other.id)
        index = self.trigger_combo.findData(track.duck_trigger_track_id)
        self.trigger_combo.setCurrentIndex(index if index >= 0 else 0)
        form.addRow("Duck under", self.trigger_combo)

        def spin(value, low, high, step, suffix, decimals=2, tip=""):
            box = QDoubleSpinBox()
            box.setRange(low, high)
            box.setDecimals(decimals)
            box.setSingleStep(step)
            box.setSuffix(suffix)
            box.setValue(value)
            box.setToolTip(tip)
            return box

        self.threshold_spin = spin(track.duck_threshold, 0.001, 1.0, 0.01, "", 3,
                                   "Trigger level (linear 0..1) above which the music is lowered")
        self.ratio_spin = spin(track.duck_ratio, 1.0, 20.0, 0.5, " : 1", 1, "How strongly the music is lowered")
        self.attack_spin = spin(track.duck_attack, 0.01, 2000.0, 5.0, " ms", 1, "How fast ducking starts")
        self.release_spin = spin(track.duck_release, 0.01, 9000.0, 25.0, " ms", 1, "How fast the music comes back")
        form.addRow("Threshold", self.threshold_spin)
        form.addRow("Ratio", self.ratio_spin)
        form.addRow("Attack", self.attack_spin)
        form.addRow("Release", self.release_spin)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> Dict[str, object]:
        return {
            "duck_trigger_track_id": self.trigger_combo.currentData(),
            "duck_threshold": self.threshold_spin.value(),
            "duck_ratio": self.ratio_spin.value(),
            "duck_attack": self.attack_spin.value(),
            "duck_release": self.release_spin.value(),
        }


def zoom_scroll_value(anchor_time: float, new_pps: float, anchor_viewport_x: float) -> int:
    """Horizontal scroll value keeping ``anchor_time`` under viewport x ``anchor_viewport_x`` after a zoom."""
    return int(round(HEADER_WIDTH + anchor_time * new_pps - anchor_viewport_x))


class FlowLayout(QLayout):
    """A left-to-right layout that wraps to a new row instead of overflowing.

    Adapted from Qt's own "Flow Layout" example. The toolbar row (8 buttons
    plus a zoom slider) needs ~1,088px at its natural size — a plain
    ``QHBoxLayout`` would let it run past the edge of any window narrower
    than that, with the rightmost buttons rendered off-screen and
    genuinely unreachable (no scrollbar, no wrap). This instead moves
    whichever widgets don't fit onto additional rows, so every control
    stays visible and clickable regardless of window width — the same
    fix (wrap instead of clip) used for an analogous tab-bar overflow in
    the sibling ``photo_professional_tool`` project.
    """

    def __init__(self, parent: Optional[QWidget] = None, margin: int = 0, spacing: int = 6):
        super().__init__(parent)
        self._items: List[QLayoutItem] = []
        self._spacing = spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 (Qt override)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Optional[QLayoutItem]:  # noqa: N802 (Qt override)
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> Optional[QLayoutItem]:  # noqa: N802 (Qt override)
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientations:  # noqa: N802 (Qt override)
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt override)
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 (Qt override)
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 (Qt override)
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 (Qt override)
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        left, top, right, bottom = self.getContentsMargins()
        return size + QSize(left + right, top + bottom)

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        left, top, right, bottom = self.getContentsMargins()
        effective = rect.adjusted(left, top, -right, -bottom)
        x, y = effective.x(), effective.y()
        line_height = 0

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > effective.right() and line_height > 0:
                x = effective.x()
                y += line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + bottom


class TimelineView(QGraphicsView):
    """QGraphicsView with Ctrl+wheel zoom and click-to-seek behaviour."""

    seek_requested = Signal(float)
    zoom_requested = Signal(float, float)  # multiplicative factor, anchor viewport x (-1 = playhead)

    def __init__(self, scene: QGraphicsScene, timeline_widget: "TimelineWidget"):
        super().__init__(scene)
        self._timeline_widget = timeline_widget
        self.setRenderHint(QPainter.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # Dragging on empty space draws a rubber band that selects clips (F6).
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setRubberBandSelectionMode(Qt.IntersectsItemShape)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self._scrubbing = False
        self._last_scrub_emit = 0.0

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Defensive: a resize can in principle fire before _build_ui() has
        # finished creating empty_hint_label (Qt may trigger one as soon as
        # this view is added to its parent's layout, before the rest of
        # TimelineWidget._build_ui() runs).
        if hasattr(self._timeline_widget, "empty_hint_label"):
            self._timeline_widget._reposition_empty_hint()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.zoom_requested.emit(factor, float(event.position().x()))  # anchored on the mouse
            event.accept()
        else:
            super().wheelEvent(event)

    def _seconds_at(self, view_pos) -> float:
        pos = self.mapToScene(view_pos)
        return max(0.0, (pos.x() - HEADER_WIDTH) / self._timeline_widget.pixels_per_second)

    def mousePressEvent(self, event) -> None:
        pos = self.mapToScene(event.pos())
        if event.button() == Qt.LeftButton and pos.y() < RULER_HEIGHT and pos.x() >= HEADER_WIDTH:
            # Press on the ruler (or the playhead's handle, which sits in
            # it) seeks, and dragging from there scrubs.
            self._scrubbing = True
            self._last_scrub_emit = time.monotonic()
            self.seek_requested.emit(self._seconds_at(event.pos()))
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._scrubbing:
            t = self._seconds_at(event.pos())
            self._timeline_widget.move_playhead_marker(t)
            now = time.monotonic()
            if now - self._last_scrub_emit >= SCRUB_SEEK_INTERVAL_S:
                self._last_scrub_emit = now
                self.seek_requested.emit(t)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._scrubbing:
            self._scrubbing = False
            self.seek_requested.emit(self._seconds_at(event.pos()))  # always land exactly where released
            return
        super().mouseReleaseEvent(event)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(MEDIA_MIME_TYPE):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat(MEDIA_MIME_TYPE):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if event.mimeData().hasFormat(MEDIA_MIME_TYPE):
            asset_id = bytes(event.mimeData().data(MEDIA_MIME_TYPE)).decode("utf-8")
            scene_pos = self.mapToScene(event.position().toPoint() if hasattr(event, "position") else event.pos())
            seconds = max(0.0, (scene_pos.x() - HEADER_WIDTH) / self._timeline_widget.pixels_per_second)
            row = row_at_scene_y(scene_pos.y(), self._timeline_widget.row_layout)
            self._timeline_widget.handle_media_drop(asset_id, seconds, row)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class TimelineWidget(QWidget):
    """Top-level timeline panel: toolbar + track headers + graphics view."""

    selection_changed = Signal(object)  # Optional[str] clip_id (the primary selection)
    project_modified = Signal()
    playhead_changed = Signal(float)
    status_message = Signal(str)
    reveal_asset_requested = Signal(str)  # asset id (clip menu > Reveal in Media Bin)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project: Optional[Project] = None
        self.history: Optional[History] = None
        self.pixels_per_second = DEFAULT_PIXELS_PER_SECOND
        self.playhead_time = 0.0
        self.selected_clip_id: Optional[str] = None  # primary selection (shown in Properties)
        # Every selected clip: Ctrl+click and rubber band add to it (F6).
        self.selected_clip_ids: Set[str] = set()
        self._syncing_selection = False
        self.razor_mode = False
        self._clipboard_payload: Optional[dict] = None
        self._layout_key = None
        # Injected so tests can replace the modal ducking dialog.
        self.ducking_dialog_factory = DuckingDialog
        # Set by clicking a track header; Delete Track acts on it (falling
        # back to the selected clip's track). Cleared by selecting a clip.
        self.selected_track_id: Optional[str] = None
        # In/Out points (seconds or None): shaded on the ruler, and offered
        # as the Export dialog's Trim Range. View state, not project data.
        self.in_point: Optional[float] = None
        self.out_point: Optional[float] = None
        # Preview renders shown on the ruler (F10c): (start, end, valid).
        self.render_ranges: list = []

        self._row_tracks: List[Track] = []
        self.row_layout = RowLayout()  # row tops/heights, shared with the clip and header items
        self._marker_items: List[MarkerItem] = []
        self._clip_items: Dict[str, ClipItem] = {}
        self._header_items: List[TrackHeaderItem] = []
        self._thumbnail_cache: Dict[str, QPixmap] = {}
        self._filmstrip_cache: Dict[str, QPixmap] = {}
        self.get_thumbnail_for_clip = None  # injected callable: Clip -> Optional[QPixmap]
        self.get_filmstrip_for_clip = None  # injected callable: Clip -> Optional[QPixmap]
        self.get_waveform_for_clip = None  # injected callable: Clip -> Optional[WaveformData]
        self.snap_enabled = True
        self.ripple_enabled = False

        self.signals = TimelineSignals()
        timeline_accessibility.install()  # screen readers see tracks and clips, not one picture
        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(0)

        toolbar = FlowLayout(margin=0, spacing=6)
        toolbar.setContentsMargins(8, 6, 8, 6)

        self.add_video_track_btn = QPushButton("+ Video Track")
        self.add_video_track_btn.setToolTip("Add a new video track")
        self.add_audio_track_btn = QPushButton("+ Audio Track")
        self.add_audio_track_btn.setToolTip("Add a new audio track")
        self.split_btn = QPushButton("Split")
        self.split_btn.setToolTip("Split the selected clip at the playhead (S)")
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setToolTip("Delete the selected clip (Delete)")
        self.delete_track_btn = QPushButton("Delete Track")
        self.delete_track_btn.setToolTip("Delete the selected track")
        self.snap_btn = QPushButton("Snap")
        self.snap_btn.setCheckable(True)
        self.snap_btn.setChecked(True)
        self.snap_btn.setToolTip("Snap clips to the playhead, clip edges, and 0:00 while dragging or trimming")
        self.ripple_btn = QPushButton("Ripple")
        self.ripple_btn.setCheckable(True)
        self.ripple_btn.setToolTip(
            "When on, Delete and Trim also shift every later clip on the same track "
            "to close (or open) the resulting gap, instead of leaving it."
        )
        self.add_marker_btn = QPushButton("+ Marker")
        self.add_marker_btn.setToolTip("Add a named marker at the playhead (M)")

        for btn in (
            self.add_video_track_btn, self.add_audio_track_btn, self.split_btn,
            self.delete_btn, self.delete_track_btn, self.snap_btn, self.ripple_btn,
            self.add_marker_btn,
        ):
            toolbar.addWidget(btn)

        toolbar.addWidget(QLabel("Zoom"))
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setMinimum(0)
        self.zoom_slider.setMaximum(100)
        self.zoom_slider.setValue(40)
        self.zoom_slider.setFixedWidth(140)
        toolbar.addWidget(self.zoom_slider)

        layout.addLayout(toolbar)

        self.scene = QGraphicsScene()
        self.scene.setBackgroundBrush(QColor(theme.COLOR_BG_DARKEST))
        self.view = TimelineView(self.scene, self)
        layout.addWidget(self.view, 1)

        # Shown only while every track is empty (a brand-new project, or one
        # that's had everything deleted) -- otherwise a first-time user sees
        # nothing but a bare ruler and empty track rows, with no indication
        # of what to do next. A child of the viewport (not the scene) so it
        # overlays on top of the ruler/tracks without needing a QGraphicsItem
        # of its own, and doesn't intercept clicks meant for the view beneath
        # it (e.g. clicking the ruler to seek).
        self.empty_hint_label = QLabel(
            "Import media (Ctrl+I), then drag a clip from the Media Bin here to begin editing.",
            self.view.viewport(),
        )
        self.empty_hint_label.setAlignment(Qt.AlignCenter)
        self.empty_hint_label.setWordWrap(True)
        self.empty_hint_label.setStyleSheet(
            f"color: {theme.COLOR_TEXT_DIM}; font-size: 13px; background: transparent;"
        )
        self.empty_hint_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.empty_hint_label.hide()

        self.ruler_item = RulerItem(self.pixels_per_second, 60.0)
        self.ruler_item.setPos(HEADER_WIDTH, 0)
        self.scene.addItem(self.ruler_item)
        self.playhead_item = PlayheadItem(400)
        self.scene.addItem(self.playhead_item)
        self.snap_guide_item = self._make_snap_guide_item(400)
        self.scene.addItem(self.snap_guide_item)
        self.corner_item = CornerItem()
        self.scene.addItem(self.corner_item)

        # The header column and corner plate are scene items (so they scroll
        # vertically in sync with the tracks for free) but must stay pinned
        # to the viewport's left edge horizontally instead of scrolling away
        # with the timeline content.
        self.view.horizontalScrollBar().valueChanged.connect(self._pin_header_items)

    def _connect_signals(self) -> None:
        self.add_video_track_btn.clicked.connect(lambda: self.add_track(TrackType.VIDEO))
        self.add_audio_track_btn.clicked.connect(lambda: self.add_track(TrackType.AUDIO))
        self.split_btn.clicked.connect(self.split_selected)
        self.delete_btn.clicked.connect(self.delete_selected)
        self.delete_track_btn.clicked.connect(self.delete_selected_track)
        self.zoom_slider.valueChanged.connect(self._on_zoom_slider)
        self.snap_btn.toggled.connect(self._on_snap_toggled)
        self.ripple_btn.toggled.connect(self._on_ripple_toggled)
        self.add_marker_btn.clicked.connect(self.add_marker_at_playhead)

        self.view.seek_requested.connect(self.set_playhead)
        self.view.zoom_requested.connect(self._on_zoom_factor)
        self.scene.selectionChanged.connect(self._on_scene_selection_changed)

        self.signals.clip_selected.connect(self._on_clip_selected)
        self.signals.clip_toggle_selected.connect(self._on_clip_toggle_selected)
        self.signals.clip_context_menu.connect(self._on_clip_context_menu)
        self.signals.clip_razor.connect(self._on_clip_razor)
        self.signals.clip_moved.connect(self._on_clip_moved)
        self.signals.clip_trimmed.connect(self._on_clip_trimmed)
        self.signals.clip_edit_gesture.connect(self._on_clip_edit_gesture)
        self.signals.clip_double_clicked.connect(self._on_clip_selected)
        self.signals.track_mute_toggled.connect(self._on_track_mute_toggled)
        self.signals.track_lock_toggled.connect(self._on_track_lock_toggled)
        self.signals.track_hidden_toggled.connect(self._on_track_hidden_toggled)
        self.signals.track_duck_config_requested.connect(self._on_track_duck_config_requested)
        self.signals.track_rename_requested.connect(self.rename_track)
        self.signals.track_move_requested.connect(self.move_track)
        self.signals.track_height_requested.connect(self.set_track_height)
        self.signals.track_selected.connect(self._on_track_selected)
        self.signals.snap_guide_show.connect(self._on_snap_guide_show)
        self.signals.snap_guide_hide.connect(self._on_snap_guide_hide)
        self.signals.marker_clicked.connect(self._on_marker_clicked)
        self.signals.marker_rename_requested.connect(self._on_marker_rename_requested)
        self.signals.marker_delete_requested.connect(self._on_marker_delete_requested)
        self.signals.marker_color_requested.connect(self._on_marker_color_requested)

    # ------------------------------------------------------------------
    def set_project(self, project: Project, history: History) -> None:
        self.project = project
        self.history = history
        self.selected_clip_id = None
        self.selected_clip_ids = set()
        self.selected_track_id = None
        self.in_point = self.out_point = None
        self.playhead_time = 0.0
        self._layout_key = None
        self.rebuild()

    def _emit_modified(self) -> None:
        self.project_modified.emit()

    # ------------------------------------------------------------------
    # Scene construction
    # ------------------------------------------------------------------
    def _compute_layout_key(self):
        """Everything besides clips that the scene's items depend on (rows, headers, markers)."""
        video_tracks = [t for t in self.project.tracks if t.track_type == TrackType.VIDEO]
        audio_tracks = [t for t in self.project.tracks if t.track_type == TrackType.AUDIO]
        rows = list(reversed(video_tracks)) + audio_tracks
        return (
            tuple((t.id, t.name, t.muted, t.locked, t.hidden, bool(t.duck_trigger_track_id), t.height) for t in rows),
            tuple((m.id, m.time, m.name, m.color) for m in self.project.markers),
            self.selected_track_id,
            self.project.settings.fps,
        )

    def rebuild(self) -> None:
        """Bring the scene up to date with the project.

        When only clips changed (the common case after an edit), existing
        clip items are updated in place by id and only added/removed clips
        create or delete items (PG3). Any change to tracks, headers or
        markers falls back to recreating the scene.
        """
        if self.project is None:
            return
        if self.selected_track_id is not None and self.project.find_track(self.selected_track_id) is None:
            self.selected_track_id = None
        key = self._compute_layout_key()
        if key == self._layout_key and self._row_tracks:
            self._sync_clip_items()
            return
        self._layout_key = key
        self._full_rebuild()

    def _sync_clip_items(self) -> None:
        offline_cache: Dict[str, bool] = {}
        seen = set()
        for row, track in enumerate(self._row_tracks):
            issues = track_transition_issues(track)
            for clip in track.sorted_clips():
                seen.add(clip.id)
                item = self._clip_items.get(clip.id)
                if item is None:
                    self._add_clip_item(clip, row, track, offline_cache)
                    continue
                item.offline = self._is_offline(clip, offline_cache)
                item.track_name = track.name
                item.set_row(row)
                item.set_source_clip(clip)
                item.set_locked(track.locked)
                item.set_transition_issue(issues.get(clip.id))
        for clip_id in [cid for cid in self._clip_items if cid not in seen]:
            item = self._clip_items.pop(clip_id)
            self.scene.removeItem(item)
        self.selected_clip_ids &= seen
        if self.selected_clip_id not in seen:
            self.selected_clip_id = None
        self.ruler_item.set_scale(self.pixels_per_second, max(self.project.total_duration, 60.0))
        self.ruler_item.update()
        self.scene.setSceneRect(0, 0, self._scene_width(), self._scene_height())
        self._highlight_selection()
        self._update_empty_hint()

    def _full_rebuild(self) -> None:
        self._syncing_selection = True
        self.scene.clear()
        self._syncing_selection = False
        self._clip_items.clear()
        self._header_items.clear()
        self._marker_items = []
        if self.selected_track_id is not None and self.project.find_track(self.selected_track_id) is None:
            self.selected_track_id = None

        video_tracks = [t for t in self.project.tracks if t.track_type == TrackType.VIDEO]
        audio_tracks = [t for t in self.project.tracks if t.track_type == TrackType.AUDIO]
        # Higher-index video tracks are drawn on top in compositing, so show
        # them higher in the panel too (matches conventional NLE layout).
        self._row_tracks = list(reversed(video_tracks)) + audio_tracks
        self.row_layout.set_heights([normalize_track_height(t.height) for t in self._row_tracks])
        offline_cache: Dict[str, bool] = {}

        total_seconds = max(self.project.total_duration, 60.0)
        self.ruler_item = RulerItem(self.pixels_per_second, total_seconds, self.project.settings.fps)
        self.ruler_item.set_in_out(self.in_point, self.out_point)
        self.ruler_item.set_render_ranges(self.render_ranges)
        self.ruler_item.setPos(HEADER_WIDTH, 0)
        self.scene.addItem(self.ruler_item)

        # Created before setSceneRect() below: changing the scene rect can
        # make the view's scrollbar emit valueChanged synchronously (its
        # range/value gets clamped to the new rect), which would otherwise
        # fire _pin_header_items() while self.corner_item still points at
        # the previous (just-cleared, now-deleted) item.
        self.corner_item = CornerItem()
        self.scene.addItem(self.corner_item)

        for row, track in enumerate(self._row_tracks):
            header = TrackHeaderItem(track.id, row, track.name, track.track_type.value, self.signals, self.row_layout)
            header.mute_btn.set_active(track.muted)
            header.lock_btn.set_active(track.locked)
            header.hide_btn.set_active(track.hidden)
            header.duck_btn.set_active(bool(track.duck_trigger_track_id))
            header.selected = track.id == self.selected_track_id
            self.scene.addItem(header)
            self._header_items.append(header)

            for clip in track.sorted_clips():
                self._add_clip_item(clip, row, track, offline_cache)

        scene_height = self._scene_height()
        self.scene.setSceneRect(0, 0, self._scene_width(), scene_height)

        self.playhead_item = PlayheadItem(scene_height)
        self.playhead_item.setX(HEADER_WIDTH + self.playhead_time * self.pixels_per_second)
        self.scene.addItem(self.playhead_item)

        self.snap_guide_item = self._make_snap_guide_item(scene_height)
        self.scene.addItem(self.snap_guide_item)

        for marker in self.project.markers:
            marker_item = MarkerItem(marker.id, marker.time, marker.name, marker.color, scene_height, self.signals)
            marker_item.setX(HEADER_WIDTH + marker.time * self.pixels_per_second)
            self.scene.addItem(marker_item)
            self._marker_items.append(marker_item)

        self._highlight_selection()
        self._pin_header_items()
        self._update_empty_hint()

    def _scene_height(self) -> float:
        return self.row_layout.total_height() + 40

    def _scene_width(self) -> float:
        total_seconds = max(self.project.total_duration if self.project else 0.0, 60.0)
        return max(total_seconds * self.pixels_per_second + 400 + HEADER_WIDTH, 1200 + HEADER_WIDTH)

    def refresh_clip(self, clip_id: str) -> None:
        """Bring one clip's item up to date after a property edit (width, badges, name).

        The Properties panel's commits used to leave the timeline stale
        (e.g. a Speed change kept the old width) until an unrelated edit
        rebuilt the scene.
        """
        if self.project is None:
            return
        clip = self.project.find_clip(clip_id)
        item = self._clip_items.get(clip_id)
        if clip is None or item is None:
            self.rebuild()
            return
        item.set_source_clip(clip)
        self._refresh_transition_issues(clip_id)
        self.ruler_item.set_scale(self.pixels_per_second, max(self.project.total_duration, 60.0))
        self.scene.setSceneRect(0, 0, self._scene_width(), self._scene_height())

    def _refresh_transition_issues(self, clip_id: str) -> None:
        """A clip's edit can make its neighbours' transitions join or not (F14)."""
        for track in self.project.tracks:
            if any(c.id == clip_id for c in track.clips):
                issues = track_transition_issues(track)
                for clip in track.clips:
                    item = self._clip_items.get(clip.id)
                    if item is not None:
                        item.set_transition_issue(issues.get(clip.id))

    def _update_empty_hint(self) -> None:
        has_any_clip = self.project is not None and any(track.clips for track in self.project.tracks)
        self.empty_hint_label.setVisible(not has_any_clip)
        self._reposition_empty_hint()

    def _reposition_empty_hint(self) -> None:
        self.empty_hint_label.setGeometry(self.view.viewport().rect())

    def _make_snap_guide_item(self, height: float) -> QGraphicsLineItem:
        """A thin vertical line shown while dragging/trimming near a snap target."""
        item = QGraphicsLineItem(0, 0, 0, height)
        pen = QPen(QColor(theme.COLOR_ACCENT))
        pen.setWidth(2)
        pen.setStyle(Qt.DashLine)
        item.setPen(pen)
        item.setZValue(45)
        item.setVisible(False)
        return item

    def _pin_header_items(self, _value: int = 0) -> None:
        """Keep the track-header column and corner plate glued to the viewport's left edge.

        Called on every horizontal scroll and right after :meth:`rebuild`.
        The view applies no scale transform, so the horizontal scrollbar's
        value is exactly the scene-x coordinate currently at the viewport's
        left edge — setting each pinned item's scene x to that value keeps
        it visually fixed while everything else scrolls underneath it.
        """
        x = self.view.horizontalScrollBar().value()
        try:
            for header in self._header_items:
                header.setX(x)
            if hasattr(self, "corner_item"):
                self.corner_item.setX(x)
        except RuntimeError:
            # A scene rebuild is in progress and this item has already been
            # deleted (e.g. scene.clear()/setSceneRect() can synchronously
            # trigger a scrollbar valueChanged mid-rebuild) — rebuild() always
            # calls this again once it finishes, so it's safe to just skip.
            pass

    def _on_snap_guide_show(self, seconds: float) -> None:
        self.snap_guide_item.setX(HEADER_WIDTH + seconds * self.pixels_per_second)
        self.snap_guide_item.setVisible(True)

    def _on_snap_guide_hide(self) -> None:
        self.snap_guide_item.setVisible(False)

    def _on_snap_toggled(self, checked: bool) -> None:
        self.snap_enabled = checked
        if not checked:
            self._on_snap_guide_hide()

    def _on_ripple_toggled(self, checked: bool) -> None:
        self.ripple_enabled = checked

    @staticmethod
    def _is_offline(clip: Clip, cache: Dict[str, bool]) -> bool:
        if clip.media_type == MediaType.TEXT or not clip.source_path:
            return False
        if clip.source_path not in cache:
            cache[clip.source_path] = not os.path.isfile(clip.source_path)
        return cache[clip.source_path]

    def _add_clip_item(self, clip: Clip, row: int, track: Optional[Track] = None,
                       offline_cache: Optional[Dict[str, bool]] = None) -> None:
        thumb = self._get_thumbnail(clip)
        filmstrip = self._get_filmstrip(clip)
        waveform = self._get_waveform(clip)
        item = ClipItem(
            clip,
            row,
            self.pixels_per_second,
            self.signals,
            thumb,
            filmstrip=filmstrip,
            waveform=waveform,
            snap_targets_provider=self._snap_targets,
            track_name=track.name if track is not None else "",
            fps=self._fps(),
            offline=self._is_offline(clip, offline_cache if offline_cache is not None else {}),
            layout=self.row_layout,
        )
        item.razor_mode = self.razor_mode
        if track is not None and track.locked:
            item.set_locked(True)
        if track is not None:
            item.set_transition_issue(track_transition_issues(track).get(clip.id))
        self.scene.addItem(item)
        self._clip_items[clip.id] = item

    def _get_thumbnail(self, clip: Clip) -> Optional[QPixmap]:
        """The clip's thumbnail, scaled once to the clip height and cached (it was rescaled on every paint)."""
        if clip.media_type not in (MediaType.VIDEO, MediaType.IMAGE) or self.get_thumbnail_for_clip is None:
            return None
        pixmap = self.get_thumbnail_for_clip(clip)
        if pixmap is None or pixmap.isNull():
            return pixmap
        key = f"{clip.source_path}|{pixmap.cacheKey()}"
        cached = self._thumbnail_cache.get(key)
        if cached is None:
            cached = pixmap.scaledToHeight(TRACK_HEIGHT - 4, Qt.SmoothTransformation)
            self._thumbnail_cache[key] = cached
        return cached

    def _get_filmstrip(self, clip: Clip) -> Optional[QPixmap]:
        if clip.media_type != MediaType.VIDEO or self.get_filmstrip_for_clip is None:
            return None
        pixmap = self.get_filmstrip_for_clip(clip)
        if pixmap is None or pixmap.isNull():
            return pixmap
        key = f"{clip.source_path}|filmstrip|{pixmap.cacheKey()}"
        cached = self._filmstrip_cache.get(key)
        if cached is None:
            cached = pixmap
            self._filmstrip_cache[key] = cached
        return cached

    def _get_waveform(self, clip: Clip):
        if clip.media_type not in (MediaType.AUDIO, MediaType.VIDEO):
            return None
        if self.get_waveform_for_clip is not None:
            return self.get_waveform_for_clip(clip)
        return None

    def _snap_targets(self, exclude_clip_id: str) -> List[float]:
        """Timeline positions (seconds) a dragged/trimmed clip should snap to.

        Includes 0:00, the playhead, every marker, and every other clip's
        start/end across all tracks. Returns an empty list when snapping is
        switched off.
        """
        if not self.snap_enabled or self.project is None:
            return []
        targets = [0.0, self.playhead_time]
        targets.extend(marker.time for marker in self.project.markers)
        for track in self.project.tracks:
            for clip in track.clips:
                if clip.id == exclude_clip_id:
                    continue
                targets.append(clip.timeline_start)
                targets.append(clip.timeline_start + clip.duration)
        return targets

    def _row_for_track_id(self, track_id: str) -> Optional[int]:
        for row, track in enumerate(self._row_tracks):
            if track.id == track_id:
                return row
        return None

    def _track_for_row(self, row: int, kind: TrackType) -> Optional[Track]:
        compatible_rows = [(r, t) for r, t in enumerate(self._row_tracks) if t.track_type == kind]
        if not compatible_rows:
            return None
        row = max(0, min(row, len(self._row_tracks) - 1))
        return min(compatible_rows, key=lambda rt: abs(rt[0] - row))[1]

    # ------------------------------------------------------------------
    # Zoom / playhead
    # ------------------------------------------------------------------
    @staticmethod
    def _pps_for_slider(value: int) -> float:
        return MIN_PPS * (MAX_PPS / MIN_PPS) ** (value / 100.0)

    @staticmethod
    def _slider_for_pps(pps: float) -> int:
        import math

        return int(round(100.0 * math.log(pps / MIN_PPS) / math.log(MAX_PPS / MIN_PPS)))

    def _on_zoom_slider(self, value: int) -> None:
        self.set_zoom(self._pps_for_slider(value))

    def _on_zoom_factor(self, factor: float, anchor_viewport_x: float = -1.0) -> None:
        self.set_zoom(self.pixels_per_second * factor, anchor_viewport_x)

    def zoom_in(self) -> None:
        self._on_zoom_factor(1.25)

    def zoom_out(self) -> None:
        self._on_zoom_factor(0.8)

    def zoom_to_fit(self) -> None:
        if self.project is None:
            return
        available = max(100, self.view.viewport().width() - HEADER_WIDTH - 20)
        self.set_zoom(available / max(self.project.total_duration, 1.0))
        self.view.horizontalScrollBar().setValue(0)

    def set_zoom(self, pixels_per_second: float, anchor_viewport_x: float = -1.0) -> None:
        """Change the zoom in place, keeping the anchor where it is on screen.

        The anchor is the viewport x under the mouse (wheel zoom) or, with
        ``anchor_viewport_x < 0``, the playhead. Items are re-scaled rather
        than the whole scene rebuilt on every slider tick/wheel step, and
        the scroll position is recomputed so the view no longer jumps.
        """
        new_pps = max(MIN_PPS, min(MAX_PPS, pixels_per_second))
        scrollbar = self.view.horizontalScrollBar()
        old_pps = self.pixels_per_second
        if anchor_viewport_x < 0:
            anchor_time = self.playhead_time
            anchor_viewport_x = HEADER_WIDTH + anchor_time * old_pps - scrollbar.value()
            if not (HEADER_WIDTH <= anchor_viewport_x <= self.view.viewport().width()):
                anchor_viewport_x = max(HEADER_WIDTH, self.view.viewport().width() / 2)
                anchor_time = max(0.0, (scrollbar.value() + anchor_viewport_x - HEADER_WIDTH) / old_pps)
        else:
            anchor_time = max(0.0, (scrollbar.value() + anchor_viewport_x - HEADER_WIDTH) / old_pps)

        self.pixels_per_second = new_pps
        for item in self._clip_items.values():
            item.prepareGeometryChange()
            item.pixels_per_second = new_pps
            item._sync_position()
            item.update()
        total_seconds = max(self.project.total_duration if self.project else 0.0, 60.0)
        self.ruler_item.set_scale(new_pps, total_seconds)
        self.ruler_item.update()
        self.playhead_item.setX(HEADER_WIDTH + self.playhead_time * new_pps)
        for marker_item in self._marker_items:
            marker_item.setX(HEADER_WIDTH + marker_item.time * new_pps)
        self.snap_guide_item.setVisible(False)
        self.scene.setSceneRect(0, 0, self._scene_width(), self._scene_height())
        scrollbar.setValue(zoom_scroll_value(anchor_time, new_pps, anchor_viewport_x))
        self._pin_header_items()

        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(self._slider_for_pps(new_pps))
        self.zoom_slider.blockSignals(False)

    def set_playhead(self, seconds: float) -> None:
        self.playhead_time = max(0.0, seconds)
        self.playhead_item.setX(HEADER_WIDTH + self.playhead_time * self.pixels_per_second)
        self.playhead_changed.emit(self.playhead_time)

    def move_playhead_marker(self, seconds: float) -> None:
        """Move only the playhead line (drag-scrub feedback between throttled seeks)."""
        self.playhead_item.setX(HEADER_WIDTH + max(0.0, seconds) * self.pixels_per_second)

    # ------------------------------------------------------------------
    # Keyboard transport / navigation (frame-accurate, project fps)
    # ------------------------------------------------------------------
    def _fps(self) -> float:
        return self.project.settings.fps if self.project is not None else 30.0

    def step_frames(self, frames: int) -> None:
        self.set_playhead(step_frames(self.playhead_time, self._fps(), frames))

    def step_seconds(self, seconds: float) -> None:
        self.set_playhead(snap_to_frame(self.playhead_time + seconds, self._fps()))

    def go_to_start(self) -> None:
        self.set_playhead(0.0)

    def go_to_end(self) -> None:
        if self.project is not None:
            self.set_playhead(self.project.total_duration)

    def go_to_edit_point(self, direction: int) -> None:
        if self.project is None:
            return
        target = adjacent_point(edit_points(self.project), self.playhead_time, direction)
        if target is not None:
            self.set_playhead(target)

    def go_to_marker(self, direction: int) -> None:
        if self.project is None:
            return
        target = adjacent_point(marker_points(self.project), self.playhead_time, direction)
        if target is not None:
            self.set_playhead(target)

    def set_in_point(self) -> None:
        self.in_point = self.playhead_time
        if self.out_point is not None and self.out_point <= self.in_point:
            self.out_point = None
        self.ruler_item.set_in_out(self.in_point, self.out_point)

    def set_out_point(self) -> None:
        self.out_point = self.playhead_time
        if self.in_point is not None and self.in_point >= self.out_point:
            self.in_point = None
        self.ruler_item.set_in_out(self.in_point, self.out_point)

    def set_render_ranges(self, ranges: list) -> None:
        """Preview renders to mark on the ruler (kept across rebuilds, like In/Out)."""
        self.render_ranges = list(ranges)
        self.ruler_item.set_render_ranges(self.render_ranges)

    def clear_in_out(self) -> None:
        self.in_point = self.out_point = None
        self.ruler_item.set_in_out(None, None)

    def in_out_range(self):
        """``(start, end)`` for the Export dialog's Trim Range, or None when neither point is set."""
        if self.in_point is None and self.out_point is None:
            return None
        start = self.in_point or 0.0
        end = self.out_point if self.out_point is not None else (self.project.total_duration if self.project else 0.0)
        return (start, end) if end > start else None

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    def _on_clip_selected(self, clip_id: str) -> None:
        """Plain click: select only this clip -- unless it is already part of a
        multi-selection, which then stays selected so the group can be dragged."""
        if clip_id not in self.selected_clip_ids or len(self.selected_clip_ids) <= 1:
            self.selected_clip_ids = {clip_id}
        self.selected_clip_id = clip_id
        self._select_track_header(None)
        self._highlight_selection()
        self.selection_changed.emit(clip_id)

    def _on_clip_toggle_selected(self, clip_id: str) -> None:
        """Ctrl+click: add the clip to, or remove it from, the selection."""
        if clip_id in self.selected_clip_ids:
            self.selected_clip_ids.discard(clip_id)
            if self.selected_clip_id == clip_id:
                self.selected_clip_id = next(iter(self.selected_clip_ids), None)
        else:
            self.selected_clip_ids.add(clip_id)
            self.selected_clip_id = clip_id
        self._highlight_selection()
        self.selection_changed.emit(self.selected_clip_id)

    def select_clips(self, clip_ids) -> None:
        """Programmatic selection (also used by Select All and after paste)."""
        ids = [cid for cid in clip_ids if cid in self._clip_items]
        self.selected_clip_ids = set(ids)
        self.selected_clip_id = ids[-1] if ids else None
        self._highlight_selection()
        self.selection_changed.emit(self.selected_clip_id)

    def select_all(self) -> None:
        self.select_clips(list(self._clip_items))

    def _on_scene_selection_changed(self) -> None:
        """Rubber-band selection (and Qt's own click handling) -> our selection set."""
        if self._syncing_selection:
            return
        try:
            ids = {item.clip_id for item in self.scene.selectedItems() if isinstance(item, ClipItem)}
        except RuntimeError:
            return
        if ids == self.selected_clip_ids:
            return
        self.selected_clip_ids = ids
        if self.selected_clip_id not in ids:
            self.selected_clip_id = next(iter(sorted(ids)), None) if ids else None
            self.selection_changed.emit(self.selected_clip_id)

    def selected_ids(self) -> List[str]:
        """The selection, primary clip included even if the set is empty (older call sites)."""
        ids = set(self.selected_clip_ids)
        if self.selected_clip_id:
            ids.add(self.selected_clip_id)
        return sorted(ids, key=lambda cid: self.project.find_clip(cid).timeline_start
                      if self.project is not None and self.project.find_clip(cid) else 0.0)

    def _on_track_selected(self, row: int) -> None:
        if 0 <= row < len(self._row_tracks):
            self._select_track_header(self._row_tracks[row].id)

    def _select_track_header(self, track_id: Optional[str]) -> None:
        self.selected_track_id = track_id
        for header in self._header_items:
            header.set_selected(header.track_id == track_id)

    def _highlight_selection(self) -> None:
        selected = set(self.selected_clip_ids)
        if self.selected_clip_id:
            selected.add(self.selected_clip_id)
        self._syncing_selection = True
        try:
            for cid, item in self._clip_items.items():
                item.setSelected(cid in selected)
        finally:
            self._syncing_selection = False

    # ------------------------------------------------------------------
    # Editing operations
    # ------------------------------------------------------------------
    def add_media_to_timeline(self, asset: MediaAsset, track_id: Optional[str] = None, at_time: Optional[float] = None) -> None:
        if self.project is None or self.history is None:
            return

        if asset.has_video or asset.is_image:
            media_type = MediaType.IMAGE if asset.is_image else MediaType.VIDEO
            kind = TrackType.VIDEO
        else:
            media_type = MediaType.AUDIO
            kind = TrackType.AUDIO

        track = self.project.find_track(track_id) if track_id else None
        if track is None:
            candidates = [t for t in self.project.tracks if t.track_type == kind]
            track = candidates[0] if candidates else None

        start = at_time if at_time is not None else (track.duration if track is not None else 0.0)
        length = asset.duration if asset.duration > 0 else 5.0

        clip = Clip(
            media_type=media_type,
            source_path=asset.path,
            source_in=0.0,
            source_out=length,
            timeline_start=start,
            source_duration=asset.duration if asset.duration > 0 else None,
            asset_id=asset.id,
        )
        # Lands on the requested track if the spot is free, else another
        # track of the same kind where it is, else a new track -- all one
        # undo step (see core.commands.build_add_clip_command).
        self.history.execute(
            build_add_clip_command(self.project, clip, kind, track.id if track is not None else None, self._row_tracks)
        )
        self.rebuild()
        self._emit_modified()

    def handle_media_drop(self, asset_id: str, at_time: float, row: int) -> None:
        if self.project is None:
            return
        asset = self.project.find_asset(asset_id)
        if asset is None:
            return
        kind = TrackType.AUDIO if (asset.has_audio and not asset.has_video) else TrackType.VIDEO
        target_track = self._track_for_row(row, kind) if self._row_tracks else None
        self.add_media_to_timeline(asset, track_id=target_track.id if target_track else None, at_time=at_time)

    def add_text_clip(self, at_time: Optional[float] = None) -> Optional[str]:
        if self.project is None or self.history is None:
            return None
        start = at_time if at_time is not None else self.playhead_time
        clip = Clip(media_type=MediaType.TEXT, source_in=0.0, source_out=4.0, timeline_start=start)
        # Top-most video track first (text is drawn over everything anyway);
        # never on top of an existing clip.
        video_tracks = [t for t in self.project.tracks if t.track_type == TrackType.VIDEO]
        order = list(reversed(video_tracks))
        preferred = order[0].id if order else None
        self.history.execute(build_add_clip_command(self.project, clip, TrackType.VIDEO, preferred, order))
        self.selected_clip_id = clip.id
        self.rebuild()
        self._emit_modified()
        self.selection_changed.emit(clip.id)
        return clip.id

    def add_marker_at_playhead(self) -> None:
        if self.project is None or self.history is None:
            return
        default_name = f"Marker {len(self.project.markers) + 1}"
        name, ok = QInputDialog.getText(self, "Add Marker", "Marker name:", QLineEdit.Normal, default_name)
        if not ok:
            return
        self.history.execute(AddMarkerCommand(self.project, self.playhead_time, name.strip() or default_name))
        self.rebuild()
        self._emit_modified()

    def _on_marker_clicked(self, marker_id: str) -> None:
        if self.project is None:
            return
        marker = self.project.find_marker(marker_id)
        if marker is not None:
            self.set_playhead(marker.time)

    def _on_marker_rename_requested(self, marker_id: str, current_name: str) -> None:
        if self.project is None or self.history is None:
            return
        new_name, ok = QInputDialog.getText(self, "Rename Marker", "Marker name:", QLineEdit.Normal, current_name)
        if not ok or not new_name.strip() or new_name.strip() == current_name:
            return
        self.history.execute(RenameMarkerCommand(self.project, marker_id, new_name.strip()))
        self.rebuild()
        self._emit_modified()

    def _on_marker_color_requested(self, marker_id: str, color: str) -> None:
        if self.project is None or self.history is None:
            return
        marker = self.project.find_marker(marker_id)
        if marker is None or marker.color.lower() == color.lower():
            return
        self.history.execute(RecolorMarkerCommand(self.project, marker_id, color))
        self.rebuild()
        self._emit_modified()

    def _on_marker_delete_requested(self, marker_id: str) -> None:
        if self.project is None or self.history is None:
            return
        self.history.execute(RemoveMarkerCommand(self.project, marker_id))
        self.rebuild()
        self._emit_modified()

    def add_track(self, track_type: TrackType) -> None:
        if self.project is None or self.history is None:
            return
        self.history.execute(AddTrackCommand(self.project, track_type))
        self.rebuild()
        self._emit_modified()

    def delete_selected_track(self) -> None:
        """Delete the track whose header was clicked, else the selected clip's track.

        Used to require a selected clip, so an empty track could never be deleted.
        """
        if self.project is None or self.history is None:
            return
        track = self.project.find_track(self.selected_track_id) if self.selected_track_id else None
        if track is None and self.selected_clip_id is not None:
            track = self.project.find_clip_track(self.selected_clip_id)
        if track is None:
            return
        self.history.execute(RemoveTrackCommand(self.project, track.id))
        if self.selected_clip_id is not None and self.project.find_clip(self.selected_clip_id) is None:
            self.selected_clip_id = None
            self.selection_changed.emit(None)
        self.selected_track_id = None
        self.rebuild()
        self._emit_modified()

    def split_selected(self) -> None:
        if self.project is None or self.history is None or self.selected_clip_id is None:
            return
        self._split_clip(self.selected_clip_id, self.playhead_time)

    def delete_selected(self) -> None:
        if self.project is None or self.history is None:
            return
        ids = self.selected_ids()
        if not ids:
            return
        if len(ids) == 1:
            if self.ripple_enabled:
                command = build_ripple_delete_command(self.project, ids[0])
            else:
                command = RemoveClipCommand(self.project, ids[0])
        else:
            command = build_delete_clips_command(self.project, ids, ripple=self.ripple_enabled)
        self.history.execute(command)
        self.selected_clip_id = None
        self.selected_clip_ids = set()
        self.rebuild()
        self._emit_modified()
        self.selection_changed.emit(None)

    # ------------------------------------------------------------------
    # Clipboard (F6)
    # ------------------------------------------------------------------
    def copy_selected(self) -> bool:
        if self.project is None:
            return False
        payload = copy_clips(self.project, self.selected_ids())
        if payload is None:
            return False
        self._clipboard_payload = payload
        try:
            mime = QMimeData()
            mime.setData(CLIPBOARD_MIME, json.dumps(payload).encode("utf-8"))
            QGuiApplication.clipboard().setMimeData(mime)
        except Exception:  # noqa: BLE001 - the in-app copy above still works
            pass
        self.status_message.emit(f"Copied {len(payload['clips'])} clip(s).")
        return True

    def cut_selected(self) -> None:
        if self.copy_selected():
            self.delete_selected()

    def _clipboard(self) -> Optional[dict]:
        try:
            mime = QGuiApplication.clipboard().mimeData()
            if mime is not None and mime.hasFormat(CLIPBOARD_MIME):
                return json.loads(bytes(mime.data(CLIPBOARD_MIME)).decode("utf-8"))
        except Exception:  # noqa: BLE001
            pass
        return self._clipboard_payload

    def _execute_and_select(self, command, select_new: bool = True) -> bool:
        if command is None or self.project is None or self.history is None:
            return False
        before = {c.id for t in self.project.tracks for c in t.clips}
        self.history.execute(command)
        self.rebuild()
        self._emit_modified()
        if select_new:
            new = [c.id for t in self.project.tracks for c in t.clips if c.id not in before]
            if new:
                self.select_clips(new)
        return True

    def paste_at_playhead(self) -> bool:
        if self.project is None:
            return False
        ok = self._execute_and_select(build_paste_command(self.project, self._clipboard(), self.playhead_time))
        if not ok:
            self.status_message.emit("Nothing to paste.")
        return ok

    def duplicate_selected(self) -> bool:
        if self.project is None:
            return False
        return self._execute_and_select(build_duplicate_command(self.project, self.selected_ids()))

    def paste_attributes(self) -> bool:
        if self.project is None:
            return False
        command = build_paste_attributes_command(self.project, self._clipboard(), self.selected_ids())
        ok = self._execute_and_select(command, select_new=False)
        if ok:
            for cid in self.selected_ids():
                self.refresh_clip(cid)
        return ok

    # ------------------------------------------------------------------
    # NLE edits (F7)
    # ------------------------------------------------------------------
    def set_razor_mode(self, enabled: bool) -> None:
        """Razor tool (C): clicking a clip splits it at the click instead of selecting it."""
        self.razor_mode = bool(enabled)
        for item in self._clip_items.values():
            item.razor_mode = self.razor_mode
        self.view.viewport().setCursor(Qt.CrossCursor if self.razor_mode else Qt.ArrowCursor)
        self.status_message.emit("Razor tool on: click a clip to cut it." if enabled else "Razor tool off.")

    def _on_clip_razor(self, clip_id: str, seconds: float) -> None:
        if self.project is None or self.history is None:
            return
        self._split_clip(clip_id, seconds)

    def _split_clip(self, clip_id: str, seconds: float) -> None:
        """Split ``clip_id`` at ``seconds`` -- refused (with a message) inside a freeze-frame tail."""
        clip = self.project.find_clip(clip_id)
        if clip is not None and clip.in_freeze_frame(seconds - clip.timeline_start) and seconds < clip.timeline_end:
            self.status_message.emit("Can't split inside a freeze frame: drag the clip's right edge to shorten it.")
            return
        self.history.execute(SplitClipCommand(self.project, clip_id, seconds))
        self.rebuild()
        self._emit_modified()

    def _clip_for_asset(self, asset: MediaAsset, start: float) -> Clip:
        media_type = MediaType.IMAGE if asset.is_image else (MediaType.VIDEO if asset.has_video else MediaType.AUDIO)
        length = asset.duration if asset.duration > 0 else 5.0
        source_in = 0.0
        # Match Frame (F28): the next Insert/Overwrite of the matched file starts at the matched frame.
        matched = getattr(self, "matched_source", None)
        if matched is not None and matched[0] == asset.id and media_type != MediaType.IMAGE and matched[1] < length - 0.1:
            source_in = matched[1]
            self.matched_source = None
        return Clip(media_type=media_type, source_path=asset.path, source_in=source_in, source_out=length, timeline_start=start,
                    source_duration=asset.duration if asset.duration > 0 else None, asset_id=asset.id)

    # ------------------------------------------------------------------
    # Premiere-style commands (F28)
    # ------------------------------------------------------------------
    def _in_out_or_message(self):
        if self.in_point is None or self.out_point is None or self.out_point <= self.in_point:
            self.status_message.emit("Set an In point (I) and an Out point (O) first.")
            return None
        return self.in_point, self.out_point

    def lift_in_out(self) -> bool:
        """Lift (;): remove In-Out from every unlocked track, leaving a gap."""
        span = self._in_out_or_message() if self.project is not None else None
        if span is None:
            return False
        ok = self._execute_and_select(build_lift_command(self.project, *span), False)
        if not ok:
            self.status_message.emit("Nothing to lift between In and Out.")
        return ok

    def extract_in_out(self) -> bool:
        """Extract ('): remove In-Out from every unlocked track and close the gap."""
        span = self._in_out_or_message() if self.project is not None else None
        if span is None:
            return False
        ok = self._execute_and_select(build_extract_command(self.project, *span), False)
        if ok:
            self.out_point = None
            self.ruler_item.set_in_out(self.in_point, self.out_point)
            self.set_playhead(span[0])
        else:
            self.status_message.emit("Nothing to extract between In and Out.")
        return ok

    def add_edit_all_tracks(self) -> bool:
        """Add Edit to All Tracks (Shift+S): cut every clip under the playhead."""
        if self.project is None:
            return False
        ok = self._execute_and_select(build_add_edit_all_tracks(self.project, self.playhead_time), False)
        if not ok:
            self.status_message.emit("No clip under the playhead to cut.")
        return ok

    def extend_edit_selected(self) -> bool:
        """Extend Edit (E): move the selected clip's nearer edit point to the playhead."""
        if self.project is None or self.selected_clip_id is None:
            self.status_message.emit("Select a clip first.")
            return False
        ok = self._execute_and_select(build_extend_edit_command(self.project, self.selected_clip_id, self.playhead_time), False)
        if not ok:
            self.status_message.emit("Can't extend there: another clip is in the way, or there isn't enough media.")
        return ok

    def insert_asset_at_playhead(self, asset: MediaAsset, overwrite: bool = False) -> bool:
        """Insert (ripple every unlocked track) or overwrite media at the playhead on the selected/first track."""
        if self.project is None:
            return False
        clip = self._clip_for_asset(asset, self.playhead_time)
        kind = TrackType.AUDIO if clip.media_type == MediaType.AUDIO else TrackType.VIDEO
        track = self.project.find_track(self.selected_track_id) if self.selected_track_id else None
        if track is None or track.track_type != kind:
            track = self._track_for_row(0, kind) if self._row_tracks else None
        if overwrite:
            command = build_overwrite_command(self.project, clip, track.id) if track is not None else None
        else:
            command = build_insert_command(self.project, clip, track.id if track is not None else None)
        ok = self._execute_and_select(command)
        if not ok:
            self.status_message.emit("Couldn't place the media there (locked track?).")
        return ok

    def detach_audio_selected(self, has_audio=None) -> bool:
        if self.project is None or self.selected_clip_id is None:
            return False
        clip = self.project.find_clip(self.selected_clip_id)
        asset = self.project.asset_for_clip(clip) if clip is not None else None
        audible = has_audio if has_audio is not None else (asset.has_audio if asset is not None else True)
        ok = self._execute_and_select(build_detach_audio_command(self.project, self.selected_clip_id, audible), False)
        self.status_message.emit("Audio detached to an audio track (linked)." if ok else "This clip has no audio to detach.")
        return ok

    def roll_selected_to_playhead(self) -> bool:
        if self.project is None or self.selected_clip_id is None:
            return False
        ok = self._execute_and_select(build_roll_command(self.project, self.selected_clip_id, self.playhead_time), False)
        if not ok:
            self.status_message.emit("Roll needs a clip touching the selected clip's right edge, and enough media.")
        return ok

    def slip_selected(self, source_delta: Optional[float] = None) -> bool:
        if self.project is None or self.selected_clip_id is None:
            return False
        if source_delta is None:
            source_delta, ok = QInputDialog.getDouble(self, "Slip Clip", "Shift the source by (seconds):", 0.0, -3600, 3600, 2)
            if not ok:
                return False
        return self._execute_and_select(build_slip_command(self.project, self.selected_clip_id, source_delta), False)

    def slide_selected(self, delta: Optional[float] = None) -> bool:
        if self.project is None or self.selected_clip_id is None:
            return False
        if delta is None:
            delta, ok = QInputDialog.getDouble(self, "Slide Clip", "Move the clip by (seconds):", 0.0, -3600, 3600, 2)
            if not ok:
                return False
        ok = self._execute_and_select(build_slide_command(self.project, self.selected_clip_id, delta), False)
        if not ok:
            self.status_message.emit("Can't slide there: the neighbouring clips don't have enough media, or it would overlap.")
        return ok

    def ripple_move_selected(self, delta: Optional[float] = None) -> bool:
        """Move the selected clip and every later clip on its track (Shift+drag, or Edit menu)."""
        if self.project is None or self.selected_clip_id is None:
            return False
        if delta is None:
            delta, ok = QInputDialog.getDouble(
                self, "Ripple Move", "Move the clip and everything after it by (seconds):", 0.0, -3600, 3600, 2)
            if not ok:
                return False
        ok = self._execute_and_select(build_ripple_move_command(self.project, self.selected_clip_id, delta), False)
        if not ok:
            self.status_message.emit("Can't ripple move there: the clips would overlap an earlier clip or start before 0.")
        return ok

    def _on_clip_edit_gesture(self, clip_id: str, gesture: str, value: float) -> None:
        """A Ctrl/Alt/Shift drag finished on a clip (see ui.widgets.timeline_items.drag_mode_for).

        Ctrl+edge = roll, Alt+body = slip, Ctrl+Alt+body = slide, Shift+body
        = ripple move. Each becomes the same core command as its menu entry,
        so it is one undo step and refused (with a message) when impossible.
        """
        clip = self.project.find_clip(clip_id) if self.project is not None else None
        if clip is None or self.history is None:
            self.rebuild()
            return
        command, refusal = None, ""
        if gesture in ("roll_left", "roll_right"):
            edge = gesture.split("_")[1]
            original = clip.timeline_start if edge == "left" else clip.timeline_end
            if abs(value - original) > 1e-4:
                command = build_roll_at_edge(self.project, clip_id, edge, value)
                refusal = "Roll needs a clip touching that edge, and enough media on both sides."
        elif gesture == "slip" and abs(value) > 1e-4:
            command = build_slip_command(self.project, clip_id, value)
            refusal = "Can't slip this clip (only video/audio clips with spare media can slip)."
        elif gesture == "slide" and abs(value) > 1e-4:
            command = build_slide_command(self.project, clip_id, value)
            refusal = "Can't slide there: the neighbouring clips don't have enough media, or it would overlap."
        elif gesture == "ripple_move" and abs(value) > 1e-4:
            command = build_ripple_move_command(self.project, clip_id, value)
            refusal = "Can't ripple move there: the clips would overlap an earlier clip or start before 0."
        if command is not None:
            self.history.execute(command)
            self._emit_modified()
        elif refusal:
            self.status_message.emit(refusal)
        self.rebuild()

    def nudge_selected(self, frames: int) -> bool:
        """Move the selected clips by whole frames (Alt+Left / Alt+Right)."""
        if self.project is None or self.history is None:
            return False
        ids = self.selected_ids()
        command = build_move_clips_command(self.project, ids, frames / self._fps())
        return self._execute_and_select(command, False)

    def select_adjacent_clip(self, direction: int) -> None:
        """Tab / Shift+Tab: select the next/previous clip in time on the selected clip's track (or any track)."""
        if self.project is None:
            return
        track = self.project.find_clip_track(self.selected_clip_id) if self.selected_clip_id else None
        clips = track.sorted_clips() if track is not None else sorted(
            (c for t in self.project.tracks for c in t.clips), key=lambda c: c.timeline_start)
        if not clips:
            return
        ids = [c.id for c in clips]
        if self.selected_clip_id in ids:
            index = (ids.index(self.selected_clip_id) + (1 if direction > 0 else -1)) % len(ids)
        else:
            index = 0 if direction > 0 else len(ids) - 1
        self._on_clip_selected(ids[index])
        self.focus_clip(ids[index])

    def focus_clip(self, clip_id: str) -> None:
        """Give keyboard focus to a clip's item, scroll it into view and tell a screen reader about it."""
        item = self._clip_items.get(clip_id)
        if item is None:
            return
        self.view.ensureVisible(item)
        self.view.setFocus()
        item.setFocus()
        timeline_accessibility.notify_focus(item)

    def go_to_keyframe(self, direction: int) -> None:
        """Previous/next keyframe of the selected clip (F12)."""
        if self.project is None or self.selected_clip_id is None:
            return
        clip = self.project.find_clip(self.selected_clip_id)
        if clip is None:
            return
        target = adjacent_point(clip_keyframe_points(clip), self.playhead_time, direction)
        if target is not None:
            self.set_playhead(target)

    # ------------------------------------------------------------------
    # Clip context menu (F6)
    # ------------------------------------------------------------------
    def build_clip_menu(self, clip_id: str) -> QMenu:
        if clip_id not in self.selected_ids():
            self._on_clip_selected(clip_id)
        menu = QMenu(self)
        entries = (
            ("Split at Playhead", self.split_selected),
            ("Delete", self.delete_selected),
            ("Ripple Delete", self._ripple_delete_selected),
            (None, None),
            ("Copy", self.copy_selected),
            ("Cut", self.cut_selected),
            ("Paste at Playhead", self.paste_at_playhead),
            ("Duplicate", self.duplicate_selected),
            ("Paste Attributes", self.paste_attributes),
            (None, None),
            ("Detach Audio", self.detach_audio_selected),
            ("Roll Edit to Playhead", self.roll_selected_to_playhead),
            ("Slip...", self.slip_selected),
            ("Slide...", self.slide_selected),
            ("Ripple Move...", self.ripple_move_selected),
            (None, None),
            ("Reveal in Media Bin", lambda: self._reveal_asset(clip_id)),
            ("Properties", lambda: self.selection_changed.emit(clip_id)),
        )
        for text, slot in entries:
            if text is None:
                menu.addSeparator()
            else:
                menu.addAction(text).triggered.connect(lambda _checked=False, s=slot: s())
        return menu

    def _on_clip_context_menu(self, clip_id: str, screen_pos) -> None:
        self.build_clip_menu(clip_id).exec(screen_pos)

    def _ripple_delete_selected(self) -> None:
        previous = self.ripple_enabled
        self.ripple_enabled = True
        try:
            self.delete_selected()
        finally:
            self.ripple_enabled = previous

    def _reveal_asset(self, clip_id: str) -> None:
        clip = self.project.find_clip(clip_id) if self.project is not None else None
        asset = self.project.asset_for_clip(clip) if clip is not None else None
        if asset is not None:
            self.reveal_asset_requested.emit(asset.id)

    def _on_clip_moved(self, clip_id: str, new_start: float, new_row: int) -> None:
        if self.project is None or self.history is None:
            self.rebuild()
            return
        clip = self.project.find_clip(clip_id)
        if clip is None:
            self.rebuild()
            return
        group = set(linked_clip_ids(self.project, clip_id))
        if clip_id in self.selected_clip_ids and len(self.selected_clip_ids) > 1:
            group |= self.selected_clip_ids
            for cid in list(self.selected_clip_ids):
                group |= set(linked_clip_ids(self.project, cid))
        if len(group) > 1:
            # Group/linked move: every clip shifts by the same time on its own track.
            command = build_move_clips_command(self.project, group, max(0.0, new_start) - clip.timeline_start)
            if command is not None:
                self.history.execute(command)
                self._emit_modified()
            else:
                self.status_message.emit("Can't move the selection there: it would overlap another clip.")
            self._layout_key = None  # Qt moved the other selected items visually; redraw them all
            self.rebuild()
            return
        kind = TrackType.AUDIO if clip.media_type == MediaType.AUDIO else TrackType.VIDEO
        target_track = self._track_for_row(new_row, kind)
        if target_track is None:
            self.rebuild()
            return
        if target_track.locked or target_track.has_overlap(
            Clip(media_type=clip.media_type, timeline_start=max(0.0, new_start), source_out=clip.duration, source_in=0.0),
            ignore_id=clip.id,
        ):
            self.rebuild()  # snap back to original position
            return
        self.history.execute(MoveClipCommand(self.project, clip_id, new_start, target_track.id))
        self.rebuild()
        self._emit_modified()

    def _on_clip_trimmed(
        self, clip_id: str, source_in: float, source_out: float, timeline_start: float,
        freeze_frame_duration: Optional[float] = None,
    ) -> None:
        if self.project is None or self.history is None:
            self.rebuild()
            return
        track = self.project.find_clip_track(clip_id)
        clip = self.project.find_clip(clip_id)
        if track is None or clip is None:
            self.rebuild()
            return
        # A copy of the real clip (speed ramp included), not a constant-speed stand-in.
        probe = clip.with_source_range(source_in, source_out)
        probe.timeline_start = timeline_start
        if freeze_frame_duration is not None:
            probe.freeze_frame_duration = max(0.0, freeze_frame_duration)
        if self.ripple_enabled:
            # Clips at/after the clip's OLD end shift in lockstep with this
            # trim (see build_ripple_trim_command), so they can never newly
            # overlap it — only clips that stay put need checking, e.g.
            # trimming the left edge back past the previous clip.
            old_end = clip.timeline_end
            stationary = [c for c in track.clips if c.id != clip_id and c.timeline_start < old_end - 0.001]
            if any(probe.timeline_start < c.timeline_end and c.timeline_start < probe.timeline_end for c in stationary):
                self.rebuild()
                return
            self.history.execute(build_ripple_trim_command(
                self.project, clip_id, source_in, source_out, timeline_start, freeze_frame_duration))
        else:
            if track.has_overlap(probe, ignore_id=clip_id):
                self.rebuild()
                return
            self.history.execute(TrimClipCommand(
                self.project, clip_id, source_in, source_out, timeline_start, freeze_frame_duration))
        self.rebuild()
        self._emit_modified()

    def _toggle_track_flag(self, row: int, attribute: str, label: str) -> Optional[Track]:
        """Flip a boolean track field through History (so it's undoable), like every other edit."""
        if self.project is None or self.history is None or not self._row_tracks or row >= len(self._row_tracks):
            return None
        track = self._row_tracks[row]
        self.history.execute(
            UpdateTrackPropertyCommand(self.project, track.id, attribute, not getattr(track, attribute), label)
        )
        return track

    def _on_track_mute_toggled(self, row: int) -> None:
        if self._toggle_track_flag(row, "muted", "Toggle Track Mute") is None:
            return
        self.rebuild()
        self._emit_modified()

    def _on_track_lock_toggled(self, row: int) -> None:
        track = self._toggle_track_flag(row, "locked", "Toggle Track Lock")
        if track is None:
            return
        for clip in track.clips:
            item = self._clip_items.get(clip.id)
            if item:
                item.set_locked(track.locked)
        self.rebuild()
        self._emit_modified()

    def _on_track_hidden_toggled(self, row: int) -> None:
        if self._toggle_track_flag(row, "hidden", "Toggle Track Visibility") is None:
            return
        self.rebuild()
        self._emit_modified()

    def _on_track_duck_config_requested(self, row: int) -> None:
        """"Duck Audio Under..." picker: sidechain-compress this track's audio under another track's.

        Minimal, deliberately not-elaborate UI (matching the mute/lock/hide
        controls' own directness): a single combo-box dialog listing every
        other track by name, plus "None" to clear ducking. The actual
        threshold/ratio/attack/release stay at Track's defaults -- not
        exposed here, same scoping call as e.g. crossfade duration being the
        only exposed crossfade parameter.
        """
        if self.project is None or not self._row_tracks or row >= len(self._row_tracks):
            return
        track = self._row_tracks[row]
        other_tracks = [t for t in self.project.tracks if t.id != track.id]
        dialog = self.ducking_dialog_factory(track, other_tracks, self)
        if dialog.exec() != QDialog.Accepted:
            return
        changes = [
            UpdateTrackPropertyCommand(self.project, track.id, name, value, "Set Audio Ducking")
            for name, value in dialog.values().items()
            if getattr(track, name) != value
        ]
        if self.history is not None and changes:
            self.history.execute(changes[0] if len(changes) == 1 else CompositeCommand(changes, "Set Audio Ducking"))
        # Rebuild so the header's duck-toggle indicator (active/inactive)
        # reflects the new value immediately, the same way mute/lock/hide's
        # own handlers rebuild right after mutating their track field.
        self.rebuild()
        self._emit_modified()

    def ask_track_name(self, track: Track) -> Optional[str]:
        """The new name typed for ``track``, or None when cancelled (a seam for tests)."""
        name, ok = QInputDialog.getText(self, "Rename Track", "Track name:", text=track.name)
        return name if ok else None

    def rename_track(self, row: int) -> None:
        """Rename the track in ``row`` (header double-click or menu), undoably."""
        if self.project is None or self.history is None or not 0 <= row < len(self._row_tracks):
            return
        track = self._row_tracks[row]
        name = self.ask_track_name(track)
        name = (name or "").strip()
        if not name or name == track.name:
            return
        self.history.execute(UpdateTrackPropertyCommand(self.project, track.id, "name", name, "Rename Track"))
        self.rebuild()
        self._emit_modified()

    def move_track(self, row: int, direction: int) -> None:
        """Move the track in ``row`` one row up (-1) or down (+1) among tracks of
        its own kind. Video rows are drawn top layer first, so moving a video
        track up raises it in the stacking order."""
        if self.project is None or self.history is None or not 0 <= row < len(self._row_tracks):
            return
        target_row = row + direction
        if not 0 <= target_row < len(self._row_tracks):
            return
        track, neighbour = self._row_tracks[row], self._row_tracks[target_row]
        if neighbour.track_type != track.track_type:
            return  # video rows stay above audio rows
        new_index = self.project.tracks.index(neighbour)
        label = "Move Track Up" if direction < 0 else "Move Track Down"
        self.history.execute(MoveTrackCommand(self.project, track.id, new_index, label))
        self.selected_track_id = track.id
        self.rebuild()
        self._emit_modified()

    def set_track_height(self, row: int, pixels: int) -> None:
        """Header menu "Track Height": one of TRACK_HEIGHT_PRESETS, undoably."""
        if self.project is None or self.history is None or not 0 <= row < len(self._row_tracks):
            return
        track = self._row_tracks[row]
        if pixels not in TRACK_HEIGHT_PRESETS.values() or track.height == pixels:
            return
        self.history.execute(UpdateTrackPropertyCommand(self.project, track.id, "height", pixels, "Set Track Height"))
        self.rebuild()
        self._emit_modified()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selected()
        elif event.key() == Qt.Key_S and not (event.modifiers() & Qt.ControlModifier):
            self.split_selected()
        elif event.key() == Qt.Key_M and not (event.modifiers() & Qt.ControlModifier):
            self.add_marker_at_playhead()
        else:
            super().keyPressEvent(event)
