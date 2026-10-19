"""The Audio Mixer dock (F31): one strip per track -- fader (dB), pan, Mute, Solo and a live meter.

Edits go through ``UpdateTrackPropertyCommand`` (undoable); a fader drag is one undo step when the
handle is released. The meters read the cached waveforms of the clips under the playhead with
each clip's own gain and the track's fader/pan (``core.mixer.track_levels``), refreshed while the
dock is visible.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget

from core.commands import UpdateTrackPropertyCommand
from core.mixer import GAIN_RANGE, track_levels
from core.track import TrackType
from core.waveform import load_waveform, window_levels
from ui import theme

METER_INTERVAL_MS = 100


class _VerticalMeter(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.peak = self.rms = 0.0
        self.setFixedWidth(14)
        self.setMinimumHeight(120)

    def set_levels(self, peak: float, rms: float) -> None:
        if (peak, rms) != (self.peak, self.rms):
            self.peak, self.rms = peak, rms
            self.update()

    def paintEvent(self, event) -> None:  # noqa: ARG002, N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.COLOR_BG_DARK))
        h = self.height() - 2
        painter.fillRect(1, 1 + int(h * (1 - self.peak)), 5, int(h * self.peak), QColor("#ef4444" if self.peak > 0.89 else "#f59e0b"))
        painter.fillRect(7, 1 + int(h * (1 - self.rms)), 5, int(h * self.rms), QColor("#22c55e"))
        painter.setPen(QColor(theme.COLOR_BORDER))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))


class TrackStrip(QWidget):
    def __init__(self, panel: "MixerPanel", track):
        super().__init__()
        self.panel, self.track_id = panel, track.id
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.name_label = QLabel(track.name)
        self.name_label.setAlignment(Qt.AlignCenter)
        self.name_label.setToolTip(f"{track.name} ({'video track audio' if track.track_type == TrackType.VIDEO else 'audio track'})")
        layout.addWidget(self.name_label)
        row = QHBoxLayout()
        self.fader = QSlider(Qt.Vertical)
        self.fader.setRange(int(GAIN_RANGE[0] * 2), int(GAIN_RANGE[1] * 2))  # half-dB steps
        self.fader.setPageStep(12)
        self.fader.setAccessibleName(f"{track.name} fader")
        self.fader.setToolTip("Track volume in dB (double-click the value to reset)")
        row.addWidget(self.fader)
        self.meter = _VerticalMeter()
        row.addWidget(self.meter)
        layout.addLayout(row, 1)
        self.gain_label = QLabel("")
        self.gain_label.setAlignment(Qt.AlignCenter)
        self.gain_label.mouseDoubleClickEvent = lambda _e: self.panel.set_track(self.track_id, "gain_db", 0.0, "Reset Track Volume")
        layout.addWidget(self.gain_label)
        self.pan = QSlider(Qt.Horizontal)
        self.pan.setRange(-100, 100)
        self.pan.setAccessibleName(f"{track.name} pan")
        self.pan.setToolTip("Pan: left / right balance")
        layout.addWidget(self.pan)
        self.pan_label = QLabel("")
        self.pan_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.pan_label)
        buttons = QHBoxLayout()
        self.mute_btn = QPushButton("M")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setToolTip("Mute this track")
        self.solo_btn = QPushButton("S")
        self.solo_btn.setCheckable(True)
        self.solo_btn.setToolTip("Solo: hear only the soloed tracks")
        buttons.addWidget(self.mute_btn)
        buttons.addWidget(self.solo_btn)
        layout.addLayout(buttons)
        self.load(track)
        self.fader.valueChanged.connect(self._on_fader_moved)
        self.fader.sliderReleased.connect(self._commit_fader)
        self.pan.valueChanged.connect(self._on_pan_moved)
        self.pan.sliderReleased.connect(self._commit_pan)
        self.mute_btn.toggled.connect(lambda on: self.panel.set_track(self.track_id, "muted", on, "Toggle Track Mute"))
        self.solo_btn.toggled.connect(lambda on: self.panel.set_track(self.track_id, "solo", on, "Toggle Track Solo"))

    def load(self, track) -> None:
        for widget, value in ((self.fader, int(round(track.gain_db * 2))), (self.pan, int(round(track.pan * 100)))):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        for button, value in ((self.mute_btn, track.muted), (self.solo_btn, track.solo)):
            button.blockSignals(True)
            button.setChecked(value)
            button.blockSignals(False)
        self.name_label.setText(track.name)
        self._update_labels()

    def _update_labels(self) -> None:
        gain = self.fader.value() / 2.0
        self.gain_label.setText(f"{gain:+.1f} dB" if gain else "0.0 dB")
        pan = self.pan.value()
        self.pan_label.setText("C" if pan == 0 else (f"L{-pan}" if pan < 0 else f"R{pan}"))

    def _on_fader_moved(self, _value) -> None:
        self._update_labels()
        if not self.fader.isSliderDown():  # keyboard / wheel / click: commit now
            self._commit_fader()

    def _on_pan_moved(self, _value) -> None:
        self._update_labels()
        if not self.pan.isSliderDown():
            self._commit_pan()

    def _commit_fader(self) -> None:
        self.panel.set_track(self.track_id, "gain_db", self.fader.value() / 2.0, "Edit Track Volume")

    def _commit_pan(self) -> None:
        self.panel.set_track(self.track_id, "pan", self.pan.value() / 100.0, "Edit Track Pan")


class MixerPanel(QWidget):
    project_modified = Signal()

    def __init__(self, project_getter: Callable, history_getter: Callable, time_getter: Callable[[], float],
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._project_getter, self._history_getter, self._time_getter = project_getter, history_getter, time_getter
        self._waveforms: Dict[str, object] = {}
        self.strips: Dict[str, TrackStrip] = {}
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._holder = QWidget()
        self._row = QHBoxLayout(self._holder)
        self._row.setAlignment(Qt.AlignLeft)
        scroll.setWidget(self._holder)
        outer.addWidget(scroll)
        self._signature = None
        self._timer = QTimer(self)
        self._timer.setInterval(METER_INTERVAL_MS)
        self._timer.timeout.connect(self.update_meters)

    # -- model ---------------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild the strips when tracks were added/removed/reordered, else reload their values."""
        project = self._project_getter()
        if project is None:
            return
        signature = tuple(t.id for t in project.tracks)
        if signature != self._signature:
            for strip in self.strips.values():
                strip.deleteLater()
            self.strips = {}
            for track in project.tracks:
                strip = TrackStrip(self, track)
                self.strips[track.id] = strip
                self._row.addWidget(strip)
            self._signature = signature
        else:
            for track in project.tracks:
                self.strips[track.id].load(track)

    def set_track(self, track_id: str, attribute: str, value, label: str) -> bool:
        project, history = self._project_getter(), self._history_getter()
        track = project.find_track(track_id) if project is not None else None
        if track is None or history is None or getattr(track, attribute) == value:
            return False
        history.execute(UpdateTrackPropertyCommand(project, track_id, attribute, value, label))
        self.strips[track_id].load(track)
        self.project_modified.emit()
        return True

    # -- meters --------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()

    def _clip_level(self, clip, offset: float):
        project = self._project_getter()
        asset = project.asset_for_clip(clip) if project is not None else None
        if asset is None or not asset.waveform_path or clip.muted or clip.is_silent or clip.in_freeze_frame(offset):
            return None
        if asset.waveform_path not in self._waveforms:
            self._waveforms[asset.waveform_path] = load_waveform(asset.waveform_path)
        data = self._waveforms[asset.waveform_path]
        if data is None:
            return None
        from ui.preview_player import PreviewPlayer

        gain = PreviewPlayer._clip_gain_at(clip, offset)
        peak, rms = window_levels(data, clip.source_time_at(offset))
        return peak * gain, rms * gain

    def update_meters(self) -> None:
        project = self._project_getter()
        if project is None:
            return
        for track_id, (peak, rms) in track_levels(project, self._time_getter(), self._clip_level).items():
            strip = self.strips.get(track_id)
            if strip is not None:
                strip.meter.set_levels(peak, rms)
