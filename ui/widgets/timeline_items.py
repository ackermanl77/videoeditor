"""QGraphicsItem subclasses that make up the timeline: clips, ruler, headers."""

from __future__ import annotations

import math
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, Signal, QObject
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
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
from core.project import MARKER_COLORS
from core.timeline_nav import clip_keyframe_points, format_timecode
from core.track import TRACK_HEIGHT_PRESETS
from core.waveform import display_gains
from ui import theme

TRACK_HEIGHT = 64
TRACK_GAP = 2
RULER_HEIGHT = 28
HEADER_WIDTH = 130
HANDLE_WIDTH = 8
MIN_CLIP_PIXELS = 6
SNAP_THRESHOLD_PX = 8

# Waveform type alias: (peak amplitudes, RMS amplitudes, peaks-per-second density).
WaveformData = Tuple[List[float], List[float], float]

CLIP_COLORS = {
    MediaType.VIDEO: theme.CLIP_COLOR_VIDEO,
    MediaType.AUDIO: theme.CLIP_COLOR_AUDIO,
    MediaType.IMAGE: theme.CLIP_COLOR_IMAGE,
    MediaType.TEXT: theme.CLIP_COLOR_TEXT,
}

# Small top-right badges on a ClipItem showing which effect categories are
# active. Each entry is (Clip attribute to check, badge letter, badge
# color). The attribute checked is one of `Clip.has_*` in `core/clip.py` --
# the exact same "is this non-default" logic `core/filter_graph.py` uses to
# decide whether to emit a filter for that field, so the badges can never
# show/hide out of step with what the export actually does. Order here is
# the order badges are drawn, left to right.
EFFECT_INDICATORS: Tuple[Tuple[str, str, str], ...] = (
    ("has_color_correction", "C", theme.COLOR_DANGER),
    ("has_chroma_key", "K", theme.COLOR_SUCCESS),
    ("has_denoise", "N", theme.COLOR_ACCENT),
    ("has_transition", "T", theme.CLIP_COLOR_TEXT),
    ("has_effects", "FX", "#8b6cd9"),
    ("has_volume_automation", "V", "#d9822b"),
    ("is_reversed", "R", "#3fa7d6"),
    ("has_freeze_frame", "FF", "#5c6bc0"),
)
# Full names for the badge tooltips (badges used to be colour + one letter only).
EFFECT_NAMES = {
    "C": "Color Correction", "K": "Chroma Key", "N": "Noise Reduction", "T": "Transition",
    "FX": "Effects", "V": "Volume Automation", "R": "Reverse", "FF": "Freeze Frame",
}
EFFECT_BADGE_SIZE = 14
EFFECT_BADGE_GAP = 2
EFFECT_BADGE_FONT_PT = 8
CLIP_LABEL_FONT_PT = 9
MEDIA_KIND_NAMES = {MediaType.VIDEO: "Video", MediaType.AUDIO: "Audio", MediaType.IMAGE: "Image", MediaType.TEXT: "Text"}


#: Drag gestures that are not plain move/trim: the timeline turns each into a
#: core edit command on release (see TimelineWidget._on_clip_edit_gesture).
EDIT_GESTURES = ("roll_left", "roll_right", "slip", "slide", "ripple_move")
GESTURE_HINTS = {
    "roll_left": "Roll edit (Ctrl+drag edge): this clip's in point and the previous clip's out point move together",
    "roll_right": "Roll edit (Ctrl+drag edge): this clip's out point and the next clip's in point move together",
    "slip": "Slip (Alt+drag): change which part of the source plays; the clip stays in place",
    "slide": "Slide (Ctrl+Alt+drag): move the clip; its neighbours trim to keep the gap closed",
    "ripple_move": "Ripple move (Shift+drag): this clip and every later clip on the track move together",
}


def drag_mode_for(x: float, width: float, modifiers) -> str:
    """The drag a mouse press starts at item-local ``x`` with these keyboard modifiers.

    Edges: plain = trim, Ctrl = roll with the touching neighbour. Body:
    plain = move, Alt = slip, Ctrl+Alt = slide, Shift = ripple move.
    (Ctrl+click in the body still toggles the selection; see mousePressEvent.)
    """
    ctrl = bool(modifiers & Qt.ControlModifier)
    alt = bool(modifiers & Qt.AltModifier)
    shift = bool(modifiers & Qt.ShiftModifier)
    if x <= HANDLE_WIDTH or x >= width - HANDLE_WIDTH:
        side = "left" if x <= HANDLE_WIDTH else "right"
        return f"roll_{side}" if ctrl else f"trim_{side}"
    if alt:
        return "slide" if ctrl else "slip"
    if shift:
        return "ripple_move"
    return "toggle_select" if ctrl else "move"


class RowLayout:
    """Top y and height of every track row: rows can have different heights
    (``Track.height``). Rows past the last one continue at ``TRACK_HEIGHT``,
    so a drop or drag below the tracks still lands on "the next row" (a new
    track). Shared by the timeline with its clip and header items, which
    read their geometry from it."""

    def __init__(self, heights=()):
        self.set_heights(heights)

    def set_heights(self, heights) -> None:
        self._heights = [int(h) for h in heights]
        self._tops = []
        y = float(RULER_HEIGHT)
        for h in self._heights:
            self._tops.append(y)
            y += h + TRACK_GAP
        self._end = y  # first y below the last row

    @property
    def heights(self) -> Tuple[int, ...]:
        return tuple(self._heights)

    def height(self, row: int) -> int:
        return self._heights[row] if 0 <= row < len(self._heights) else TRACK_HEIGHT

    def top(self, row: int) -> float:
        if 0 <= row < len(self._tops):
            return self._tops[row]
        return self._end + (row - len(self._tops)) * (TRACK_HEIGHT + TRACK_GAP)

    def total_height(self) -> float:
        return self._end

    def row_at(self, scene_y: float) -> int:
        """The row under ``scene_y`` (``floor``-like: anywhere on a row or the gap below it)."""
        if scene_y >= self._end:  # below the last row: extrapolate
            return len(self._tops) + int(math.floor((scene_y - self._end) / (TRACK_HEIGHT + TRACK_GAP)))
        for row in range(len(self._tops) - 1, -1, -1):
            if scene_y >= self._tops[row]:
                return row
        return 0

    def nearest_row(self, top_y: float) -> int:
        """The row whose top is nearest ``top_y`` (where a dragged clip's top edge settles)."""
        candidates = range(len(self._tops) + 1)  # one past the end: drag onto a new track
        return min(candidates, key=lambda row: abs(self.top(row) - top_y))


def row_at_scene_y(scene_y: float, layout: Optional[RowLayout] = None) -> int:
    """Track row under a scene y coordinate (e.g. the mouse position of a drop).

    ``floor``, not ``round``: rounding sent a drop on the lower half of a
    track to the next one. Without ``layout`` every row is ``TRACK_HEIGHT``.
    """
    if layout is not None and layout.heights:
        return max(0, layout.row_at(scene_y))
    return max(0, int(math.floor((scene_y - RULER_HEIGHT) / (TRACK_HEIGHT + TRACK_GAP))))


def _active_effect_badges(clip: Clip) -> List[Tuple[str, str]]:
    """Return (letter, color) for every active effect category on ``clip``."""
    return [(letter, color) for attr, letter, color in EFFECT_INDICATORS if getattr(clip, attr)]


def clip_accessible_name(clip: Clip, track_name: str = "", fps: float = 30.0) -> str:
    """What a screen reader announces for a clip, e.g. "Video clip Arbol.mp4, 00:00:12:00 to 00:00:20:00, track Video 1"."""
    text = (
        f"{MEDIA_KIND_NAMES.get(clip.media_type, 'Clip')} clip {clip.name}, "
        f"{format_timecode(clip.timeline_start, fps)} to {format_timecode(clip.timeline_end, fps)}"
    )
    return text + (f", track {track_name}" if track_name else "")


def clip_description(clip: Clip, track_name: str = "", fps: float = 30.0, offline: bool = False) -> str:
    """Spoken-style description of a clip: its tooltip and accessible description.

    e.g. "Video clip Arbol.mp4, 00:00:12:00 to 00:00:20:00, track Video 1. Effects: Color Correction."
    """
    text = clip_accessible_name(clip, track_name, fps) + "."
    badges = [EFFECT_NAMES[letter] for letter, _color in _active_effect_badges(clip)]
    if badges:
        text += " Effects: " + ", ".join(badges) + "."
    if clip.link_group:
        text += " Linked to another clip (they move together)."
    if offline:
        text += " OFFLINE: the source file is missing (File > Relink Media)."
    return text


class TimelineSignals(QObject):
    """Central signal hub since QGraphicsItem is not a QObject."""

    clip_selected = Signal(str)
    clip_toggle_selected = Signal(str)  # Ctrl+click: add to / remove from the selection
    clip_context_menu = Signal(str, object)  # clip id, global QPoint
    clip_razor = Signal(str, float)  # clip id, timeline seconds to split at (Razor tool)
    clip_moved = Signal(str, float, int)  # clip_id, new_start, new_track_row
    clip_trimmed = Signal(str, float, float, float, float)  # clip_id, in, out, start, freeze-frame seconds
    # clip_id, gesture (an EDIT_GESTURES name), value: the new edit time for a
    # roll, source seconds for a slip, timeline seconds moved for slide/ripple.
    clip_edit_gesture = Signal(str, str, float)
    clip_double_clicked = Signal(str)
    track_mute_toggled = Signal(int)
    track_lock_toggled = Signal(int)
    track_hidden_toggled = Signal(int)
    track_duck_config_requested = Signal(int)  # row -> right-clicked "Duck Audio Under..."
    track_selected = Signal(int)  # row -> header clicked (Delete Track acts on it)
    track_rename_requested = Signal(int)  # row -> header menu "Rename Track..."
    track_move_requested = Signal(int, int)  # row, -1 (up) / +1 (down) -> header menu "Move Track Up/Down"
    track_height_requested = Signal(int, int)  # row, pixels -> header menu "Track Height"
    snap_guide_show = Signal(float)  # timeline seconds where the guide should be drawn
    snap_guide_hide = Signal()
    marker_clicked = Signal(str)  # marker_id -> seek the playhead there
    marker_rename_requested = Signal(str, str)  # marker_id, new_name
    marker_delete_requested = Signal(str)  # marker_id
    marker_color_requested = Signal(str, str)  # marker_id, "#rrggbb" (F15)


class ClipItem(QGraphicsObject):
    """A draggable, trimmable rectangle representing one timeline clip."""

    def __init__(
        self,
        clip: Clip,
        row: int,
        pixels_per_second: float,
        signals: TimelineSignals,
        thumbnail: Optional[QPixmap] = None,
        filmstrip: Optional[QPixmap] = None,
        waveform: Optional[WaveformData] = None,
        snap_targets_provider: Optional[Callable[[str], List[float]]] = None,
        track_name: str = "",
        fps: float = 30.0,
        offline: bool = False,
        layout: Optional[RowLayout] = None,
    ):
        super().__init__()
        self.layout = layout if layout is not None else RowLayout()
        self.clip_id = clip.id
        self.track_name = track_name
        self.fps = fps
        self.offline = offline
        # Razor tool: a click splits the clip there instead of selecting/dragging.
        self.razor_mode = False
        self.row = row
        self.pixels_per_second = pixels_per_second
        self.signals = signals
        self.thumbnail = thumbnail
        self.filmstrip = filmstrip
        self._waveform_peaks, self._waveform_rms, self._waveform_density = waveform if waveform else (None, None, 0.0)
        self.snap_targets_provider = snap_targets_provider
        self._media_type = clip.media_type
        self._name = clip.name
        self._duration = clip.duration
        self._start = clip.timeline_start
        self._speed = clip.speed if clip.speed > 0 else 1.0
        self._source_in = clip.source_in
        self._source_out = clip.source_out
        self._source_duration = clip.source_duration
        self._reverse = clip.reverse
        self._freeze = clip.freeze_frame_duration
        self._locked = False
        self._effect_badges = _active_effect_badges(clip)
        self._transition_out = clip.transition_out
        self._transition_duration = clip.transition_duration
        # Why the transition out won't render (core.filter_graph.track_transition_issues), set by
        # the timeline, which knows the neighbours; None when it joins or there's none (F14).
        self.transition_issue: Optional[str] = None
        # A private copy of the model clip, so trim drags compute the
        # trimmed duration exactly as the model will (speed ramps included)
        # instead of assuming one constant speed.
        self._model = clip.with_source_range(clip.source_in, clip.source_out)
        self._keyframe_times = [t - clip.timeline_start for t in clip_keyframe_points(clip)]
        self._describe(clip)

        # Focusable so keyboard selection (Tab) moves real focus, which screen
        # readers follow (see ui/widgets/timeline_accessibility.py).
        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemIsFocusable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setZValue(10)
        # None | "move" | "trim_left" | "trim_right" | one of EDIT_GESTURES
        self._drag_mode: Optional[str] = None
        self._press_scene_x = 0.0
        self._press_start = 0.0
        self._press_duration = 0.0
        self._press_source_in = 0.0
        self._press_source_out = 0.0
        self._press_freeze = 0.0

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
        self._reverse = clip.reverse
        self._freeze = clip.freeze_frame_duration
        self._effect_badges = _active_effect_badges(clip)
        self._transition_out = clip.transition_out
        self._transition_duration = clip.transition_duration
        self._model = clip.with_source_range(clip.source_in, clip.source_out)
        self._keyframe_times = [t - clip.timeline_start for t in clip_keyframe_points(clip)]
        self._describe(clip)
        self._sync_position()
        self.prepareGeometryChange()
        self.update()

    def _describe(self, clip: Clip) -> None:
        """Tooltip and accessible name/description (the screen reader's view of this clip)."""
        self.accessible_name = clip_accessible_name(clip, self.track_name, self.fps)
        self.accessible_description = clip_description(clip, self.track_name, self.fps, self.offline)
        if self.transition_issue:
            self.accessible_description += f" Transition not applied: {self.transition_issue}."
        self.setToolTip(self.accessible_description)

    def set_transition_issue(self, reason: Optional[str]) -> None:
        """F14: mark a transition out that won't render, and say why in the tooltip."""
        if reason == self.transition_issue:
            return
        self.transition_issue = reason
        self._describe(self._model)
        self.update()

    def keyframe_times(self) -> List[float]:
        """Clip-local times of the keyframe ticks drawn on this clip."""
        return list(self._keyframe_times)

    def set_row(self, row: int) -> None:
        if row != self.row:
            self.prepareGeometryChange()  # rows can differ in height
            self.row = row
            self._sync_position()

    def active_effect_labels(self) -> List[str]:
        """The letters of every effect-indicator badge currently shown (left to right)."""
        return [letter for letter, _color in self._effect_badges]

    def _sync_position(self) -> None:
        self.setPos(HEADER_WIDTH + self._start * self.pixels_per_second, self.layout.top(self.row))

    def width_px(self) -> float:
        return max(MIN_CLIP_PIXELS, self._duration * self.pixels_per_second)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.width_px(), self.layout.height(self.row))

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

        # Filmstrip thumbnails on video clips; a single poster frame on images.
        if self.filmstrip is not None and rect.width() > 52:
            self._paint_filmstrip(painter, rect)
        elif self.thumbnail is not None and rect.width() > 30:
            painter.save()
            thumb_rect = QRectF(2, 2, min(rect.width() - 4, 46), rect.height() - 4)
            painter.setClipRect(thumb_rect)
            painter.drawPixmap(thumb_rect.toRect(), self.thumbnail)
            painter.restore()

        # Freeze-frame tail: hatched, so it reads as "held picture, no sound".
        freeze_px = min(rect.width(), max(0.0, self._freeze) * self.pixels_per_second)
        if freeze_px >= 2:
            painter.save()
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(255, 255, 255, 70), Qt.Dense6Pattern))
            painter.drawRect(QRectF(rect.width() - freeze_px, 1, freeze_px - 1, rect.height() - 2))
            painter.restore()

        # Waveform: full clip body for audio clips, a thin strip along the
        # bottom for video clips that happen to carry audio too.
        if self._waveform_peaks:
            if self._media_type == MediaType.AUDIO:
                self._paint_waveform(painter, rect, top=18.0, bottom=rect.height() - 3.0)
            else:
                self._paint_waveform(painter, rect, top=rect.height() - 16.0, bottom=rect.height() - 3.0)

        # Effect-indicator badges (top-right corner): a small colored/lettered
        # square per active effect category (Color Correction, Chroma Key,
        # Denoise, Transition) -- see EFFECT_INDICATORS. Skipped entirely
        # below a minimum width rather than drawn partially/overlapping, the
        # same "hide, don't crowd" guard used for the trim handles below.
        badge_reserve = 0.0
        show_badges = bool(self._effect_badges) and rect.width() >= 34
        if show_badges:
            n = len(self._effect_badges)
            wide = sum(1 for letter, _c in self._effect_badges if len(letter) > 1)
            badge_reserve = n * EFFECT_BADGE_SIZE + wide * 8 + (n - 1) * EFFECT_BADGE_GAP + 4

        if self.offline:
            # Offline media: red diagonal hatching over the whole clip.
            painter.save()
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(theme.COLOR_DANGER), Qt.BDiagPattern))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 5, 5)
            painter.restore()

        font = QFont()
        font.setPointSize(CLIP_LABEL_FONT_PT)
        font.setBold(True)
        painter.setFont(font)
        text_x = 52 if (self.thumbnail is not None and rect.width() > 30) else 8
        text_rect = QRectF(text_x, 2, max(0.0, rect.width() - text_x - 4 - badge_reserve), 16)
        label = f"{self._name} (offline)" if self.offline else self._name
        elided = painter.fontMetrics().elidedText(label, Qt.ElideRight, int(text_rect.width()))
        if elided and text_rect.width() > 4:
            # Dark translucent strip behind the name (see theme.CLIP_LABEL_STRIP_ALPHA).
            strip_width = min(text_rect.width(), painter.fontMetrics().horizontalAdvance(elided) + 8)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, theme.CLIP_LABEL_STRIP_ALPHA))
            painter.drawRoundedRect(QRectF(text_x - 4, 2, strip_width, 16), 3, 3)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

        self._paint_transition_mark(painter, rect)

        if show_badges:
            badge_font = QFont()
            badge_font.setPointSize(EFFECT_BADGE_FONT_PT)
            badge_font.setBold(True)
            painter.setFont(badge_font)
            x = rect.width() - badge_reserve + 2
            y = 2.0
            for letter, color in self._effect_badges:
                width = EFFECT_BADGE_SIZE + (8 if len(letter) > 1 else 0)
                badge_rect = QRectF(x, y, width, EFFECT_BADGE_SIZE)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(color))
                painter.drawRoundedRect(badge_rect, 2, 2)
                painter.setPen(QColor(theme.readable_text_color(color)))
                painter.drawText(badge_rect, Qt.AlignCenter, letter)
                x += width + EFFECT_BADGE_GAP

        # Keyframe ticks: a small diamond per keyframe along the bottom edge.
        if self._keyframe_times and self.pixels_per_second > 0:
            painter.setPen(QPen(QColor("#111111"), 1))
            painter.setBrush(QColor("#ffd54a"))
            y = rect.height() - 6
            for t in self._keyframe_times:
                x = t * self.pixels_per_second
                if 0 <= x <= rect.width():
                    painter.drawPolygon(QPolygonF([QPointF(x, y - 4), QPointF(x + 4, y), QPointF(x, y + 4), QPointF(x - 4, y)]))

        # Fade / trim handles indicator (subtle triangles at the corners).
        if rect.width() > 20:
            handle_color = QColor(255, 255, 255, 60)
            painter.setBrush(handle_color)
            painter.setPen(Qt.NoPen)
            painter.drawRect(QRectF(0, 0, HANDLE_WIDTH, rect.height()))
            painter.drawRect(QRectF(rect.width() - HANDLE_WIDTH, 0, HANDLE_WIDTH, rect.height()))

    def _paint_filmstrip(self, painter: QPainter, rect: QRectF) -> None:
        if self.filmstrip is None or self.filmstrip.isNull():
            return
        columns = max(1, self.filmstrip.width() // max(1, self.filmstrip.height()))
        tile_w = self.filmstrip.width() / columns
        band = QRectF(2, 2, max(0.0, self._playable_width(rect) - 4), min(rect.height() - 4, 26))
        if band.width() < 18:
            return
        painter.save()
        painter.setClipRect(band)
        frames = max(2, min(columns, int(band.width() // 22) + 1))
        clip_span = max(self._source_out - self._source_in, 0.001)
        # The strip's tiles span the whole source file (see
        # core.media_probe.generate_filmstrip), so a source time maps to
        # time / file length -- not / source_out, which showed the file's
        # last frames on any clip trimmed short of the end.
        file_length = max(self._source_duration or self._source_out, 0.001)
        for i in range(frames):
            frac = 0.0 if frames == 1 else i / max(1, frames - 1)
            shown = 1.0 - frac if self._reverse else frac
            src_frac = min(0.999, max(0.0, (self._source_in + clip_span * shown) / file_length))
            src_index = min(columns - 1, max(0, int(src_frac * columns)))
            src = QRectF(src_index * tile_w, 0, tile_w, self.filmstrip.height())
            x0 = band.left() + frac * band.width()
            x1 = band.right() if i == frames - 1 else band.left() + ((i + 1) / frames) * band.width()
            painter.drawPixmap(QRectF(x0, band.top(), max(1.0, x1 - x0), band.height()), self.filmstrip, src)
        painter.restore()

    def _playable_width(self, rect: QRectF) -> float:
        """Width of the part that plays footage (the clip minus its freeze-frame tail)."""
        return max(0.0, rect.width() - max(0.0, self._freeze) * self.pixels_per_second)

    def _paint_transition_mark(self, painter: QPainter, rect: QRectF) -> None:
        if getattr(self._transition_out, "value", "none") == "none" or rect.width() < 18:
            return
        width = min(20.0, max(8.0, self._transition_duration * self.pixels_per_second))
        x0 = rect.width() - width - 2.0
        y0 = 4.0
        y1 = min(rect.height() - 4.0, y0 + 18.0)
        painter.save()
        if self.transition_issue:
            # Not applied (F14): a solid warning-coloured cross with "!" -- the tooltip says why.
            painter.setPen(QPen(QColor(theme.COLOR_DANGER), 2, Qt.SolidLine))
        else:
            painter.setPen(QPen(QColor(255, 255, 255, 170), 1, Qt.DashLine))
        painter.drawLine(int(x0), int(y0), int(rect.width() - 2), int(y1))
        painter.drawLine(int(x0), int(y1), int(rect.width() - 2), int(y0))
        if self.transition_issue:
            font = painter.font()
            font.setBold(True)
            font.setPointSize(EFFECT_BADGE_FONT_PT)
            painter.setFont(font)
            painter.drawText(QRectF(x0 - 12.0, y0, 10.0, y1 - y0), Qt.AlignCenter, "!")
        painter.restore()

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
        rms_values = (self._waveform_rms or self._waveform_peaks)[idx_start:idx_end]
        if not peaks:
            return
        if self._reverse:  # a reversed clip plays (and sounds) back to front
            peaks, rms_values = peaks[::-1], rms_values[::-1]

        width = int(self._playable_width(rect))  # the freeze-frame tail is silent
        if width <= 2:
            return
        mid = (top + bottom) / 2.0
        half = (bottom - top) / 2.0
        painter.setPen(QPen(QColor(255, 255, 255, 165), 1))

        m = len(peaks)
        max_bars = min(width, 640)
        step_px = max(1, width // max_bars)
        # Drawn as it will sound (F11): scaled by the clip's volume / automation envelope, flat
        # when muted; a peak the gain pushes past full scale is red (it will clip).
        gains = display_gains(self._model, -(-width // step_px))
        for bar, x in enumerate(range(0, width, step_px)):
            a = int(x * m / width)
            b = max(a + 1, int((x + step_px) * m / width))
            gain = gains[bar] if bar < len(gains) else 1.0
            peak = max(peaks[a:b], default=0.0) * gain
            rms = sum(rms_values[a:b]) / max(1, b - a) * gain
            rms_h = min(rms, 1.0) * half
            peak_h = min(peak, 1.0) * half
            painter.setPen(QPen(QColor(255, 255, 255, 80), 1))
            painter.drawLine(int(x), int(mid - rms_h), int(x), int(mid + rms_h))
            painter.setPen(QPen(QColor(theme.COLOR_DANGER) if peak > 1.0 else QColor(255, 255, 255, 175), 1))
            painter.drawLine(int(x), int(mid - peak_h), int(x), int(mid + peak_h))

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
        if event.button() == Qt.RightButton:
            # Right-click opens the clip menu (it used to start a drag).
            event.accept()
            return
        if self.razor_mode and not self._locked:
            seconds = self._start + max(0.0, event.pos().x()) / self.pixels_per_second
            self.signals.clip_razor.emit(self.clip_id, seconds)
            event.accept()
            return
        mode = drag_mode_for(event.pos().x(), self.boundingRect().width(), event.modifiers())
        if mode == "toggle_select":
            self.signals.clip_toggle_selected.emit(self.clip_id)
            event.accept()
            return
        self.signals.clip_selected.emit(self.clip_id)
        if self._locked:
            event.accept()
            return
        self.begin_drag(mode, event.scenePos().x())
        if mode == "move":
            super().mousePressEvent(event)
        else:
            event.accept()  # trims and edit gestures are drawn by this item, not moved by Qt

    def begin_drag(self, mode: str, scene_x: float) -> None:
        """Start a drag (``drag_mode_for``'s result) at scene x ``scene_x``."""
        self._drag_mode = mode
        self._press_scene_x = scene_x
        self._press_start = self._start
        self._press_duration = self._duration
        self._press_source_in = self._source_in
        self._press_source_out = self._source_out
        self._press_freeze = self._freeze
        if mode in GESTURE_HINTS:
            self.setToolTip(GESTURE_HINTS[mode])

    def update_drag(self, scene_x: float) -> None:
        """Follow the mouse during a trim or edit gesture (a plain move is Qt's own item move)."""
        if self._drag_mode in (None, "move"):
            return
        delta_timeline_s = (scene_x - self._press_scene_x) / self.pixels_per_second
        mode = self._drag_mode
        if mode in ("trim_left", "roll_left"):
            self._apply_trim_left(delta_timeline_s)
            snap_t = self._nearest_target(self._start, self._snap_targets())
            if snap_t is not None:
                self.signals.snap_guide_show.emit(snap_t)
                self._apply_trim_left(snap_t - self._press_start)
            else:
                self.signals.snap_guide_hide.emit()
            self._sync_position()
        elif mode in ("trim_right", "roll_right"):
            self._apply_trim_right(delta_timeline_s)
            snap_t = self._nearest_target(self._start + self._duration, self._snap_targets())
            if snap_t is not None:
                self.signals.snap_guide_show.emit(snap_t)
                self._apply_trim_right(snap_t - self._start - self._press_duration)
            else:
                self.signals.snap_guide_hide.emit()
        elif mode in ("slide", "ripple_move"):
            self._start = max(0.0, self._press_start + delta_timeline_s)
            self._sync_position()
        elif mode == "slip":
            # Content moves with the mouse: dragging right shows earlier source
            # (later source on a reversed clip, which plays back to front).
            consumed = self._model.playback_source_consumed(delta_timeline_s)
            shift = consumed if self._model.reverse else -consumed
            self._source_in = self._press_source_in + shift
            self._source_out = self._press_source_out + shift
        self.prepareGeometryChange()
        self.update()

    def finish_drag(self) -> None:
        """End the drag: emit the trim or edit gesture for the timeline to apply (it rebuilds the item)."""
        mode, self._drag_mode = self._drag_mode, None
        self.signals.snap_guide_hide.emit()
        if mode in ("trim_left", "trim_right"):
            self.signals.clip_trimmed.emit(self.clip_id, self._source_in, self._source_out, self._start, self._freeze)
        elif mode == "roll_left":
            self.signals.clip_edit_gesture.emit(self.clip_id, mode, self._start)
        elif mode == "roll_right":
            self.signals.clip_edit_gesture.emit(self.clip_id, mode, self._start + self._duration)
        elif mode == "slip":
            self.signals.clip_edit_gesture.emit(self.clip_id, mode, self._source_in - self._press_source_in)
        elif mode in ("slide", "ripple_move"):
            self.signals.clip_edit_gesture.emit(self.clip_id, mode, self._start - self._press_start)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._locked:
            return
        if self._drag_mode == "move":
            super().mouseMoveEvent(event)
            return
        self.update_drag(event.scenePos().x())

    def _min_source_length(self) -> float:
        return 0.1 * self._model.speed_at(0.0)

    def _apply_trim_left(self, delta_timeline_s: float) -> None:
        """Move the left edge by ``delta_timeline_s`` (clamped), updating in/out/start/duration.

        Source and timeline offsets go through the model clip's own
        playback mapping (:meth:`Clip.playback_source_consumed`), so speed
        ramps are right and a reversed clip -- whose left edge is its
        ``source_out`` end -- trims the correct end. The resulting length is
        the trimmed clip's real ``Clip.duration`` (freeze frame included).
        """
        model = self._model
        delta_timeline_s = max(delta_timeline_s, -self._press_start)  # timeline start can't go below 0
        consumed = model.playback_source_consumed(delta_timeline_s)
        limit = self._source_duration if self._source_duration is not None else math.inf
        room = (limit - self._press_source_out) if model.reverse else self._press_source_in
        consumed = max(consumed, -room)  # can't extend past the media's start (reversed: its end)
        consumed = min(consumed, self._press_source_out - self._press_source_in - self._min_source_length())
        if model.reverse:
            self._source_in, self._source_out = self._press_source_in, self._press_source_out - consumed
        else:
            self._source_in, self._source_out = self._press_source_in + consumed, self._press_source_out
        self._freeze = self._press_freeze
        self._start = max(0.0, self._press_start + model.playback_timeline_offset(consumed))
        self._duration = model.with_source_range(self._source_in, self._source_out).duration

    def _apply_trim_right(self, delta_timeline_s: float) -> None:
        """Move the right edge by ``delta_timeline_s`` (clamped); see :meth:`_apply_trim_left`.

        The freeze-frame tail shrinks/grows first (:meth:`Clip.trim_right_range`);
        on a reversed clip the right edge is the ``source_in`` end.
        """
        model = self._model
        min_length = model.playback_timeline_offset(self._min_source_length())
        new_len = max(self._press_duration + delta_timeline_s, min_length)
        source_in, source_out, freeze = model.trim_right_range(new_len)
        if model.reverse:
            source_in = min(max(0.0, source_in), self._press_source_out - self._min_source_length())
        else:
            source_out = max(source_out, self._press_source_in + self._min_source_length())
            if self._source_duration is not None:
                source_out = min(source_out, self._source_duration)
        self._source_in, self._source_out, self._freeze = source_in, source_out, freeze
        probe = model.with_source_range(source_in, source_out)
        probe.freeze_frame_duration = freeze
        self._duration = probe.duration

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
                new_row = self.layout.nearest_row(y)
            new_start = max(0.0, (self.pos().x() - HEADER_WIDTH) / self.pixels_per_second)
            self._drag_mode = None
            self.signals.clip_moved.emit(self.clip_id, new_start, new_row)
            return
        self.finish_drag()

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.signals.clip_double_clicked.emit(self.clip_id)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.signals.clip_context_menu.emit(self.clip_id, event.screenPos())
        event.accept()


class ToggleButtonItem(QGraphicsObject):
    """A tiny clickable square icon-button used in track headers."""

    clicked = Signal()

    def __init__(self, label: str, size: int = 18, parent: Optional[QGraphicsItem] = None, description: str = ""):
        super().__init__(parent)
        self.label = label
        self.size = size
        self.description = description
        self.active = False
        self.setAcceptHoverEvents(True)
        self._hover = False
        self._refresh_tooltip()

    def set_active(self, active: bool) -> None:
        self.active = bool(active)
        self._refresh_tooltip()
        self.update()

    def _refresh_tooltip(self) -> None:
        if self.description:
            self.setToolTip(f"{self.description} ({'on' if self.active else 'off'})")

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
        font.setPointSize(8)
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
    """Sticky left-hand panel showing a track's name and mute/lock/hide/duck controls.

    Lives in the same scene as the clips (so it scrolls vertically in sync
    with them for free), but its horizontal position is pinned to the
    viewport's left edge by :class:`~ui.widgets.timeline_widget.TimelineWidget`
    on every horizontal scroll, rather than living at a fixed scene x. Clip
    content itself starts at scene x = ``HEADER_WIDTH`` (see
    :meth:`ClipItem._sync_position`), so nothing else needs to be shifted.

    ``duck_btn`` mirrors the mute/lock/hide toggles' own look but isn't a
    toggle in the same sense -- clicking it re-opens the "Duck Audio
    Under..." picker (the same ``track_duck_config_requested`` signal the
    right-click context menu already uses, see :meth:`contextMenuEvent`)
    rather than flipping a plain bool. Its ``active`` state (set by
    :class:`~ui.widgets.timeline_widget.TimelineWidget` from
    ``Track.duck_trigger_track_id``) is the only thing that makes an
    otherwise entirely invisible per-track setting visible at a glance.
    """

    def __init__(self, track_id: str, row: int, name: str, track_kind: str, signals: TimelineSignals,
                 layout: Optional[RowLayout] = None):
        super().__init__()
        self.track_id = track_id
        self.row = row
        self.name = name
        self.track_kind = track_kind
        self.signals = signals
        self.selected = False
        layout = layout if layout is not None else RowLayout()
        self.row_height = layout.height(row)
        self.setZValue(60)  # above clips, ruler and the playhead
        self.setPos(0, layout.top(row))
        buttons_y = self.row_height - 24

        # Letters stay compact; every toggle has a tooltip naming it and its state
        # ("H" was easily read as headphones or help).
        self.mute_btn = ToggleButtonItem("M", parent=self, description="Mute track audio")
        self.lock_btn = ToggleButtonItem("L", parent=self, description="Lock track against edits")
        self.hide_btn = ToggleButtonItem("H", parent=self, description="Hide track picture (audio still plays)")
        self.duck_btn = ToggleButtonItem("D", parent=self, description="Duck audio under another track (click to set)")
        self.mute_btn.setPos(HEADER_WIDTH - 92, buttons_y)
        self.lock_btn.setPos(HEADER_WIDTH - 70, buttons_y)
        self.hide_btn.setPos(HEADER_WIDTH - 48, buttons_y)
        self.duck_btn.setPos(HEADER_WIDTH - 26, buttons_y)

        self.mute_btn.clicked.connect(lambda: self.signals.track_mute_toggled.emit(self.row))
        self.lock_btn.clicked.connect(lambda: self.signals.track_lock_toggled.emit(self.row))
        self.hide_btn.clicked.connect(lambda: self.signals.track_hidden_toggled.emit(self.row))
        # Reuses the exact same signal/handler the right-click "Duck Audio
        # Under..." context-menu action emits (see contextMenuEvent below)
        # rather than duplicating the QInputDialog picker logic.
        self.duck_btn.clicked.connect(lambda: self.signals.track_duck_config_requested.emit(self.row))

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, HEADER_WIDTH, self.row_height)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)
        bg = QColor(theme.COLOR_BG_DARK)
        painter.fillRect(self.boundingRect(), bg)
        accent = QColor(theme.CLIP_COLOR_VIDEO if self.track_kind == "video" else theme.CLIP_COLOR_AUDIO)
        painter.fillRect(QRectF(0, 0, 4, self.row_height), accent)
        painter.setPen(QColor(theme.COLOR_BORDER))
        painter.drawLine(0, self.row_height, HEADER_WIDTH, self.row_height)

        if self.selected:
            pen = QPen(QColor(theme.COLOR_ACCENT))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(self.boundingRect().adjusted(1, 1, -1, -1))

        painter.setPen(QColor(theme.COLOR_TEXT))
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(12, 6, HEADER_WIDTH - 24, 18), Qt.AlignLeft | Qt.AlignVCenter, self.name)

    def set_selected(self, selected: bool) -> None:
        self.selected = selected
        self.update()

    def mousePressEvent(self, event) -> None:
        # The M/L/H/D buttons are child items and take their own clicks;
        # anywhere else on the header selects the track.
        if event.button() == Qt.LeftButton:
            self.signals.track_selected.emit(self.row)
        event.accept()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu()
        rename_action = menu.addAction("Rename Track...")
        up_action = menu.addAction("Move Track Up")
        down_action = menu.addAction("Move Track Down")
        height_menu = menu.addMenu("Track Height")
        height_actions = {}
        for label, pixels in TRACK_HEIGHT_PRESETS.items():
            action = height_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(pixels == self.row_height)
            height_actions[action] = pixels
        menu.addSeparator()
        duck_action = menu.addAction("Duck Audio Under / Ducking Settings...")
        chosen = menu.exec(event.screenPos())
        if chosen in height_actions:
            self.signals.track_height_requested.emit(self.row, height_actions[chosen])
        if chosen == rename_action:
            self.signals.track_rename_requested.emit(self.row)
        elif chosen == up_action:
            self.signals.track_move_requested.emit(self.row, -1)
        elif chosen == down_action:
            self.signals.track_move_requested.emit(self.row, 1)
        elif chosen == duck_action:
            self.signals.track_duck_config_requested.emit(self.row)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.signals.track_rename_requested.emit(self.row)
        event.accept()


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
        color_menu = menu.addMenu("Colour")
        color_actions = {}
        for color_name, value in MARKER_COLORS.items():
            swatch = QPixmap(12, 12)
            swatch.fill(QColor(value))
            action = color_menu.addAction(QIcon(swatch), color_name)
            action.setCheckable(True)
            action.setChecked(value.lower() == str(self.color).lower())
            color_actions[action] = value
        delete_action = menu.addAction("Delete Marker")
        chosen = menu.exec(event.screenPos())
        if chosen in color_actions:
            self.signals.marker_color_requested.emit(self.marker_id, color_actions[chosen])
            return
        if chosen == rename_action:
            self.signals.marker_rename_requested.emit(self.marker_id, self.name)
        elif chosen == delete_action:
            self.signals.marker_delete_requested.emit(self.marker_id)
        event.accept()


class RulerItem(QGraphicsObject):
    """Time ruler drawn across the top of the timeline scene."""

    def __init__(self, pixels_per_second: float, total_seconds: float, fps: float = 30.0):
        super().__init__()
        self.pixels_per_second = pixels_per_second
        self.total_seconds = max(total_seconds, 30.0)
        self.fps = fps if fps > 0 else 30.0
        # In/Out points (seconds or None), shaded on the ruler.
        self.in_point: Optional[float] = None
        self.out_point: Optional[float] = None
        self.setZValue(40)
        # Makes option.exposedRect the actually-exposed area, so paint()
        # can skip the ticks outside it.
        self.setFlag(QGraphicsItem.ItemUsesExtendedStyleOption, True)

    def set_in_out(self, in_point: Optional[float], out_point: Optional[float]) -> None:
        self.in_point, self.out_point = in_point, out_point
        self.update()

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

        if self.in_point is not None or self.out_point is not None:
            start = (self.in_point or 0.0) * self.pixels_per_second
            end = (self.out_point if self.out_point is not None else self.total_seconds) * self.pixels_per_second
            if end > start:
                shade = QColor(theme.COLOR_ACCENT)
                shade.setAlpha(70)
                painter.fillRect(QRectF(start, 0, end - start, RULER_HEIGHT - 1), shade)

        interval = self._pick_interval()
        painter.setPen(QColor(theme.COLOR_TEXT_DIM))
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)

        # Only the ticks in the exposed area (not the whole timeline width).
        exposed = option.exposedRect if option is not None else self.boundingRect()
        first = max(0, int(exposed.left() / (interval * self.pixels_per_second)) - 1)
        last = int(exposed.right() / (interval * self.pixels_per_second)) + 1
        for k in range(first, last + 1):
            t = k * interval
            if t > self.total_seconds + interval:
                break
            x = t * self.pixels_per_second
            painter.drawLine(int(x), RULER_HEIGHT - 8, int(x), RULER_HEIGHT - 1)
            painter.drawText(int(x) + 3, RULER_HEIGHT - 10, self._label(t, interval))

    def _label(self, t: float, interval: float) -> str:
        if interval < 1.0:
            # Sub-second ticks: MM:SS:FF, so frame positions are readable at high zoom.
            return format_timecode(t, self.fps)[3:]
        return f"{int(t // 60):02d}:{int(t % 60):02d}"

    def _pick_interval(self) -> float:
        # Choose a tick interval so ticks land roughly every 60-100px;
        # below a second, whole numbers of frames.
        frame = 1.0 / self.fps
        for candidate in (5 * frame, 10 * frame, 15 * frame, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600):
            if candidate * self.pixels_per_second >= 70:
                return candidate
        return 600
