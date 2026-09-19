"""The multi-track timeline editor: the heart of the editing UI."""

from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QGraphicsLineItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
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
    RemoveClipCommand,
    RemoveMarkerCommand,
    RemoveTrackCommand,
    RenameMarkerCommand,
    SplitClipCommand,
    TrimClipCommand,
    build_ripple_delete_command,
    build_ripple_trim_command,
)
from core.history import History
from core.media_asset import MediaAsset
from core.project import Project
from core.track import Track, TrackType
from ui import theme
from ui.media_bin import MEDIA_MIME_TYPE
from ui.widgets.timeline_items import (
    HEADER_WIDTH,
    RULER_HEIGHT,
    TRACK_GAP,
    TRACK_HEIGHT,
    ClipItem,
    CornerItem,
    MarkerItem,
    PlayheadItem,
    RulerItem,
    TimelineSignals,
    TrackHeaderItem,
)

DEFAULT_PIXELS_PER_SECOND = 40.0
MIN_PPS = 5.0
MAX_PPS = 400.0


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
    zoom_requested = Signal(float)  # multiplicative factor

    def __init__(self, scene: QGraphicsScene, timeline_widget: "TimelineWidget"):
        super().__init__(scene)
        self._timeline_widget = timeline_widget
        self.setRenderHint(QPainter.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)

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
            self.zoom_requested.emit(factor)
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event) -> None:
        pos = self.mapToScene(event.pos())
        if event.button() == Qt.LeftButton and pos.y() < RULER_HEIGHT and pos.x() >= HEADER_WIDTH:
            seconds = max(0.0, (pos.x() - HEADER_WIDTH) / self._timeline_widget.pixels_per_second)
            self.seek_requested.emit(seconds)
            return
        super().mousePressEvent(event)

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
            row = max(0, round((scene_pos.y() - RULER_HEIGHT) / (TRACK_HEIGHT + TRACK_GAP)))
            self._timeline_widget.handle_media_drop(asset_id, seconds, row)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class TimelineWidget(QWidget):
    """Top-level timeline panel: toolbar + track headers + graphics view."""

    selection_changed = Signal(object)  # Optional[str] clip_id
    project_modified = Signal()
    playhead_changed = Signal(float)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project: Optional[Project] = None
        self.history: Optional[History] = None
        self.pixels_per_second = DEFAULT_PIXELS_PER_SECOND
        self.playhead_time = 0.0
        self.selected_clip_id: Optional[str] = None

        self._row_tracks: List[Track] = []
        self._clip_items: Dict[str, ClipItem] = {}
        self._header_items: List[TrackHeaderItem] = []
        self._thumbnail_cache: Dict[str, QPixmap] = {}
        self.get_thumbnail_for_clip = None  # injected callable: Clip -> Optional[QPixmap]
        self.get_waveform_for_clip = None  # injected callable: Clip -> Optional[WaveformData]
        self.snap_enabled = True
        self.ripple_enabled = False

        self.signals = TimelineSignals()
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

        self.signals.clip_selected.connect(self._on_clip_selected)
        self.signals.clip_moved.connect(self._on_clip_moved)
        self.signals.clip_trimmed.connect(self._on_clip_trimmed)
        self.signals.clip_double_clicked.connect(self._on_clip_selected)
        self.signals.track_mute_toggled.connect(self._on_track_mute_toggled)
        self.signals.track_lock_toggled.connect(self._on_track_lock_toggled)
        self.signals.track_hidden_toggled.connect(self._on_track_hidden_toggled)
        self.signals.snap_guide_show.connect(self._on_snap_guide_show)
        self.signals.snap_guide_hide.connect(self._on_snap_guide_hide)
        self.signals.marker_clicked.connect(self._on_marker_clicked)
        self.signals.marker_rename_requested.connect(self._on_marker_rename_requested)
        self.signals.marker_delete_requested.connect(self._on_marker_delete_requested)

    # ------------------------------------------------------------------
    def set_project(self, project: Project, history: History) -> None:
        self.project = project
        self.history = history
        self.selected_clip_id = None
        self.playhead_time = 0.0
        self.rebuild()

    def _emit_modified(self) -> None:
        self.project_modified.emit()

    # ------------------------------------------------------------------
    # Scene construction
    # ------------------------------------------------------------------
    def rebuild(self) -> None:
        if self.project is None:
            return
        self.scene.clear()
        self._clip_items.clear()
        self._header_items.clear()

        video_tracks = [t for t in self.project.tracks if t.track_type == TrackType.VIDEO]
        audio_tracks = [t for t in self.project.tracks if t.track_type == TrackType.AUDIO]
        # Higher-index video tracks are drawn on top in compositing, so show
        # them higher in the panel too (matches conventional NLE layout).
        self._row_tracks = list(reversed(video_tracks)) + audio_tracks

        total_seconds = max(self.project.total_duration, 60.0)
        self.ruler_item = RulerItem(self.pixels_per_second, total_seconds)
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
            header = TrackHeaderItem(track.id, row, track.name, track.track_type.value, self.signals)
            header.mute_btn.active = track.muted
            header.lock_btn.active = track.locked
            header.hide_btn.active = track.hidden
            self.scene.addItem(header)
            self._header_items.append(header)

            for clip in track.sorted_clips():
                self._add_clip_item(clip, row)

        scene_height = RULER_HEIGHT + len(self._row_tracks) * (TRACK_HEIGHT + TRACK_GAP) + 40
        scene_width = max(total_seconds * self.pixels_per_second + 400 + HEADER_WIDTH, 1200 + HEADER_WIDTH)
        self.scene.setSceneRect(0, 0, scene_width, scene_height)

        self.playhead_item = PlayheadItem(scene_height)
        self.playhead_item.setX(HEADER_WIDTH + self.playhead_time * self.pixels_per_second)
        self.scene.addItem(self.playhead_item)

        self.snap_guide_item = self._make_snap_guide_item(scene_height)
        self.scene.addItem(self.snap_guide_item)

        for marker in self.project.markers:
            marker_item = MarkerItem(marker.id, marker.time, marker.name, marker.color, scene_height, self.signals)
            marker_item.setX(HEADER_WIDTH + marker.time * self.pixels_per_second)
            self.scene.addItem(marker_item)

        self._highlight_selection()
        self._pin_header_items()
        self._update_empty_hint()

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

    def _add_clip_item(self, clip: Clip, row: int) -> None:
        thumb = self._get_thumbnail(clip)
        waveform = self._get_waveform(clip)
        item = ClipItem(
            clip,
            row,
            self.pixels_per_second,
            self.signals,
            thumb,
            waveform=waveform,
            snap_targets_provider=self._snap_targets,
        )
        self.scene.addItem(item)
        self._clip_items[clip.id] = item

    def _get_thumbnail(self, clip: Clip) -> Optional[QPixmap]:
        if clip.media_type not in (MediaType.VIDEO, MediaType.IMAGE):
            return None
        if self.get_thumbnail_for_clip is not None:
            return self.get_thumbnail_for_clip(clip)
        return None

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
    def _on_zoom_slider(self, value: int) -> None:
        t = value / 100.0
        self.pixels_per_second = MIN_PPS * (MAX_PPS / MIN_PPS) ** t
        self.rebuild()

    def _on_zoom_factor(self, factor: float) -> None:
        self.pixels_per_second = max(MIN_PPS, min(MAX_PPS, self.pixels_per_second * factor))
        self.rebuild()

    def zoom_in(self) -> None:
        self._on_zoom_factor(1.25)

    def zoom_out(self) -> None:
        self._on_zoom_factor(0.8)

    def set_playhead(self, seconds: float) -> None:
        self.playhead_time = max(0.0, seconds)
        self.playhead_item.setX(HEADER_WIDTH + self.playhead_time * self.pixels_per_second)
        self.playhead_changed.emit(self.playhead_time)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    def _on_clip_selected(self, clip_id: str) -> None:
        self.selected_clip_id = clip_id
        self._highlight_selection()
        self.selection_changed.emit(clip_id)

    def _highlight_selection(self) -> None:
        for cid, item in self._clip_items.items():
            item.setSelected(cid == self.selected_clip_id)

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
            track = candidates[0] if candidates else self.project.add_track(kind)

        start = at_time if at_time is not None else track.duration
        length = asset.duration if asset.duration > 0 else 5.0

        clip = Clip(
            media_type=media_type,
            source_path=asset.path,
            source_in=0.0,
            source_out=length,
            timeline_start=start,
            track_index=self.project.tracks.index(track),
            source_duration=asset.duration if asset.duration > 0 else None,
        )
        self.history.execute(AddClipCommand(self.project, track.id, clip))
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
        video_tracks = [t for t in self.project.tracks if t.track_type == TrackType.VIDEO]
        track = video_tracks[-1] if video_tracks else self.project.add_track(TrackType.VIDEO)
        start = at_time if at_time is not None else self.playhead_time
        clip = Clip(
            media_type=MediaType.TEXT,
            source_in=0.0,
            source_out=4.0,
            timeline_start=start,
            track_index=self.project.tracks.index(track),
        )
        self.history.execute(AddClipCommand(self.project, track.id, clip))
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
        if self.project is None or self.history is None or self.selected_clip_id is None:
            return
        track = self.project.find_clip_track(self.selected_clip_id)
        if track is None:
            return
        self.history.execute(RemoveTrackCommand(self.project, track.id))
        self.selected_clip_id = None
        self.rebuild()
        self._emit_modified()

    def split_selected(self) -> None:
        if self.project is None or self.history is None or self.selected_clip_id is None:
            return
        self.history.execute(SplitClipCommand(self.project, self.selected_clip_id, self.playhead_time))
        self.rebuild()
        self._emit_modified()

    def delete_selected(self) -> None:
        if self.project is None or self.history is None or self.selected_clip_id is None:
            return
        if self.ripple_enabled:
            command = build_ripple_delete_command(self.project, self.selected_clip_id)
        else:
            command = RemoveClipCommand(self.project, self.selected_clip_id)
        self.history.execute(command)
        self.selected_clip_id = None
        self.rebuild()
        self._emit_modified()
        self.selection_changed.emit(None)

    def _on_clip_moved(self, clip_id: str, new_start: float, new_row: int) -> None:
        if self.project is None or self.history is None:
            self.rebuild()
            return
        clip = self.project.find_clip(clip_id)
        if clip is None:
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

    def _on_clip_trimmed(self, clip_id: str, source_in: float, source_out: float, timeline_start: float) -> None:
        if self.project is None or self.history is None:
            self.rebuild()
            return
        track = self.project.find_clip_track(clip_id)
        clip = self.project.find_clip(clip_id)
        if track is None or clip is None:
            self.rebuild()
            return
        probe = Clip(
            media_type=clip.media_type,
            timeline_start=timeline_start,
            source_in=source_in,
            source_out=source_out,
            speed=clip.speed,
        )
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
            self.history.execute(build_ripple_trim_command(self.project, clip_id, source_in, source_out, timeline_start))
        else:
            if track.has_overlap(probe, ignore_id=clip_id):
                self.rebuild()
                return
            self.history.execute(TrimClipCommand(self.project, clip_id, source_in, source_out, timeline_start))
        self.rebuild()
        self._emit_modified()

    def _on_track_mute_toggled(self, row: int) -> None:
        if not self._row_tracks or row >= len(self._row_tracks):
            return
        track = self._row_tracks[row]
        track.muted = not track.muted
        self.rebuild()
        self._emit_modified()

    def _on_track_lock_toggled(self, row: int) -> None:
        if not self._row_tracks or row >= len(self._row_tracks):
            return
        track = self._row_tracks[row]
        track.locked = not track.locked
        for clip in track.clips:
            item = self._clip_items.get(clip.id)
            if item:
                item.set_locked(track.locked)
        self.rebuild()
        self._emit_modified()

    def _on_track_hidden_toggled(self, row: int) -> None:
        if not self._row_tracks or row >= len(self._row_tracks):
            return
        track = self._row_tracks[row]
        track.hidden = not track.hidden
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
