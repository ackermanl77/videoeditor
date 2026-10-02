"""Export dialog: choose render settings and run FFmpeg in a background thread."""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, Optional, Tuple

from PySide6.QtCore import QCoreApplication, QObject, QSettings, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
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
from core.export_presets import (
    BUILTIN_PRESETS,
    FPS_CHOICES,
    delete_user_preset,
    load_user_presets,
    save_user_preset,
    source_defaults,
)
from core.exporter import (
    AUDIO_BITRATE_KBPS,
    AUDIO_BITRATE_OPTIONS_KBPS,
    CODEC_CONTAINERS,
    CODEC_ENCODERS,
    LOUDNESS_TARGETS,
    QUALITY_PRESETS,
    TWO_PASS_ENCODERS,
    VIDEO_CODEC_CHOICES,
    ExportCancelled,
    ExportError,
    ExportSettings,
    Exporter,
)
from core.ffmpeg_utils import nvenc_available
from core.project import Project

RESOLUTION_PRESETS = {
    "3840 x 2160 (4K UHD)": (3840, 2160),
    "1920 x 1080 (Full HD)": (1920, 1080),
    "1280 x 720 (HD)": (1280, 720),
    "854 x 480 (SD)": (854, 480),
    "1080 x 1920 (Vertical, 9:16)": (1080, 1920),
    "Custom": None,
}

FPS_OPTIONS = [label for label, _value in FPS_CHOICES]
FPS_VALUES = dict(FPS_CHOICES)
LAST_SETTINGS_KEY = "export/last_settings"
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


def container_of(path: str) -> Optional[str]:
    """The path's extension if it is one of CONTAINER_OPTIONS (lower-cased), else None."""
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in CONTAINER_OPTIONS else None


def apply_container_extension(path: str, ext: str) -> str:
    """``path`` with the chosen container's extension: a known extension is replaced, anything else appended.

    The Format combo is authoritative -- the exporter picks the codec from
    the path's extension, and the dialog used to only *append* the combo's
    extension when the path had none, so choosing .webm or .gif with the
    default ``...mp4`` path still produced an MP4.
    """
    if container_of(path) is not None:
        return os.path.splitext(path)[0] + ext
    return path + ext


def codecs_for_container(ext: str):
    """Video codec choices (VIDEO_CODEC_CHOICES keys) valid for a container, "auto" first."""
    if ext == GIF_EXTENSION:
        return ["auto"]
    return ["auto"] + [key for key, exts in CODEC_CONTAINERS.items() if ext in exts]


def _settings_store() -> Optional[QSettings]:
    """The app's QSettings, or None when no organization is set (tests): nothing is persisted then."""
    if not QCoreApplication.organizationName():
        return None
    return QSettings()


class ExportWorker(QObject):
    progress = Signal(float, str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, project: Project, settings: ExportSettings):
        super().__init__()
        # The exporter renders its own deep copy of the project: the dialog is
        # non-modal, so the user may keep editing while this runs.
        self.exporter = Exporter(copy.deepcopy(project))
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
    """Dialog to configure export settings and watch render progress.

    Non-modal (the editor stays usable during a render, which works on a
    project snapshot), deleted when closed, and Esc/Cancel during a render
    cancels it instead of hiding it.
    """

    def __init__(
        self,
        project: Project,
        parent: Optional[QWidget] = None,
        in_out_range: Optional[Tuple[float, float]] = None,
    ):
        super().__init__(parent)
        self.project = project
        self._in_out_range = in_out_range
        self.setWindowTitle("Export Video")
        self.setMinimumWidth(440)
        self._thread: Optional[QThread] = None
        self._worker: Optional[ExportWorker] = None
        self.warnings = []
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        self._build_ui()
        self._restore_last_settings()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()

        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip("Apply a saved group of export settings")
        self._reload_presets()
        self.preset_combo.activated.connect(self._on_preset_chosen)
        self.save_preset_btn = QPushButton("Save...")
        self.save_preset_btn.setToolTip("Save the current settings as a preset")
        self.save_preset_btn.clicked.connect(self._on_save_preset)
        self.delete_preset_btn = QPushButton("Delete")
        self.delete_preset_btn.setToolTip("Delete the selected user preset")
        self.delete_preset_btn.clicked.connect(self._on_delete_preset)
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(self.save_preset_btn)
        preset_row.addWidget(self.delete_preset_btn)
        form.addRow("Preset", preset_row)

        path_row = QHBoxLayout()
        default_name = os.path.join(os.path.expanduser("~"), "Videos", f"{self.project.name}.mp4")
        self.path_edit = QLineEdit(default_name)
        self.path_edit.editingFinished.connect(self._on_path_edited)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_btn)
        form.addRow("Output File", path_row)

        self.container_combo = QComboBox()
        self.container_combo.addItems(CONTAINER_OPTIONS)
        self.container_combo.currentTextChanged.connect(self._on_container_changed)
        form.addRow("Format", self.container_combo)

        self.codec_combo = QComboBox()
        self.codec_combo.setToolTip(
            "HEVC and AV1 make smaller files at the same quality; ProRes and DNxHR are large, "
            "edit-friendly intermediates."
        )
        self.codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        form.addRow("Video Codec", self.codec_combo)

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
        self.fps_combo.setToolTip("23.976 / 29.97 / 59.94 are the exact NTSC rates (24000/1001...) of most camera footage")
        form.addRow("Frame Rate", self.fps_combo)

        self.quality_combo = QComboBox()
        self.quality_combo.addItems(list(QUALITY_PRESETS.keys()))
        self.quality_combo.setCurrentText("Balanced (CRF 23)")
        form.addRow("Quality", self.quality_combo)

        # Probed once (cached in core.ffmpeg_utils.nvenc_available) against
        # whichever ffmpeg binary this app resolves to -- bundled or the
        # user's own system ffmpeg, which might be a build without NVENC.
        try:
            self._gpu_detected = nvenc_available()
        except Exception:
            self._gpu_detected = False
        self.gpu_checkbox = QCheckBox("Use GPU encoding (NVENC) when available")
        self.gpu_checkbox.setChecked(self._gpu_detected)
        self.gpu_checkbox.setEnabled(self._gpu_detected)
        if self._gpu_detected:
            self.gpu_checkbox.setToolTip(
                "Encodes with your NVIDIA GPU (NVENC) instead of the CPU for MP4/MOV/MKV/AVI/FLV output -- "
                "much faster on supported hardware. Falls back to the CPU encoder automatically for this "
                "export if GPU encoding fails partway through."
            )
        else:
            self.gpu_checkbox.setToolTip(
                "NVENC hardware encoding wasn't detected (no supported NVIDIA GPU/driver, or this ffmpeg "
                "build doesn't include it) -- exporting with the CPU encoder."
            )
        form.addRow("", self.gpu_checkbox)

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

        self.gpu_target_checkbox = QCheckBox("Target size on the GPU (one pass, approximate size)")
        self.gpu_target_checkbox.setToolTip(
            "One NVENC pass with a capped bitrate: much faster, lands close to the target. "
            "Off: CPU two-pass, which hits the size exactly."
        )
        self.gpu_target_checkbox.setEnabled(False)
        form.addRow("", self.gpu_target_checkbox)

        self.audio_bitrate_combo = QComboBox()
        for kbps in AUDIO_BITRATE_OPTIONS_KBPS:
            self.audio_bitrate_combo.addItem(f"{kbps} kbps", kbps)
        self.audio_bitrate_combo.setCurrentIndex(self.audio_bitrate_combo.findData(AUDIO_BITRATE_KBPS))
        form.addRow("Audio Bitrate", self.audio_bitrate_combo)

        self.loudness_combo = QComboBox()
        for label, target in LOUDNESS_TARGETS.items():
            self.loudness_combo.addItem(label, target)
        self.loudness_combo.setToolTip(
            "Normalize the mix to a platform's loudness (two-pass EBU R128 loudnorm, true peak -1 dBTP). "
            "Peaks are always limited to -1 dBFS."
        )
        form.addRow("Loudness", self.loudness_combo)

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

        # Now that container_combo/two_pass_checkbox both exist, resolve the
        # GPU checkbox's actual initial enabled state/tooltip against them
        # (matches the defaults already set above for the common case, but
        # keeps this correct if those defaults ever change).
        self._refresh_gpu_checkbox_enabled()

        self._refresh_codec_combo()

        # In/Out points set on the timeline (I/O keys) pre-fill Trim Range.
        if self._in_out_range is not None:
            start, end = self._in_out_range
            self.trim_checkbox.setChecked(True)
            self.trim_start_spin.setValue(start)
            self.trim_end_spin.setValue(end)

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

    # ------------------------------------------------------------------
    # Codec choice
    # ------------------------------------------------------------------
    def current_codec(self) -> str:
        return self.codec_combo.currentData() or "auto"

    def _refresh_codec_combo(self) -> None:
        current = self.current_codec() if self.codec_combo.count() else "auto"
        self.codec_combo.blockSignals(True)
        self.codec_combo.clear()
        for key in codecs_for_container(self.container_combo.currentText()):
            self.codec_combo.addItem(VIDEO_CODEC_CHOICES[key], key)
        index = self.codec_combo.findData(current)
        self.codec_combo.setCurrentIndex(index if index >= 0 else 0)
        self.codec_combo.blockSignals(False)
        self._on_codec_changed()

    def _effective_encoder(self) -> str:
        from core.exporter import resolve_codecs

        try:
            return resolve_codecs(self.container_combo.currentText(), self.current_codec())[0]
        except ValueError:
            return ""

    def _on_codec_changed(self, *_args) -> None:
        if not hasattr(self, "two_pass_checkbox"):
            return
        is_gif = self.container_combo.currentText() == GIF_EXTENSION
        two_pass_ok = not is_gif and self._effective_encoder() in TWO_PASS_ENCODERS
        if not two_pass_ok and self.two_pass_checkbox.isChecked():
            self.two_pass_checkbox.setChecked(False)
        self.two_pass_checkbox.setEnabled(two_pass_ok)
        self.two_pass_checkbox.setToolTip(
            "" if two_pass_ok else "Target file size needs H.264, VP9 or AV1."
        )
        intermediate = self.current_codec() in ("prores", "dnxhr")
        self.quality_combo.setEnabled(not is_gif and not intermediate and not self.two_pass_checkbox.isChecked())
        self._refresh_gpu_checkbox_enabled()

    def _on_two_pass_toggled(self, checked: bool) -> None:
        self.target_size_spin.setEnabled(checked)
        self.quality_combo.setEnabled(not checked)
        # core.exporter.Exporter always uses the CPU codec for two-pass,
        # target-file-size exports regardless of this checkbox (NVENC's
        # two-pass rate control doesn't use the same -pass 1/2 log-file
        # scheme libx264 does) -- reflect that in the UI rather than
        # offering a control that silently does nothing while two-pass is on.
        self._refresh_gpu_checkbox_enabled()

    def _refresh_gpu_checkbox_enabled(self) -> None:
        from core.exporter import NVENC_CODEC_MAP

        is_gpu_capable_format = self._effective_encoder() in NVENC_CODEC_MAP
        two_pass = self.two_pass_checkbox.isChecked()
        enabled = self._gpu_detected and is_gpu_capable_format and not two_pass
        self.gpu_checkbox.setEnabled(enabled)
        if hasattr(self, "gpu_target_checkbox"):
            self.gpu_target_checkbox.setEnabled(self._gpu_detected and is_gpu_capable_format and two_pass)
        if not self._gpu_detected:
            return
        if not is_gpu_capable_format:
            self.gpu_checkbox.setToolTip("GPU encoding isn't available for this format/codec -- using the CPU encoder.")
        elif two_pass:
            self.gpu_checkbox.setToolTip(
                "Target-size exports use CPU two-pass unless 'Target size on the GPU' below is ticked."
            )
        else:
            self.gpu_checkbox.setToolTip(
                "Encodes with your NVIDIA GPU (NVENC) instead of the CPU for MP4/MOV/MKV/AVI/FLV output -- "
                "much faster on supported hardware. Falls back to the CPU encoder automatically for this "
                "export if GPU encoding fails partway through."
            )

    def _on_container_changed(self, text: str) -> None:
        is_gif = text == GIF_EXTENSION
        if is_gif:
            # Uncheck first: this fires _on_two_pass_toggled(False), which
            # would otherwise re-enable quality_combo after we disable it below.
            self.two_pass_checkbox.setChecked(False)
        self.quality_combo.setEnabled(not is_gif)
        self.two_pass_checkbox.setEnabled(not is_gif)
        self.audio_bitrate_combo.setEnabled(not is_gif)
        self.loudness_combo.setEnabled(not is_gif)
        self.gif_note_label.setVisible(is_gif)
        self._refresh_codec_combo()
        self._refresh_gpu_checkbox_enabled()
        # The combo is authoritative: keep the output path's extension in step.
        path = self.path_edit.text().strip()
        if path and container_of(path) != text:
            self.path_edit.setText(apply_container_extension(path, text))

    def _on_path_edited(self) -> None:
        """Typing a different known extension selects that format."""
        ext = container_of(self.path_edit.text().strip())
        if ext is not None and ext != self.container_combo.currentText():
            self.container_combo.setCurrentText(ext)

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
            self.path_edit.setText(apply_container_extension(path, ext) if container_of(path) != ext else path)

    # ------------------------------------------------------------------
    # Presets and remembered settings
    # ------------------------------------------------------------------
    def current_settings_dict(self) -> Dict[str, Any]:
        """The dialog's reusable settings (not the file name or trim range), as a preset dict."""
        return {
            "container": self.container_combo.currentText(),
            "video_codec": self.current_codec(),
            "width": self.width_spin.value(),
            "height": self.height_spin.value(),
            "fps": FPS_VALUES.get(self.fps_combo.currentText(), 30.0),
            "quality": self.quality_combo.currentText(),
            "use_gpu": self.gpu_checkbox.isChecked(),
            "two_pass": self.two_pass_checkbox.isChecked(),
            "target_size_mb": self.target_size_spin.value(),
            "gpu_target_size": self.gpu_target_checkbox.isChecked(),
            "audio_bitrate": self.audio_bitrate_combo.currentData(),
            "loudness": self.loudness_combo.currentData(),
            "output_dir": os.path.dirname(self.path_edit.text().strip()),
        }

    def apply_settings(self, data: Dict[str, Any]) -> None:
        """Apply a preset/remembered dict; missing keys leave their control unchanged."""
        if data.get("container") in CONTAINER_OPTIONS:
            self.container_combo.setCurrentText(data["container"])
        if "video_codec" in data:
            index = self.codec_combo.findData(data["video_codec"])
            if index >= 0:
                self.codec_combo.setCurrentIndex(index)
        if data.get("width") and data.get("height"):
            self.set_resolution(int(data["width"]), int(data["height"]))
        if data.get("fps"):
            label = min(FPS_CHOICES, key=lambda c: abs(c[1] - float(data["fps"])))[0]
            self.fps_combo.setCurrentText(label)
        if data.get("quality") in QUALITY_PRESETS:
            self.quality_combo.setCurrentText(data["quality"])
        if "audio_bitrate" in data:
            index = self.audio_bitrate_combo.findData(data["audio_bitrate"])
            if index >= 0:
                self.audio_bitrate_combo.setCurrentIndex(index)
        if "loudness" in data:
            index = self.loudness_combo.findData(data["loudness"])
            if index >= 0:
                self.loudness_combo.setCurrentIndex(index)
        if "use_gpu" in data and self.gpu_checkbox.isEnabled():
            self.gpu_checkbox.setChecked(bool(data["use_gpu"]))
        if "two_pass" in data and self.two_pass_checkbox.isEnabled():
            self.two_pass_checkbox.setChecked(bool(data["two_pass"]))
        if data.get("target_size_mb"):
            self.target_size_spin.setValue(float(data["target_size_mb"]))
        if "gpu_target_size" in data:
            self.gpu_target_checkbox.setChecked(bool(data["gpu_target_size"]))
        if data.get("output_dir") and os.path.isdir(data["output_dir"]):
            name = os.path.splitext(os.path.basename(self.path_edit.text().strip()))[0] or self.project.name
            self.path_edit.setText(os.path.join(data["output_dir"], name + self.container_combo.currentText()))
        if data.get("platform") in PLATFORM_DURATION_CAPS:
            self.platform_combo.setCurrentText(data["platform"])

    def set_resolution(self, width: int, height: int) -> None:
        for label, preset in RESOLUTION_PRESETS.items():
            if preset == (width, height):
                self.resolution_combo.setCurrentText(label)
                return
        self.resolution_combo.setCurrentText("Custom")
        self.width_spin.setValue(width)
        self.height_spin.setValue(height)

    def _restore_last_settings(self) -> None:
        """Last export's settings, then the dominant source's size and frame rate (if any).

        The project's own footage decides resolution/fps (exporting 29.97
        camera footage at 30 made a frame judder every 33 s); everything
        else (format, codec, quality, loudness, folder) is remembered.
        """
        store = _settings_store()
        if store is not None:
            try:
                remembered = json.loads(store.value(LAST_SETTINGS_KEY, "") or "{}")
            except (TypeError, ValueError):
                remembered = {}
            if isinstance(remembered, dict):
                self.apply_settings(remembered)
        defaults = source_defaults(self.project)
        if defaults is not None:
            width, height, fps_label = defaults
            self.set_resolution(width, height)
            if fps_label:
                self.fps_combo.setCurrentText(fps_label)

    def _remember_settings(self) -> None:
        store = _settings_store()
        if store is not None:
            store.setValue(LAST_SETTINGS_KEY, json.dumps(self.current_settings_dict()))

    def _reload_presets(self) -> None:
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("Choose a preset...", None)
        for name in BUILTIN_PRESETS:
            self.preset_combo.addItem(name, ("builtin", name))
        for name in sorted(load_user_presets()):
            self.preset_combo.addItem(f"{name} (saved)", ("user", name))
        self.preset_combo.blockSignals(False)

    def preset_data(self, key) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        kind, name = key
        return BUILTIN_PRESETS.get(name) if kind == "builtin" else load_user_presets().get(name)

    def _on_preset_chosen(self, index: int) -> None:
        data = self.preset_data(self.preset_combo.itemData(index))
        if data is not None:
            self.apply_settings(data)

    def _on_save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "Save Export Preset", "Preset name:")
        if not ok or not name.strip():
            return
        save_user_preset(name.strip(), self.current_settings_dict())
        self._reload_presets()
        self.preset_combo.setCurrentIndex(self.preset_combo.findData(("user", name.strip())))

    def _on_delete_preset(self) -> None:
        key = self.preset_combo.currentData()
        if not key or key[0] != "user":
            QMessageBox.information(self, "Export Presets", "Built-in presets can't be deleted.")
            return
        delete_user_preset(key[1])
        self._reload_presets()

    # ------------------------------------------------------------------
    def is_running(self) -> bool:
        return self._worker is not None and self._thread is not None and self._thread.isRunning()

    def _on_export(self) -> None:
        output_path = self.path_edit.text().strip()
        if not output_path:
            QMessageBox.warning(self, "Export Video", "Please choose an output file.")
            return
        ext = self.container_combo.currentText()
        if container_of(output_path) != ext:
            output_path = apply_container_extension(output_path, ext)
            self.path_edit.setText(output_path)

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
            fps=FPS_VALUES.get(self.fps_combo.currentText(), 30.0),
            quality_preset=self.quality_combo.currentText(),
            two_pass=self.two_pass_checkbox.isChecked(),
            target_size_mb=self.target_size_spin.value() if self.two_pass_checkbox.isChecked() else None,
            reframe_override=ReframeMode.FILL if self.autofill_checkbox.isChecked() else None,
            trim_start=self.trim_start_spin.value() if trim_enabled else 0.0,
            trim_end=self.trim_end_spin.value() if trim_enabled else None,
            use_gpu=self.gpu_checkbox.isChecked(),
            audio_bitrate_kbps=int(self.audio_bitrate_combo.currentData()),
            loudness_target=self.loudness_combo.currentData(),
            video_codec=self.current_codec(),
            gpu_target_size=self.gpu_target_checkbox.isChecked() and self.gpu_target_checkbox.isEnabled(),
        )
        self._remember_settings()

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
        self.warnings = list(self._worker.exporter.warnings) if self._worker is not None else []
        message = f"Video exported successfully to:\n{path}"
        if self.warnings:
            message += "\n\nWarnings:\n" + "\n".join(self.warnings)
        QMessageBox.information(self, "Export Video", message)
        self.accept()

    def _on_failed(self, message: str) -> None:
        # ExportError messages can carry FFmpeg's last ~30 log lines; the
        # status label gets just the headline, the dialog below the full text.
        self.status_label.setText(message.splitlines()[0] if message else message)
        self.export_btn.setEnabled(True)
        QMessageBox.critical(self, "Export Video", message)

    def _on_cancel(self) -> None:
        if self.is_running():
            self._worker.cancel()
            self.status_label.setText("Cancelling...")
        else:
            self.reject()

    def cancel_and_wait(self, timeout_ms: int = 5000) -> bool:
        """Cancel a running export and wait for it; True when nothing is running any more."""
        if not self.is_running():
            return True
        self._worker.cancel()  # terminates the ffmpeg process immediately (core/exporter.py)
        self.status_label.setText("Cancelling export...")
        if self._thread.wait(timeout_ms):
            return True
        # FFmpeg still hasn't exited: don't destroy a QThread/QObject that's
        # still running underneath it; ask the user to wait instead.
        QMessageBox.warning(
            self, "Export Video", "The export is still shutting down. Please wait a moment and try again."
        )
        return False

    def reject(self) -> None:
        """Esc / Cancel / window close: cancel a running export first.

        QDialog.reject() used to just hide the dialog -- Esc left the render
        running invisibly with no way to cancel it.
        """
        if self.cancel_and_wait():
            super().reject()

    def closeEvent(self, event) -> None:
        if not self.cancel_and_wait():
            event.ignore()
            return
        super().closeEvent(event)
