"""Auto Color (F36) and recording a voiceover, the screen or a webcam (F34). A mixin of MainWindow."""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QStandardPaths, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
)

from core.clip import MediaType
from core.ffmpeg_utils import find_binary


def recordings_folder() -> str:
    movies = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
    return os.path.join(movies or os.path.expanduser("~"), "Open Video Editor", "Recordings")


class RecordDialog(QDialog):
    """Choose what to record and with which devices; Start/Stop in the same dialog."""

    def __init__(self, devices, parent=None, kind: str = "voiceover"):
        super().__init__(parent)
        self.setWindowTitle("Record")
        self.recorder = None
        self.result_path: Optional[str] = None
        form = QFormLayout(self)
        self.kind_combo = QComboBox()
        for key, label in (("voiceover", "Voiceover (microphone)"), ("screen", "Screen"), ("webcam", "Webcam")):
            self.kind_combo.addItem(label, key)
        self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData(kind)))
        form.addRow("Record", self.kind_combo)
        self.mic_combo = QComboBox()
        self.mic_combo.addItem("(none)", "")
        for device in devices:
            if device.kind == "audio":
                self.mic_combo.addItem(device.name, device.spec)
        if self.mic_combo.count() > 1:
            self.mic_combo.setCurrentIndex(1)
        form.addRow("Microphone", self.mic_combo)
        self.camera_combo = QComboBox()
        for device in devices:
            if device.kind == "video":
                self.camera_combo.addItem(device.name, device.spec)
        if not self.camera_combo.count():
            self.camera_combo.addItem("(no camera found)", "")
        form.addRow("Camera", self.camera_combo)
        self.status = QLabel("The recording goes to the media bin; a voiceover is also placed on an audio track "
                             "at the playhead.")
        self.status.setWordWrap(True)
        form.addRow(self.status)
        self.buttons = QDialogButtonBox()
        self.start_btn = self.buttons.addButton("Start Recording", QDialogButtonBox.ButtonRole.ActionRole)
        self.stop_btn = self.buttons.addButton("Stop", QDialogButtonBox.ButtonRole.ActionRole)
        self.stop_btn.setEnabled(False)
        self.close_btn = self.buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        self.close_btn.clicked.connect(self.reject)
        form.addRow(self.buttons)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._tick)

    def kind(self) -> str:
        return self.kind_combo.currentData()

    def start(self) -> bool:
        from core.recording import Recorder, output_path, recording_args

        folder = recordings_folder()
        os.makedirs(folder, exist_ok=True)
        output = output_path(folder, self.kind())
        try:
            args = recording_args(find_binary("ffmpeg"), self.kind(), output, microphone=self.mic_combo.currentData(),
                                  camera=self.camera_combo.currentData())
        except ValueError as exc:
            QMessageBox.information(self, "Record", str(exc))
            return False
        self.recorder = Recorder(args, output)
        for widget in (self.kind_combo, self.mic_combo, self.camera_combo, self.start_btn, self.close_btn):
            widget.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.timer.start()
        return True

    def _tick(self) -> None:
        if self.recorder is None:
            return
        if not self.recorder.running:  # FFmpeg stopped by itself: a device error
            self.stop()
            return
        seconds = int(self.recorder.elapsed())
        self.status.setText(f"● Recording {seconds // 60}:{seconds % 60:02d} — press Stop to finish.")

    def stop(self) -> Optional[str]:
        if self.recorder is None:
            return None
        self.timer.stop()
        path = self.recorder.stop()
        error = self.recorder.error_text() if path is None else ""
        self.recorder = None
        self.result_path = path
        if path is None:
            self.status.setText("Nothing was recorded. " + error)
            for widget in (self.kind_combo, self.mic_combo, self.camera_combo, self.start_btn, self.close_btn):
                widget.setEnabled(True)
            self.stop_btn.setEnabled(False)
            return None
        self.accept()
        return path

    def reject(self) -> None:
        if self.recorder is not None:
            self.stop()
            return
        super().reject()


class ToolsMixin:
    # -- Auto Color (F36) ---------------------------------------------------------
    def auto_color(self, levels=None) -> bool:
        """Edit > Auto Color: measure the frame under the playhead and add (or update) the clip's
        Levels effect -- black/white points per channel, which also removes a colour cast."""
        from core.auto_color import measure_levels
        from core.effects import EffectInstance

        title = "Auto Color"
        clip = self.project.find_clip(self.timeline.selected_clip_id) if self.timeline.selected_clip_id else None
        if clip is None or clip.media_type not in (MediaType.VIDEO, MediaType.IMAGE):
            QMessageBox.information(self, title, "Select a video or image clip first.")
            return False
        if levels is None:
            local = min(max(0.0, self.timeline.playhead_time - clip.timeline_start), clip.playable_duration)
            levels = measure_levels(find_binary("ffmpeg"), clip.source_path, clip.source_time_at(local))
        if not levels:
            QMessageBox.warning(self, title, "The frame under the playhead couldn't be read.")
            return False
        self.properties.set_selected_clip(clip.id)
        effects = self.properties._effects_copy()
        existing = next((i for i, e in enumerate(effects) if e.kind == "color_levels"), None)
        if existing is None:
            effects.append(EffectInstance.create("color_levels", **levels))
            row = len(effects) - 1
        else:
            effects[existing].params.update(levels)
            effects[existing].enabled = True
            row = existing
        self.properties._commit_effects(effects, title, row)
        self.statusBar().showMessage("Auto Color: levels set from the frame under the playhead.", 5000)
        return True

    # -- recording (F34) ------------------------------------------------------------
    def record_media(self, kind: str = "voiceover") -> Optional[str]:
        """Edit > Record Voiceover / Screen / Webcam: into the media bin (FFmpeg)."""
        from core.recording import list_devices

        try:
            devices = list_devices(find_binary("ffmpeg"))
        except Exception:  # noqa: BLE001 -- no DirectShow (not Windows): screen only
            devices = []
        dialog = RecordDialog(devices, self, kind)
        start_time = self.timeline.playhead_time
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.result_path:
            return None
        path = dialog.result_path
        if dialog.kind() == "voiceover":
            # Placed on an audio track at the playhead once the import has probed it.
            self._pending_voiceover = (path, start_time)
        self.import_media_paths([path])
        self.statusBar().showMessage(f"Recorded {os.path.basename(path)}", 6000)
        return path

    def _place_pending_voiceover(self, asset) -> bool:
        pending = getattr(self, "_pending_voiceover", None)
        if not pending or os.path.normcase(os.path.abspath(asset.path)) != os.path.normcase(os.path.abspath(pending[0])):
            return False
        self._pending_voiceover = None
        from core.commands import build_add_clip_command
        from core.track import TrackType

        # A free spot on an audio track at the recording's start, or a new audio track (one undo
        # step): a voiceover must never overwrite the dialogue under it.
        clip = self.timeline._clip_for_asset(asset, pending[1])
        command = build_add_clip_command(self.project, clip, TrackType.AUDIO)
        ok = self.timeline._execute_and_select(command)
        self.timeline.set_playhead(pending[1])
        return ok
