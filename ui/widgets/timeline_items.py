"""QGraphicsItem subclasses that make up the timeline: clips, ruler, headers."""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, Signal, QObject
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsSceneMouseEvent,
    QMenu,
    QStyleOptionGraphicsItem,
    QWidget,
)

from core.clip import Clip, MediaType
from ui import theme

TRACK_HEIGHT = 64
TRACK_GAP = 2
RULER_HEIGHT = 28
HEADER_WIDTH = 130
HANDLE_WIDTH = 8
MIN_CLIP_PIXELS = 6
SNAP_THRESHOLD_PX = 8

# Waveform type alias: (peak amplitudes 0..1, peaks-per-second density).
WaveformData = Tuple[List[float], float]

CLIP_COLORS = {
    MediaType.VIDEO: theme.CLIP_COLOR_VIDEO,
    MediaType.AUDIO: theme.CLIP_COLOR_AUDIO,
    MediaType.IMAGE: theme.CLIP_COLOR_IMAGE,
    MediaType.TEXT: theme.CLIP_COLOR_TEXT,
}


class TimelineSignals(QObject):
    """Central signal hub since QGraphicsItem is not a QObject."""

    clip_selected = Signal(str)
    clip_moved = Signal(str, float, int)  # clip_id, new_start, new_track_row
    clip_trimmed = Signal(str, float, float, float)  # clip_id, in, out, start
    clip_double_clicked = Signal(str)
    playhead_dragged = Signal(float)
    track_mute_toggled = Signal(int)
    track_lock_toggled = Signal(int)
    track_hidden_toggled = Signal(int)
    snap_guide_show = Signal(float)  # timeline seconds where the guide should be drawn
    snap_guide_hide = Signal()
    marker_clicked = Signal(str)  # marker_id -> seek the playhead there
    marker_rename_requested = Signal(str, str)  # marker_id, new_name
    marker_delete_requested = Signal(str)  # marker_id


class ClipItem(QGraphicsObject):
    """A draggable, trimmable rectangle representing one timeline clip."""

    def __init__(
        self,
        clip: Clip,
        row: int,
        pixels_per_second: float,
        signals: TimelineSignals,
        thumbnail: Optional[QPixmap] = None,
        waveform: Optional[WaveformData] = None,
        snap_targets_provider: Optional[Callable[[str], List[float]]] = None,
    ):
        super().__init__()
        self.clip_id = clip.id
        self.row = row
        self.pixels_per_second = pixels_per_second
        self.signals = signals
        self.thumbnail = thumbnail
        self._waveform_peaks, self._waveform_density = waveform if waveform else (None, 0.0)
        self.snap_targets_provider = snap_targets_provider
        self._media_type = clip.media_type
        self._name = clip.name
        self._duration = clip.duration
        self._start = clip.timeline_start
        self._speed = clip.speed if clip.speed > 0 else 1.0
        self._source_in = clip.source_in
        self._source_out = clip.source_out
        self._source_duration = clip.source_duration
        self._locked = False

        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setZValue(10)
        self._drag_mode: Optional[str] = None  # None | "move" | "trim_left" | "trim_right"
        self._press_scene_x = 0.0
        self._press_start = 0.0
        self._press_duration = 0.0
        self._press_source_in = 0.0
        self._press_source_out = 0.0

        self._sync_position()

    # -- geometry -----------------------------------------------------
    def set_source_clip(self, clip: Clip) -> None:
        self._duration = clip.duration
        self._start = clip.timeline_start
        self._name = clip.name
        self._speed = clip.speed if clip.speed > 0 else 1.0
        self._source_in = clip.source_in
        self._source_out = clip.source_out
        self._source_duration = clip.source_duration
        self._sync_position()
        self.prepareGeometryChange()
        self.update()

    def _sync_position(self) -> None:
        y = RULER_HEIGHT + self.row * (TRACK_HEIGHT + TRACK_GAP)
        self.setPos(HEADER_WIDTH + self._start * self.pixels_per_second, y)

    def width_px(self) -> float:
        return max(MIN_CLIP_PIXELS, self._duration * self.pixels_per_second)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.width_px(), TRACK_HEIGHT)

    def set_locked(self, locked: bool) -> None:
        self._locked = locked
        self.setFlag(QGraphicsItem.ItemIsMovable, not locked)

    # -- painting -------------------------------------------------------
    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        rect = self.boundingRect()
        base_color = QColor(CLIP_COLORS.get(self._media_type, theme.CLIP_COLOR_VIDEO))

        gradient = QLinearGradient(0, 0, 0, rect.height())
        gradient.setColorAt(0, base_color.lighter(115))
        gradient.setColorAt(1, base_color.darker(115))
        painter.setRenderHint(QPainter.Antialiasing, True)

        is_selected = self.isSelected()
        pen = QPen(QColor(theme.COLOR_ACCENT) if is_selected else QColor(theme.COLOR_BORDER))
        pen.setWidth(2 if is_selected else 1)
        painter.setPen(pen)
        painter.setBrush(QBrush(gradient))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 5, 5)

        # Thumbnail strip (video/image clips only), clipped to rounded rect.
        if self.thumbnail is not None and rect.width() > 30:
            painter.save()
            thumb_rect = QRectF(2, 2, min(rect.width() - 4, 46), rect.height() - 4)
            painter.setClipRect(thumb_rect)
            painter.drawPixmap(thumb_rect.toRect(), self.thumbnail)
            painter.restore()

        # Waveform: full clip body for audio clips, a thin strip along the
        # bottom for video clips that happen to carry audio too.
        if self._waveform_peaks:
            if self._media_type == MediaType.AUDIO:
                self._paint_waveform(painter, rect, top=18.0, bottom=rect.height() - 3.0)
            else:
                self._paint_waveform(painter, rect, top=rect.height() - 16.0, bottom=rect.height() - 3.0)

        painter.setPen(QColor("#ffffff"))
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        text_x = 52 if (self.thumbnail is not None and rect.width() > 30) else 8
        text_rect = QRectF(text_x, 2, rect.width() - text_x - 4, 16)
        elided = painter.fontMetrics().elidedText(self._name, Qt.ElideRight, int(text_rect.width()))
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

        # Fade / trim handles indicator (subtle triangles at the corners).
        if rect.width() > 20:
            handle_color = QColor(255, 255, 255, 60)
            painter.setBrush(handle_color)
            painter.setPen(Qt.NoPen)
            painter.drawRect(QRectF(0, 0, HANDLE_WIDTH, rect.height()))
            painter.drawRect(QRectF(rect.width() - HANDLE_WIDTH, 0, HANDLE_WIDTH, rect.height()))

    def _paint_waveform(self, painter: QPainter, rect: QRectF, top: float, bottom: float) -> None:
        """Draw the portion of the cached peak data this clip's trim shows.

        Reads ``self._source_in``/``self._source_out`` live, so the plotted
        slice updates in real time while the clip is being trimmed.
        """
        if bottom - top < 4 or not self._waveform_peaks or self._waveform_density <= 0:
            return
        density = self._waveform_density
        total_peaks = len(self._waveform_peaks)
        idx_start = max(0, min(total_peaks, int(self._source_in * density)))
        idx_end = max(idx_start, min(total_peaks, int(self._source_out * density)))
        peaks = self._waveform_peaks[idx_start:idx_end]
        if not peaks:
            return

        width = int(rect.width())
        if width <= 2:
            return
        mid = (top + bottom) / 2.0
        half = (bottom - top) / 2.0
        painter.setPen(QPen(QColor(255, 255, 255, 140), 1))

        m = len(peaks)
        # Cap how many bars we draw regardless of pixel width — a waveform
        # preview doesn't need per-pixel resolution, and this keeps very
        # wide (heavily zoomed) clips fast to repaint.
        max_bars = min(width, 400)
        step_px = max(1, width // max_bars)
        for x in range(0, width, step_px):
            src_idx = int(x * m / width)
            amp = peaks[src_idx] if 0 <= src_idx < m else 0.0
            h = amp * half
            painter.drawLine(int(x), int(mid - h), int(x), int(mid + h))

    # -- interaction ------------------------------------------------------
    def hoverMoveEvent(self, event) -> None:
        x = event.pos().x()
        if x <= HANDLE_WIDTH or x >= self.boundingRect().width() - HANDLE_WIDTH:
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.setCursor(Qt.OpenHandCursor)
        super().hoverMoveEvent(event)

    # -- snapping -----------------------------------------------------
    def _snap_threshold_seconds(self) -> float:
        return SNAP_THRESHOLD_PX / self.pixels_per_second if self.pixels_per_second > 0 else 0.0

    def _snap_targets(self) -> List[float]:
        if self.snap_targets_provider is None:
            return []
        return self.snap_targets_provider(self.clip_id)

    def _nearest_target(self, t: float, targets: List[float]) -> Optional[float]:
        threshold = self._snap_threshold_seconds()
        best: Optional[float] = None
        best_dist = threshold
        for target in targets:
            dist = abs(t - target)
            if dist <= best_dist:
                best = target
                best_dist = dist
        return best

    def itemChange(self, change, value):
        if (
            change == QGraphicsItem.ItemPositionChange
            and self._drag_mode == "move"
            and not self._locked
        ):
            proposed_x = value.x()
            proposed_start_s = max(0.0, (proposed_x - HEADER_WIDTH) / self.pixels_per_second)
            end_s = proposed_start_s + self._duration
            targets = self._snap_targets()

            start_candidate = self._nearest_target(proposed_start_s, targets)
            end_candidate = self._nearest_target(end_s, targets)

            snapped_start: Optional[float] = None
            guide_t: Optional[float] = None
            if start_candidate is not None and end_candidate is not None:
                if abs(proposed_start_s - start_candidate) <= abs(end_s - end_candidate):
                    snapped_start, guide_t = start_candidate, start_candidate
                else:
                    snapped_start, guide_t = end_candidate - self._duration, end_candidate
            elif start_candidate is not None:
                snapped_start, guide_t = start_candidate, start_candidate
            elif end_candidate is not None:
                snapped_start, guide_t = end_candidate - self._duration, end_candidate

            if snapped_start is not None and guide_t is not None:
                self.signals.snap_guide_show.emit(guide_t)
                return QPointF(HEADER_WIDTH + max(0.0, snapped_start) * self.pixels_per_second, value.y())
            self.signals.snap_guide_hide.emit()
            clamped_x = max(HEADER_WIDTH, proposed_x)
            if clamped_x != proposed_x:
                return QPointF(clamped_x, value.y())
            return value
        return super().itemChange(change, value)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.signals.clip_selected.emit(self.clip_id)
        if self._locked:
            event.accept()
            return
        x = event.pos().x()
        width = self.boundingRect().width()
        if x <= HANDLE_WIDTH:
            self._drag_mode = "trim_left"
        elif x >= width - HANDLE_WIDTH:
            self._drag_mode = "trim_right"
        else:
            self._drag_mode = "move"
        self._press_scene_x = event.scenePos().x()
        self._press_start = self._start
        self._press_duration = self._duration
        self._press_source_in = self._source_in
        self._press_source_out = self._source_out
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._locked:
            return
        if self._drag_mode == "move":
            super().mouseMoveEvent(event)
            return

        delta_px = event.scenePos().x() - self._press_scene_x
        delta_timeline_s = delta_px / self.pixels_per_second
        delta_source_s = delta_timeline_s * self._speed
        min_source_len = 0.1 * self._speed

        if self._drag_mode == "trim_left":
            max_delta = self._press_source_out - self._press_source_in - min_source_len
            lower_bound = -self._press_source_in  # can't trim before source start
            lower_bound = max(lower_bound, -self._press_start)  # can't move timeline start below 0
            clamped = max(lower_bound, min(delta_source_s, max_delta))
            self._source_in = self._press_source_in + clamped
            self._start = self._press_start + clamped / self._speed
            self._duration = (self._press_source_out - self._source_in) / self._speed

            snap_t = self._nearest_target(self._start, self._snap_targets())
            if snap_t is not None:
                self.signals.snap_guide_show.emit(snap_t)
                snapped_clamped = max(lower_bound, min((snap_t - self._press_start) * self._speed, max_delta))
                self._source_in = self._press_source_in + snapped_clamped
                self._start = self._press_start + snapped_clamped / self._speed
                self._duration = (self._press_source_out - self._source_in) / self._speed
            else:
                self.signals.snap_guide_hide.emit()

            self._sync_position()
            self.prepareGeometryChange()
            self.update()
        elif self._drag_mode == "trim_right":
            min_delta = -(self._press_source_out - self._press_source_in - min_source_len)
            max_delta = None
            if self._source_duration is not None:
                max_delta = self._source_duration - self._press_source_out
            clamped = max(min_delta, delta_source_s)
            if max_delta is not None:
                clamped = min(clamped, max_delta)
            self._source_out = self._press_source_out + clamped
            self._duration = (self._source_out - self._source_in) / self._speed

            end_s = self._start + self._duration
            snap_t = self._nearest_target(end_s, self._snap_targets())
            if snap_t is not None:
                self.signals.snap_guide_show.emit(snap_t)
                snapped_clamped = (snap_t - self._start) * self._speed - (self._press_source_out - self._press_source_in)
                snapped_clamped = max(min_delta, snapped_clamped)
                if max_delta is not None:
                    snapped_clamped = min(snapped_clamped, max_delta)
                self._source_out = self._press_source_out + snapped_clamped
                self._duration = (self._source_out - self._source_in) / self._speed
            else:
                self.signals.snap_guide_hide.emit()

            self.prepareGeometryChange()
            self.update()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.signals.snap_guide_hide.emit()
        if self._locked:
            event.accept()
            return
        if self._drag_mode == "move":
            super().mouseReleaseEvent(event)
            new_row = self.row
            if self.scene() is not None:
                y = self.pos().y()
                new_row = max(0, round((y - RULER_HEIGHT) / (TRACK_HEIGHT + TRACK_GAP)))
            new_start = max(0.0, (self.pos().x() - HEADER_WIDTH) / self.pixels_per_second)
            self.signals.clip_moved.emit(self.clip_id, new_start, new_row)
        elif self._drag_mode in ("trim_left", "trim_right"):
            self.signals.clip_trimmed.emit(self.clip_id, self._source_in, self._source_out, self._start)
        self._drag_mode = None

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.signals.clip_double_clicked.emit(self.clip_id)
        super().mouseDoubleClickEvent(event)


class ToggleButtonItem(QGraphicsObject):
    """A tiny clickable square icon-button used in track headers."""

    clicked = Signal()

    def __init__(self, label: str, size: int = 18, parent: Optional[QGraphicsItem] = None):
        super().__init__(parent)
        self.label = label
        self.size = size
        self.active = False
        self.setAcceptHoverEvents(True)
        self._hover = False

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.size, self.size)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        bg = QColor(theme.COLOR_ACCENT) if self.active else QColor(theme.COLOR_BG_LIGHT)
        if self._hover:
            bg = bg.lighter(120)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(bg)
        painter.setPen(QPen(QColor(theme.COLOR_BORDER)))
        painter.drawRoundedRect(self.boundingRect(), 3, 3)
        painter.setPen(QColor("white") if self.active else QColor(theme.COLOR_TEXT_DIM))
        font = QFont()
        font.setPointSize(7)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(self.boundingRect(), Qt.AlignCenter, self.label)

    def hoverEnterEvent(self, event) -> None:
        self._hover = True
        self.update()

    def hoverLeaveEvent(self, event) -> None:
        self._hover = False
        self.update()

    def mousePressEvent(self, event) -> None:
        self.clicked.emit()
        event.accept()


class TrackHeaderItem(QGraphicsObject):
    """Sticky left-hand panel showing a track's name and mute/lock/hide controls.

    Lives in the same scene as the clips (so it scrolls vertically in sync
    with them for free), but its horizontal position is pinned to the
    viewport's left edge by :class:`~ui.widgets.timeline_widget.TimelineWidget`
    on every horizontal scroll, rather than living at a fixed scene x. Clip
    content itself starts at scene x = ``HEADER_WIDTH`` (see
    :meth:`ClipItem._sync_position`), so nothing else needs to be shifted.
    """

    def __init__(self, track_id: str, row: int, name: str, track_kind: str, signals: TimelineSignals):
        super().__init__()
        self.track_id = track_id
        self.row = row
        self.name = name
        self.track_kind = track_kind
        self.signals = signals
        self.setZValue(60)  # above clips, ruler and the playhead

        y = RULER_HEIGHT + row * (TRACK_HEIGHT + TRACK_GAP)
        self.setPos(0, y)

        self.mute_btn = ToggleButtonItem("M", parent=self)
        self.lock_btn = ToggleButtonItem("L", parent=self)
        self.hide_btn = ToggleButtonItem("H", parent=self)
        self.mute_btn.setPos(HEADER_WIDTH - 92, TRACK_HEIGHT - 24)
        self.lock_btn.setPos(HEADER_WIDTH - 68, TRACK_HEIGHT - 24)
        self.hide_btn.setPos(HEADER_WIDTH - 44, TRACK_HEIGHT - 24)

        self.mute_btn.clicked.connect(lambda: self.signals.track_mute_toggled.emit(self.row))
        self.lock_btn.clicked.connect(lambda: self.signals.track_lock_toggled.emit(self.row))
        self.hide_btn.clicked.connect(lambda: self.signals.track_hidden_toggled.emit(self.row))

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, HEADER_WIDTH, TRACK_HEIGHT)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)
        bg = QColor(theme.COLOR_BG_DARK)
        painter.fillRect(self.boundingRect(), bg)
        accent = QColor(theme.CLIP_COLOR_VIDEO if self.track_kind == "video" else theme.CLIP_COLOR_AUDIO)
        painter.fillRect(QRectF(0, 0, 4, TRACK_HEIGHT), accent)
        painter.setPen(QColor(theme.COLOR_BORDER))
        painter.drawLine(0, TRACK_HEIGHT, HEADER_WIDTH, TRACK_HEIGHT)

        painter.setPen(QColor(theme.COLOR_TEXT))
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(12, 6, HEADER_WIDTH - 24, 18), Qt.AlignLeft | Qt.AlignVCenter, self.name)


class CornerItem(QGraphicsObject):
    """Blank plate covering the top-left corner (above the headers, left of the ruler).

    Pinned to the viewport's left edge the same way :class:`TrackHeaderItem`
    is, so the ruler (which starts at scene x = ``HEADER_WIDTH``) never
    shows through underneath the sticky header column while scrolling.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setZValue(61)
        self.setPos(0, 0)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, HEADER_WIDTH, RULER_HEIGHT)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.fillRect(self.boundingRect(), QColor(theme.COLOR_BG_DARK))
        painter.setPen(QColor(theme.COLOR_BORDER))
        painter.drawLine(0, RULER_HEIGHT - 1, HEADER_WIDTH, RULER_HEIGHT - 1)
        painter.drawLine(HEADER_WIDTH - 1, 0, HEADER_WIDTH - 1, RULER_HEIGHT)


class PlayheadItem(QGraphicsObject):
    """The vertical red line + handle marking the current playback position."""

    def __init__(self, height: float):
        super().__init__()
        self._height = height
        self.setZValue(50)

    def set_height(self, height: float) -> None:
        self.prepareGeometryChange()
        self._height = height

    def boundingRect(self) -> QRectF:
        return QRectF(-6, 0, 12, self._height)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor("#ff4d4d"))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawLine(0, 0, 0, int(self._height))
        painter.setBrush(QColor("#ff4d4d"))
        painter.setPen(Qt.NoPen)
        points = [(-6, 0), (6, 0), (0, 10)]
        painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))


class MarkerItem(QGraphicsObject):
    """A named point on the shared timeline: a small flag in the ruler band
    plus a thin dashed line threading down through the tracks so its
    position is easy to line up against clips.

    Left-click seeks the playhead there; right-click opens a Rename/Delete
    context menu. Both go through ``TimelineSignals`` rather than mutating
    the project directly, so the caller (``TimelineWidget``) can route them
    through ``core.history.History`` for undo/redo, same as every other
    timeline edit.
    """

    _FLAG_HALF_WIDTH = 6
    _FLAG_HEIGHT = 10

    def __init__(self, marker_id: str, time: float, name: str, color: str, height: float, signals: TimelineSignals):
        super().__init__()
        self.marker_id = marker_id
        self.time = time
        self.name = name
        self.color = color
        self._height = height
        self.signals = signals
        self.setZValue(44)
        self.setAcceptHoverEvents(True)
        self.setToolTip(name)
        self.setCursor(Qt.PointingHandCursor)

    def boundingRect(self) -> QRectF:
        w = self._FLAG_HALF_WIDTH
        return QRectF(-w, 0, 2 * w, self._height)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)
        color = QColor(self.color)

        # Thin line down through the tracks so the marker's exact position
        # is easy to line up against clip edges below the ruler.
        line_pen = QPen(color)
        line_pen.setWidth(1)
        line_pen.setStyle(Qt.DashLine)
        painter.setPen(line_pen)
        painter.drawLine(0, RULER_HEIGHT, 0, int(self._height))

        # A small downward-pointing flag within the ruler band.
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        w, h = self._FLAG_HALF_WIDTH, self._FLAG_HEIGHT
        points = [(-w, 0), (w, 0), (w, h * 0.5), (0, h), (-w, h * 0.5)]
        painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.signals.marker_clicked.emit(self.marker_id)
        event.accept()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu()
        rename_action = menu.addAction("Rename...")
        delete_action = menu.addAction("Delete Marker")
        chosen = menu.exec(event.screenPos())
        if chosen == rename_action:
            self.signals.marker_rename_requested.emit(self.marker_id, self.name)
        elif chosen == delete_action:
            self.signals.marker_delete_requested.emit(self.marker_id)
        event.accept()


class RulerItem(QGraphicsObject):
    """Time ruler drawn across the top of the timeline scene."""

    def __init__(self, pixels_per_second: float, total_seconds: float):
        super().__init__()
        self.pixels_per_second = pixels_per_second
        self.total_seconds = max(total_seconds, 30.0)
        self.setZValue(40)

    def set_scale(self, pixels_per_second: float, total_seconds: float) -> None:
        self.prepareGeometryChange()
        self.pixels_per_second = pixels_per_second
        self.total_seconds = max(total_seconds, 30.0)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.total_seconds * self.pixels_per_second + 400, RULER_HEIGHT)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.fillRect(self.boundingRect(), QColor(theme.COLOR_BG_DARK))
        painter.setPen(QColor(theme.COLOR_BORDER))
        painter.drawLine(0, RULER_HEIGHT - 1, int(self.boundingRect().width()), RULER_HEIGHT - 1)

        interval = self._pick_interval()
        painter.setPen(QColor(theme.COLOR_TEXT_DIM))
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)

        t = 0.0
        while t <= self.total_seconds + interval:
            x = t * self.pixels_per_second
            painter.drawLine(int(x), RULER_HEIGHT - 8, int(x), RULER_HEIGHT - 1)
            minutes = int(t // 60)
            seconds = int(t % 60)
            painter.drawText(int(x) + 3, RULER_HEIGHT - 10, f"{minutes:02d}:{seconds:02d}")
            t += interval

    def _pick_interval(self) -> float:
        # Choose a tick interval so ticks land roughly every 60-100px.
        for candidate in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600):
            if candidate * self.pixels_per_second >= 70:
                return candidate
        return 600
