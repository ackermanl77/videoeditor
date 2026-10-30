"""Source monitor (UI6): preview a Media Bin file before it goes on the timeline.

Double-click a video or audio file in the Media Bin: it opens here (its own window, beside the
program preview), with a position slider. Mark In (I) and Mark Out (O) set the part to use; Insert
(,), Overwrite (.) and Append place just that part at the playhead or the end of the timeline. The
range is remembered per file (MainWindow.source_ranges), so Edit > Insert / Overwrite with the file
selected in the bin use it too.
"""

from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget

#: The shortest part of a file that can be marked (seconds).
MIN_RANGE_SECONDS = 0.1


def marked_range(duration: float, mark_in: Optional[float], mark_out: Optional[float]) -> Optional[Tuple[float, float]]:
    """The (in, out) seconds to use: the marks, inside the file, the whole file for a missing one;
    None when nothing is marked or the marks leave less than MIN_RANGE_SECONDS."""
    if mark_in is None and mark_out is None:
        return None
    start = min(max(0.0, mark_in or 0.0), duration)
    end = min(duration, mark_out) if mark_out is not None else duration
    return (start, end) if end - start >= MIN_RANGE_SECONDS else None


def format_seconds(seconds: float) -> str:
    minutes, rest = divmod(max(0.0, seconds), 60.0)
    return f"{int(minutes):02d}:{rest:05.2f}"


class SourceMonitor(QDialog):
    #: asset id, "insert" / "overwrite" / "append", (in, out) or None for the whole file
    place_requested = Signal(str, str, object)
    #: asset id, (in, out) or None -- the window remembers it per file
    range_changed = Signal(str, object)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Source Monitor")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.resize(640, 460)
        self.asset_id: Optional[str] = None
        self.duration = 0.0
        self.mark_in: Optional[float] = None
        self.mark_out: Optional[float] = None
        self._seeking = False

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget(self)
        self.video.setMinimumHeight(240)
        self.video.setStyleSheet("background: black;")
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self._on_position)

        layout = QVBoxLayout(self)
        layout.addWidget(self.video, 1)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setAccessibleName("Source position")
        self.slider.sliderMoved.connect(lambda ms: self.seek(ms / 1000.0))
        layout.addWidget(self.slider)
        self.time_label = QLabel()
        self.range_label = QLabel()
        info = QHBoxLayout()
        info.addWidget(self.time_label)
        info.addStretch(1)
        info.addWidget(self.range_label)
        layout.addLayout(info)

        marks = QHBoxLayout()
        self.play_button = self._button(marks, "Play", self.toggle_play, "Play / pause (Space)")
        self._button(marks, "Mark In", self.set_in, "Mark In at this frame (I)")
        self._button(marks, "Mark Out", self.set_out, "Mark Out at this frame (O)")
        self._button(marks, "Clear", self.clear_marks, "Use the whole file")
        marks.addStretch(1)
        self._button(marks, "Insert", lambda: self._place("insert"), "Insert the marked part at the timeline's playhead (,)")
        self._button(marks, "Overwrite", lambda: self._place("overwrite"), "Overwrite at the timeline's playhead (.)")
        self._button(marks, "Append", lambda: self._place("append"), "Add the marked part at the end of the timeline")
        layout.addLayout(marks)

        for key, slot in (("Space", self.toggle_play), ("I", self.set_in), ("O", self.set_out),
                          (",", lambda: self._place("insert")), (".", lambda: self._place("overwrite")),
                          ("Left", lambda: self.step(-1 / 25)), ("Right", lambda: self.step(1 / 25))):
            QShortcut(QKeySequence(key), self, slot)
        self._update_labels()

    @staticmethod
    def _button(row, text, slot, tip) -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(tip)
        button.clicked.connect(slot)
        row.addWidget(button)
        return button

    # -- loading and playback --------------------------------------------------
    def open_asset(self, asset_id: str, path: str, duration: float, name: str,
                   marks: Optional[Tuple[float, float]] = None) -> None:
        self.player.stop()
        self.asset_id, self.duration = asset_id, max(0.0, float(duration))
        self.mark_in, self.mark_out = marks if marks else (None, None)
        self.setWindowTitle(f"Source Monitor - {name}")
        self.slider.setRange(0, int(self.duration * 1000))
        self.player.setSource(QUrl.fromLocalFile(path))
        self.seek(self.mark_in or 0.0)
        self._update_labels()

    def position(self) -> float:
        return self.slider.value() / 1000.0

    def seek(self, seconds: float) -> None:
        seconds = min(max(0.0, seconds), self.duration)
        self._seeking = True
        self.slider.setValue(int(round(seconds * 1000)))
        self._seeking = False
        self.player.setPosition(int(round(seconds * 1000)))
        self._update_labels()

    def step(self, seconds: float) -> None:
        self.seek(self.position() + seconds)

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_button.setText("Play")
        else:
            self.player.play()
            self.play_button.setText("Pause")

    def _on_position(self, ms: int) -> None:
        if not self._seeking and not self.slider.isSliderDown():
            self.slider.setValue(ms)
            self._update_labels()

    # -- marks -------------------------------------------------------------------
    def set_in(self, seconds: Optional[float] = None) -> None:
        self.mark_in = self.position() if seconds is None else seconds
        if self.mark_out is not None and self.mark_out <= self.mark_in:
            self.mark_out = None  # an In after the Out starts a new range
        self._marks_changed()

    def set_out(self, seconds: Optional[float] = None) -> None:
        self.mark_out = self.position() if seconds is None else seconds
        if self.mark_in is not None and self.mark_in >= self.mark_out:
            self.mark_in = None
        self._marks_changed()

    def clear_marks(self) -> None:
        self.mark_in = self.mark_out = None
        self._marks_changed()

    def marked(self) -> Optional[Tuple[float, float]]:
        return marked_range(self.duration, self.mark_in, self.mark_out)

    def _marks_changed(self) -> None:
        self._update_labels()
        if self.asset_id is not None:
            self.range_changed.emit(self.asset_id, self.marked())

    def _update_labels(self) -> None:
        self.time_label.setText(f"{format_seconds(self.position())} / {format_seconds(self.duration)}")
        marked = self.marked()
        self.range_label.setText(
            f"In {format_seconds(marked[0])}  Out {format_seconds(marked[1])}  ({marked[1] - marked[0]:.2f} s)"
            if marked else "Whole file (I / O mark a part)"
        )

    def _place(self, mode: str) -> None:
        if self.asset_id is not None:
            self.place_requested.emit(self.asset_id, mode, self.marked())

    def closeEvent(self, event) -> None:
        self.player.stop()
        super().closeEvent(event)
