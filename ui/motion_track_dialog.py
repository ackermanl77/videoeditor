"""Track Motion dialog (F18): draw a box around the object on a frame of the tracked clip."""
from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget


class BoxPicker(QWidget):
    """Shows a frame and lets the user drag a rectangle on it; ``box()`` gives it as fractions."""

    def __init__(self, image: QImage, parent=None):
        super().__init__(parent)
        self._pixmap = QPixmap.fromImage(image)
        self.setFixedSize(self._pixmap.size())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setAccessibleName("Frame to draw the tracking box on")
        self._start: Optional[QPoint] = None
        self._rect = QRect()

    def box(self) -> Optional[Tuple[float, float, float, float]]:
        rect = self._rect.normalized()
        if rect.width() < 4 or rect.height() < 4:
            return None
        w, h = self.width(), self.height()
        return rect.x() / w, rect.y() / h, rect.width() / w, rect.height() / h

    def set_box(self, box: Tuple[float, float, float, float]) -> None:
        w, h = self.width(), self.height()
        self._rect = QRect(int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h))
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = event.position().toPoint()
            self._rect = QRect(self._start, self._start)
            self.update()

    def mouseMoveEvent(self, event):
        if self._start is not None:
            point = event.position().toPoint()
            point.setX(min(max(0, point.x()), self.width() - 1))
            point.setY(min(max(0, point.y()), self.height() - 1))
            self._rect = QRect(self._start, point)
            self.update()

    def mouseReleaseEvent(self, event):
        self._start = None
        self.window().findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self.box() is not None)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._pixmap)
        if not self._rect.isNull():
            painter.setPen(QPen(QColor("#FACC15"), 2))
            painter.drawRect(self._rect.normalized())


class MotionTrackDialog(QDialog):
    def __init__(self, image: QImage, tracked_name: str, follower_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Track Motion")
        layout = QVBoxLayout(self)
        intro = QLabel(f"Draw a box around the object to follow in “{tracked_name}”. "
                       f"“{follower_name}” will move with it.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.picker = BoxPicker(image, self)
        layout.addWidget(self.picker, 0, Qt.AlignmentFlag.AlignHCenter)
        self.keep_offset = QCheckBox("Keep the clip's current offset from the object")
        self.keep_offset.setChecked(True)
        self.keep_offset.setToolTip("Off: the clip's centre sits on the object's centre.")
        layout.addWidget(self.keep_offset)
        hint = QLabel("Works best on an object with clear detail that doesn't turn or change size much; "
                      "frames where it can't be found are skipped.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Track")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
