"""Export dialog: choose render settings and run FFmpeg in a background thread."""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.clip import ReframeMode
from core.exporter import ExportCancelled, ExportError, ExportSettings, Exporter, QUALITY_PRESETS
from core.project import Project

RESOLUTION_PRESETS = {
    "3840 x 2160 (4K UHD)": (3840, 2160),
    "1920 x 1080 (Full HD)": (1920, 1080),
    "1280 x 720 (HD)": (1280, 720),
    "854 x 480 (SD)": (854, 480),
    "1080 x 1920 (Vertical, 9:16)": (1080, 1920),
    "Custom": None,
}

FPS_OPTIONS = ["24", "25", "30", "50", "60"]
CONTAINER_OPTIONS = [".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".gif"]
GIF_EXTENSION = ".gif"

VERTICAL_RESOLUTION_PRESET = "1080 x 1920 (Vertical, 9:16)"
SHORTS_PLATFORM_NONE = "None (custom settings)"
# Approximate, adjustable defaults for each platform's short-form vertical
# video slot — not a legal or contractual limit, just a sensible starting
# point the user can override via Trim Range. None means "no automatic cap".
PLATFORM_DURATION_CAPS = {
    "YouTube Shorts": 60.0,
    "TikTok": None,
    "Instagram Reels": 90.0,
}
SHORTS_PLATFORM_OPTIONS = [SHORTS_PLATFORM_NONE] + list(PLATFORM_DURATION_CAPS.keys())


class ExportWorker(QObject):
    progress = Signal(float, str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, project: Project, settings: ExportSettings):
        super().__init__()
        self.exporter = Exporter(project)
        self.settings = settings

    def run(self) -> None:
        try:
            path = self.exporter.export(self.settings, on_progress=lambda f, msg: self.progress.emit(f, msg))
            self.finished.emit(path)
        except ExportCancelled:
            self.failed.emit("Export cancelled.")
        except ExportError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - surface any unexpected failure to the user
            self.failed.emit(f"Unexpected error: {exc}")

    def cancel(self) -> None:
        self.exporter.cancel()


class ExportDialog(QDialog):
    """Modal dialog to configure export settings and watch render progress."""

    def __init__(self, project: Project, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project = project
        self.setWindowTitle("Export Video")
        self.setMinimumWidth(440)
        self._thread: Optional[QThread] = None
        self._worker: Optional[ExportWorker] = None

        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()

        path_row = QHBoxLayout()
        default_name = os.path.join(os.path.expanduser("~"), "Videos", f"{self.project.name}.mp4")
        self.path_edit = QLineEdit(default_name)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_btn)
        form.addRow("Output File", path_row)

        self.container_combo = QComboBox()
        self.container_combo.addItems(CONTAINER_OPTIONS)
        self.container_combo.currentTextChanged.connect(self._on_container_changed)
        form.addRow("Format", self.container_combo)

        self.resolution_combo = QComboBox()
        self.resolution_combo.addItems(list(RESOLUTION_PRESETS.keys()))
        self.resolution_combo.setCurrentText("1920 x 1080 (Full HD)")
        self.resolution_combo.currentTextChanged.connect(self._on_resolution_changed)
        form.addRow("Resolution", self.resolution_combo)

        custom_row = QHBoxLayout()
        self.width_spin = QSpinBox()
        self.width_spin.setRange(16, 7680)
        self.width_spin.setValue(1920)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(16, 4320)
        self.height_spin.setValue(1080)
        custom_row.addWidget(QLabel("W"))
        custom_row.addWidget(self.width_spin)
        custom_row.addWidget(QLabel("H"))
        custom_row.addWidget(self.height_spin)
        self.width_spin.setEnabled(False)
        self.height_spin.setEnabled(False)
        form.addRow("Custom Size", custom_row)

        self.fps_combo = QComboBox()
        self.fps_combo.addItems(FPS_OPTIONS)
        self.fps_combo.setCurrentText("30")
        form.addRow("Frame Rate", self.fps_combo)

        self.quality_combo = QComboBox()
        self.quality_combo.addItems(list(QUALITY_PRESETS.keys()))
        self.quality_combo.setCurrentText("Balanced (CRF 23)")
        form.addRow("Quality", self.quality_combo)

        two_pass_row = QHBoxLayout()
        self.two_pass_checkbox = QCheckBox("Two-pass, target file size")
        self.two_pass_checkbox.toggled.connect(self._on_two_pass_toggled)
        self.target_size_spin = QDoubleSpinBox()
        self.target_size_spin.setRange(1.0, 20000.0)
        self.target_size_spin.setValue(100.0)
        self.target_size_spin.setSuffix(" MB")
        self.target_size_spin.setEnabled(False)
        two_pass_row.addWidget(self.two_pass_checkbox)
        two_pass_row.addWidget(self.target_size_spin)
        form.addRow("", two_pass_row)

        self.gif_note_label = QLabel("Animated GIF: no audio track; quality comes from palette dithering, not the setting above.")
        self.gif_note_label.setWordWrap(True)
        self.gif_note_label.setStyleSheet("color: #9aa1ad; font-size: 11px;")
        self.gif_note_label.setVisible(False)
        form.addRow("", self.gif_note_label)

        # ---- Shorts / vertical video ----
        self.platform_combo = QComboBox()
        self.platform_combo.addItems(SHORTS_PLATFORM_OPTIONS)
        self.platform_combo.currentTextChanged.connect(self._on_platform_changed)
        form.addRow("Shorts Platform", self.platform_combo)

        self.autofill_checkbox = QCheckBox("Auto-fill vertical frame (crop instead of letterbox)")
        form.addRow("", self.autofill_checkbox)

        trim_row = QHBoxLayout()
        self.trim_checkbox = QCheckBox("Limit duration")
        self.trim_checkbox.toggled.connect(self._on_trim_toggled)
        self.trim_start_spin = QDoubleSpinBox()
        self.trim_start_spin.setRange(0.0, 36000.0)
        self.trim_start_spin.setSuffix(" s")
        self.trim_start_spin.setEnabled(False)
        self.trim_end_spin = QDoubleSpinBox()
        self.trim_end_spin.setRange(0.1, 36000.0)
        self.trim_end_spin.setSuffix(" s")
        self.trim_end_spin.setValue(max(self.project.total_duration, 0.1))
        self.trim_end_spin.setEnabled(False)
        trim_row.addWidget(self.trim_checkbox)
        trim_row.addWidget(QLabel("Start"))
        trim_row.addWidget(self.trim_start_spin)
        trim_row.addWidget(QLabel("End"))
        trim_row.addWidget(self.trim_end_spin)
        form.addRow("Trim Range", trim_row)

        self.shorts_note_label = QLabel("")
        self.shorts_note_label.setWordWrap(True)
        self.shorts_note_label.setStyleSheet("color: #9aa1ad; font-size: 11px;")
        self.shorts_note_label.setVisible(False)
        form.addRow("", self.shorts_note_label)

        layout.addLayout(form)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #9aa1ad;")
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.export_btn = QPushButton("Export")
        self.export_btn.setObjectName("PrimaryButton")
        self.export_btn.clicked.connect(self._on_export)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.export_btn)
        layout.addLayout(buttons)

    def _on_two_pass_toggled(self, checked: bool) -> None:
        self.target_size_spin.setEnabled(checked)
        self.quality_combo.setEnabled(not checked)

    def _on_container_changed(self, text: str) -> None:
        is_gif = text == GIF_EXTENSION
        if is_gif:
            # Uncheck first: this fires _on_two_pass_toggled(False), which
            # would otherwise re-enable quality_combo after we disable it below.
            self.two_pass_checkbox.setChecked(False)
        self.quality_combo.setEnabled(not is_gif)
        self.two_pass_checkbox.setEnabled(not is_gif)
        self.gif_note_label.setVisible(is_gif)

    def _on_resolution_changed(self, text: str) -> None:
        preset = RESOLUTION_PRESETS.get(text)
        is_custom = preset is None
        self.width_spin.setEnabled(is_custom)
        self.height_spin.setEnabled(is_custom)
        if preset is not None:
            self.width_spin.setValue(preset[0])
            self.height_spin.setValue(preset[1])

    def _on_trim_toggled(self, checked: bool) -> None:
        self.trim_start_spin.setEnabled(checked)
        self.trim_end_spin.setEnabled(checked)

    def _on_platform_changed(self, text: str) -> None:
        if text == SHORTS_PLATFORM_NONE:
            self.shorts_note_label.setVisible(False)
            return

        self.resolution_combo.setCurrentText(VERTICAL_RESOLUTION_PRESET)
        self.fps_combo.setCurrentText("30")
        self.autofill_checkbox.setChecked(True)

        cap = PLATFORM_DURATION_CAPS.get(text)
        if cap is not None:
            # Enforce the cap by clamping the End spinbox's own range, so a
            # manual edit afterward can't exceed it while this platform is
            # still selected — not just a one-time default value.
            self.trim_end_spin.setMaximum(cap)
            self.trim_checkbox.setChecked(True)
            self.trim_start_spin.setValue(0.0)
            self.trim_end_spin.setValue(min(cap, max(self.project.total_duration, 0.1)))
            self.shorts_note_label.setText(
                f"{text} preset applied: 1080x1920 vertical, auto-fill crop, capped at {cap:.0f}s "
                "(adjust Trim Range End down if you want it shorter)."
            )
        else:
            self.trim_end_spin.setMaximum(36000.0)
            self.shorts_note_label.setText(
                f"{text} preset applied: 1080x1920 vertical, auto-fill crop. No automatic duration cap "
                "— use Trim Range to limit it yourself."
            )
        self.shorts_note_label.setVisible(True)

    def _browse(self) -> None:
        ext = self.container_combo.currentText()
        path, _ = QFileDialog.getSaveFileName(self, "Export Video", self.path_edit.text(), f"Video (*{ext})")
        if path:
            if not path.lower().endswith(ext):
                path += ext
            self.path_edit.setText(path)

    def _on_export(self) -> None:
        output_path = self.path_edit.text().strip()
        if not output_path:
            QMessageBox.warning(self, "Export Video", "Please choose an output file.")
            return
        ext = self.container_combo.currentText()
        if not output_path.lower().endswith(tuple(CONTAINER_OPTIONS)):
            output_path += ext

        if os.path.exists(output_path):
            result = QMessageBox.question(
                self,
                "Export Video",
                f"{output_path}\n\nThis file already exists. Overwrite it?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if result != QMessageBox.Yes:
                return

        trim_enabled = self.trim_checkbox.isChecked()
        if trim_enabled and self.trim_start_spin.value() >= self.trim_end_spin.value():
            QMessageBox.warning(self, "Export Video", "Trim Range Start must be less than End.")
            return

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        settings = ExportSettings(
            output_path=output_path,
            width=self.width_spin.value(),
            height=self.height_spin.value(),
            fps=float(self.fps_combo.currentText()),
            quality_preset=self.quality_combo.currentText(),
            two_pass=self.two_pass_checkbox.isChecked(),
            target_size_mb=self.target_size_spin.value() if self.two_pass_checkbox.isChecked() else None,
            reframe_override=ReframeMode.FILL if self.autofill_checkbox.isChecked() else None,
            trim_start=self.trim_start_spin.value() if trim_enabled else 0.0,
            trim_end=self.trim_end_spin.value() if trim_enabled else None,
        )

        self.export_btn.setEnabled(False)
        self.status_label.setText("Starting export...")

        self._thread = QThread(self)
        self._worker = ExportWorker(self.project, settings)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)

        self._thread.start()

    def _on_progress(self, fraction: float, message: str) -> None:
        self.progress_bar.setValue(int(fraction * 100))
        self.status_label.setText(message)

    def _on_finished(self, path: str) -> None:
        self.status_label.setText(f"Export complete: {path}")
        self.export_btn.setEnabled(True)
        QMessageBox.information(self, "Export Video", f"Video exported successfully to:\n{path}")
        self.accept()

    def _on_failed(self, message: str) -> None:
        self.status_label.setText(message)
        self.export_btn.setEnabled(True)
        QMessageBox.critical(self, "Export Video", message)

    def _on_cancel(self) -> None:
        if self._worker is not None and self._thread is not None and self._thread.isRunning():
            self._worker.cancel()
            self.status_label.setText("Cancelling...")
        else:
            self.reject()

    def closeEvent(self, event) -> None:
        if self._worker is not None and self._thread is not None and self._thread.isRunning():
            self._worker.cancel()  # now terminates the ffmpeg process immediately (core/exporter.py)
            self.status_label.setText("Cancelling export...")
            if not self._thread.wait(5000):
                # FFmpeg still hasn't exited — don't destroy a QThread/QObject
                # that's still running underneath it; ask the user to wait
                # rather than risk a crash on teardown.
                QMessageBox.warning(
                    self,
                    "Export Video",
                    "The export is still shutting down. Please wait a moment and try closing again.",
                )
                event.ignore()
                return
        super().closeEvent(event)
