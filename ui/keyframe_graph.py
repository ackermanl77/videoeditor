"""The keyframe graph (F39): a property's keyframes as a curve over the clip's time.

Drag a keyframe point to change its time and value; drag the two round handles on the
first gap to shape the easing of every gap of that property (a custom "curve:m0,m1"
easing, core.clip.curve_easing -- left handle = how it leaves a keyframe, right handle =
how it arrives at the next). Each release is one undo step through the Properties panel.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from core.clip import curve_easing, curve_slopes, interpolate_keyframes

#: (key, label, clip attribute, value index in a keyframe, easing target, value range)
PROPERTIES = (
    ("opacity", "Opacity", "opacity_keyframes", 1, "opacity", (0.0, 1.0)),
    ("volume", "Volume", "volume_keyframes", 1, "volume", (0.0, 2.0)),
    ("position_x", "Position X", "position_keyframes", 1, "position", (-0.5, 1.5)),
    ("position_y", "Position Y", "position_keyframes", 2, "position", (-0.5, 1.5)),
)
HANDLE_LENGTH = 0.25  # of the first gap's width
POINT_RADIUS = 6


class GraphCanvas(QWidget):
    """Paints one property's curve; emits ``keyframes_edited``/``easing_edited`` on release."""

    keyframes_edited = Signal(list)
    easing_edited = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(560, 260)
        self.setMouseTracking(True)
        self.keyframes: List[Tuple[float, ...]] = []
        self.index = 1
        self.duration = 1.0
        self.value_range = (0.0, 1.0)
        self.easing = "linear"
        self._drag: Optional[Tuple[str, int]] = None

    def set_data(self, keyframes, index: int, duration: float, value_range, easing: str) -> None:
        self.keyframes = sorted(tuple(k) for k in keyframes)
        self.index, self.duration = index, max(duration, 1e-3)
        self.value_range, self.easing = value_range, easing
        self.update()

    # -- geometry -----------------------------------------------------------
    def _plot(self) -> QRectF:
        return QRectF(40, 12, max(10, self.width() - 56), max(10, self.height() - 36))

    def _to_screen(self, t: float, v: float) -> QPointF:
        r, (low, high) = self._plot(), self.value_range
        return QPointF(r.left() + r.width() * t / self.duration, r.bottom() - r.height() * (v - low) / max(high - low, 1e-9))

    def _from_screen(self, point: QPointF) -> Tuple[float, float]:
        r, (low, high) = self._plot(), self.value_range
        t = (point.x() - r.left()) / r.width() * self.duration
        v = low + (r.bottom() - point.y()) / r.height() * (high - low)
        return min(max(t, 0.0), self.duration), min(max(v, low), high)

    def _slopes(self) -> Tuple[float, float]:
        slopes = curve_slopes(self.easing)
        if slopes is not None:
            return slopes
        return {"ease_in": (0.0, 2.0), "ease_out": (2.0, 0.0), "ease_in_out": (0.0, 0.0)}.get(self.easing, (1.0, 1.0))

    def _handles(self) -> List[QPointF]:
        """The two tangent handles, drawn on the first gap (they shape every gap)."""
        if len(self.keyframes) < 2:
            return []
        (t0, *a), (t1, *b) = self.keyframes[0], self.keyframes[1]
        v0, v1 = a[self.index - 1], b[self.index - 1]
        m0, m1 = self._slopes()
        dt, dv = (t1 - t0) * HANDLE_LENGTH, (v1 - v0) * HANDLE_LENGTH
        return [self._to_screen(t0 + dt, v0 + dv * m0), self._to_screen(t1 - dt, v1 - dv * m1)]

    # -- painting -----------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#1b1e23"))
        r = self._plot()
        painter.setPen(QPen(QColor("#3a404a"), 1))
        painter.drawRect(r)
        low, high = self.value_range
        painter.setPen(QColor("#9aa1ad"))
        painter.drawText(QPointF(4, r.top() + 10), f"{high:g}")
        painter.drawText(QPointF(4, r.bottom()), f"{low:g}")
        painter.drawText(QPointF(r.right() - 40, self.height() - 6), f"{self.duration:.1f} s")
        if not self.keyframes:
            painter.drawText(r, Qt.AlignmentFlag.AlignCenter, "No keyframes on this property yet.")
            return
        path = QPainterPath()
        steps = 240
        for i in range(steps + 1):
            t = self.duration * i / steps
            value = interpolate_keyframes(self.keyframes, t, self.easing)[self.index - 1]
            point = self._to_screen(t, value)
            path.moveTo(point) if i == 0 else path.lineTo(point)
        painter.setPen(QPen(QColor("#4c9aff"), 2))
        painter.drawPath(path)
        handles = self._handles()
        if handles:
            anchors = [self._to_screen(self.keyframes[0][0], self.keyframes[0][self.index]),
                       self._to_screen(self.keyframes[1][0], self.keyframes[1][self.index])]
            painter.setPen(QPen(QColor("#e0b84c"), 1))
            for anchor, handle in zip(anchors, handles):
                painter.drawLine(anchor, handle)
                painter.setBrush(QColor("#e0b84c"))
                painter.drawEllipse(handle, 4, 4)
        painter.setBrush(QColor("#ffffff"))
        painter.setPen(QPen(QColor("#1b1e23"), 1))
        for key in self.keyframes:
            point = self._to_screen(key[0], key[self.index])
            painter.drawRect(QRectF(point.x() - 4, point.y() - 4, 8, 8))

    # -- dragging -----------------------------------------------------------
    def _hit(self, pos: QPointF) -> Optional[Tuple[str, int]]:
        for i, handle in enumerate(self._handles()):
            if (handle - pos).manhattanLength() <= POINT_RADIUS * 2:
                return ("handle", i)
        for i, key in enumerate(self.keyframes):
            if (self._to_screen(key[0], key[self.index]) - pos).manhattanLength() <= POINT_RADIUS * 2:
                return ("key", i)
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._drag = self._hit(event.position())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is None:
            hit = self._hit(event.position())
            self.setCursor(Qt.CursorShape.SizeAllCursor if hit else Qt.CursorShape.ArrowCursor)
            return
        kind, i = self._drag
        t, v = self._from_screen(event.position())
        if kind == "key":
            key = list(self.keyframes[i])
            neighbours = [k[0] for j, k in enumerate(self.keyframes) if j != i]
            before = max([n for n in neighbours if n < key[0]], default=None)
            after = min([n for n in neighbours if n > key[0]], default=None)
            if before is not None:
                t = max(t, before + 1e-3)
            if after is not None:
                t = min(t, after - 1e-3)
            key[0], key[self.index] = round(t, 4), round(v, 4)
            self.keyframes[i] = tuple(key)
        else:
            (t0, *a), (t1, *b) = self.keyframes[0], self.keyframes[1]
            v0, v1 = a[self.index - 1], b[self.index - 1]
            dv = (v1 - v0) * HANDLE_LENGTH
            m0, m1 = self._slopes()
            if abs(dv) > 1e-9:
                if i == 0:
                    m0 = (v - v0) / dv
                else:
                    m1 = (v1 - v) / dv
            self.easing = curve_easing(m0, m1)
        self.update()

    def mouseReleaseEvent(self, _event) -> None:  # noqa: N802
        if self._drag is None:
            return
        kind, _i = self._drag
        self._drag = None
        if kind == "key":
            self.keyframes.sort()
            self.keyframes_edited.emit(list(self.keyframes))
        else:
            self.easing_edited.emit(self.easing)


class KeyframeGraphDialog(QDialog):
    """``commit_keyframes(attribute, keyframes)`` and ``commit_easing(target, easing)`` apply edits."""

    def __init__(self, clip_getter: Callable, commit_keyframes: Callable, commit_easing: Callable, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyframe Graph")
        self._clip = clip_getter
        self._commit_keyframes, self._commit_easing = commit_keyframes, commit_easing
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Property"))
        self.property_combo = QComboBox()
        for key, label, *_rest in PROPERTIES:
            self.property_combo.addItem(label, key)
        self.property_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        row.addWidget(self.property_combo, 1)
        self.reset_btn = QPushButton("Straight (Linear)")
        self.reset_btn.setToolTip("Back to linear easing for this property")
        self.reset_btn.clicked.connect(lambda: self._on_easing("linear"))
        row.addWidget(self.reset_btn)
        layout.addLayout(row)
        self.canvas = GraphCanvas(self)
        self.canvas.keyframes_edited.connect(self._on_keyframes)
        self.canvas.easing_edited.connect(self._on_easing)
        layout.addWidget(self.canvas, 1)
        hint = QLabel("Drag a white keyframe to change its time and value; drag the yellow handles to shape how "
                      "the value moves between keyframes (the same shape for every gap of this property).")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh()

    def _property(self):
        key = self.property_combo.currentData()
        return next(p for p in PROPERTIES if p[0] == key)

    def refresh(self) -> None:
        clip = self._clip()
        if clip is None:
            self.canvas.set_data([], 1, 1.0, (0.0, 1.0), "linear")
            return
        _key, _label, attribute, index, target, value_range = self._property()
        self.canvas.set_data(getattr(clip, attribute), index, clip.duration, value_range, clip.easing_for(target))

    def _on_keyframes(self, keyframes) -> None:
        _key, _label, attribute, *_rest = self._property()
        self._commit_keyframes(attribute, [tuple(k) for k in keyframes])
        self.refresh()

    def _on_easing(self, easing: str) -> None:
        self._commit_easing(self._property()[4], easing)
        self.refresh()
