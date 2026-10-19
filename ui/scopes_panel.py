"""The Scopes dock (F24): waveform, RGB parade or vectorscope of the frame under the playhead.

Renders only while the dock is visible and playback is paused, a moment after the playhead or
the project last changed (``REFRESH_DELAY_MS``), on a worker thread; a result a newer request
has superseded is dropped -- the same pattern as the preview's exact-frame render.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from core.scopes import SCOPES, ScopeError, timeline_scope

REFRESH_DELAY_MS = 300


class ScopesPanel(QWidget):
    _scope_ready = Signal(int, object)  # (generation, PNG bytes or an error message)

    def __init__(self, project_getter: Callable, time_getter: Callable[[], float],
                 playing_getter: Callable[[], bool] = lambda: False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._project_getter, self._time_getter, self._playing_getter = project_getter, time_getter, playing_getter
        self._generation = 0
        self._pixmap = QPixmap()
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.kind_combo = QComboBox()
        for kind, label in SCOPES.items():
            self.kind_combo.addItem(label, kind)
        self.kind_combo.setAccessibleName("Scope")
        self.kind_combo.currentIndexChanged.connect(lambda _index: self.schedule_refresh())
        row.addWidget(self.kind_combo, 1)
        layout.addLayout(row)
        self.image_label = QLabel("Pause playback to measure the frame under the playhead.")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(240, 140)
        self.image_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.image_label.setStyleSheet("background: #000; color: #9aa1ad;")
        layout.addWidget(self.image_label, 1)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #9aa1ad;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(REFRESH_DELAY_MS)
        self._timer.timeout.connect(self._start_render)
        self._scope_ready.connect(self._on_scope_ready)

    @property
    def kind(self) -> str:
        return self.kind_combo.currentData()

    def schedule_refresh(self) -> None:
        """Re-measure soon (debounced); a no-op while hidden -- showing the dock refreshes it."""
        self._generation += 1
        if self.isVisible():
            self._timer.start()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self.schedule_refresh()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._show_pixmap()

    def _start_render(self) -> None:
        project = self._project_getter()
        if project is None or project.total_duration <= 0:
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText("Add clips to the timeline to see its scopes.")
            return
        if self._playing_getter():
            self.status_label.setText("Paused playback updates the scope.")
            return
        generation, t, kind = self._generation, self._time_getter(), self.kind
        self.status_label.setText("Measuring…")

        def work() -> None:
            try:
                result = timeline_scope(project, t, kind)
            except ScopeError as exc:
                result = str(exc)
            except Exception as exc:  # noqa: BLE001 - never let the worker thread die silently
                result = f"Scope failed: {exc}"
            self._scope_ready.emit(generation, result)

        threading.Thread(target=work, name="scope-render", daemon=True).start()

    def _on_scope_ready(self, generation: int, result) -> None:
        if generation != self._generation:
            return  # superseded by a newer playhead position, edit or scope choice
        if isinstance(result, (bytes, bytearray)):
            self._pixmap = QPixmap()
            self._pixmap.loadFromData(bytes(result), "PNG")
            self.status_label.setText(f"{SCOPES[self.kind]} of the frame at the playhead")
            self._show_pixmap()
        else:
            self._pixmap = QPixmap()
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText(f"Couldn't measure this frame:\n{result}")
            self.status_label.setText("")

    def _show_pixmap(self) -> None:
        if not self._pixmap.isNull():
            self.image_label.setPixmap(self._pixmap.scaled(self.image_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    @property
    def has_image(self) -> bool:
        return not self._pixmap.isNull()
