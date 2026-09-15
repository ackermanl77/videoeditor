"""The multi-track timeline editor: the heart of the editing UI."""

from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QGraphicsLineItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from core.clip import Clip, MediaType
from core.commands import (
    AddClipCommand,
    AddTrackCommand,
    MoveClipCommand,
    RemoveClipCommand,
    RemoveTrackCommand,
    SplitClipCommand,
    TrimClipCommand,
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
    PlayheadItem,
    RulerItem,
    TimelineSignals,
    TrackHeaderItem,
)

DEFAULT_PIXELS_PER_SECOND = 40.0
MIN_PPS = 5.0
MAX_PPS = 400.0


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

        self.signals = TimelineSignals()
        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(0)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(8, 6, 8, 6)
        toolbar.setSpacing(6)

        self.add_video_track_btn = QPushButton("+ Video Track")
        self.add_audio_track_btn = QPushButton("+ Audio Track")
        self.split_btn = QPushButton("Split")
        self.delete_btn = QPushButton("Delete")
        self.delete_track_btn = QPushButton("Delete Track")
        self.snap_btn = QPushButton("Snap")
        self.snap_btn.setCheckable(True)
        self.snap_btn.setChecked(True)
        self.snap_btn.setToolTip("Snap clips to the playhead, clip edges, and 0:00 while dragging or trimming")

        for btn in (self.add_video_track_btn, self.add_audio_track_btn, self.split_btn, self.delete_btn, self.delete_track_btn, self.snap_btn):
            toolbar.addWidget(btn)

        toolbar.addStretch(1)

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

        self._highlight_selection()
        self._pin_header_items()

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

        Includes 0:00, the playhead, and every other clip's start/end across
        all tracks. Returns an empty list when snapping is switched off.
        """
        if not self.snap_enabled or self.project is None:
            return []
        targets = [0.0, self.playhead_time]
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
        self.history.execute(RemoveClipCommand(self.project, self.selected_clip_id))
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
        else:
            super().keyPressEvent(event)
