"""Export dialog: choose render settings and run FFmpeg in a background thread."""

from __future__ import annotations

import copy
import json
import math
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
    hdr_passthrough_choice,
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
    TEN_BIT_CODECS,
    TWO_PASS_ENCODERS,
    VIDEO_CODEC_CHOICES,
    ExportCancelled,
    ExportError,
    ExportSettings,
    Exporter,
)
from core.ffmpeg_utils import encoder_supports_10bit, nvenc_available
from core.output_color import COLOR_MODE_LABELS
from core.project import Project
from core.subtitles import SUBTITLE_CODECS, SUBTITLE_MODE_LABELS

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


class LoudnessWorker(QObject):
    """Runs Exporter.measure_loudness() on a QThread for the dialog's meter."""

    finished = Signal(object)  # LoudnessReading
    failed = Signal(str)

    def __init__(self, project: Project, settings: ExportSettings):
        super().__init__()
        self.exporter = Exporter(copy.deepcopy(project))
        self.settings = settings

    def run(self) -> None:
        try:
            self.finished.emit(self.exporter.measure_loudness(self.settings))
        except ExportCancelled:
            self.failed.emit("Measurement cancelled.")
        except ExportError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - surface any unexpected failure to the user
            self.failed.emit(f"Unexpected error: {exc}")

    def cancel(self) -> None:
        self.exporter.cancel()


def color_modes_for(codec_key: str, ext: str, hdr_choice: Optional[str], ten_bit_ok) -> list:
    """Colour modes (core.output_color keys) the dialog offers for a codec choice, 8-bit SDR first.

    10-bit SDR needs a 10-bit codec (HEVC, AV1, ProRes, DNxHR) whose encoder
    really encodes 10-bit here (``ten_bit_ok(codec_key)``); HDR passthrough
    additionally needs HDR sources that share one transfer (``hdr_choice``).
    Anything the machine or the timeline can't do is not listed at all.
    """
    modes = ["sdr8"]
    if ext == GIF_EXTENSION or codec_key not in TEN_BIT_CODECS or not ten_bit_ok(codec_key):
        return modes
    modes.append("sdr10")
    if hdr_choice:
        modes.append(hdr_choice)
    return modes


def _cpu_encoder_10bit(codec_key: str) -> bool:
    try:
        return encoder_supports_10bit(CODEC_ENCODERS[codec_key])
    except Exception:  # noqa: BLE001 - a failed probe just hides the option
        return False


class ExportDialog(QDialog):
    """Dialog to configure export settings and watch render progress.

    Non-modal (the editor stays usable during a render, which works on a
    project snapshot), deleted when closed, and Esc/Cancel during a render
    cancels it instead of hiding it. With a render queue attached
    (``queue_requested`` connected), "Add to Queue" hands the settings to it
    instead of rendering here.
    """

    #: ExportSettings for the render queue (see ui/render_queue.py).
    queue_requested = Signal(object)

    def __init__(
        self,
        project: Project,
        parent: Optional[QWidget] = None,
        in_out_range: Optional[Tuple[float, float]] = None,
        queue_available: bool = False,
    ):
        super().__init__(parent)
        self._queue_available = queue_available
        self.project = project
        self._in_out_range = in_out_range
        self.setWindowTitle("Export Video")
        self.setMinimumWidth(440)
        self._thread: Optional[QThread] = None
        self._loudness_thread: Optional[QThread] = None
        self._loudness_worker: Optional[LoudnessWorker] = None
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

        # Bit depth / dynamic range. Only modes this codec, this machine's
        # encoders and this timeline's sources support are listed.
        self._hdr_choice = hdr_passthrough_choice(self.project)
        self.color_combo = QComboBox()
        self.color_combo.setToolTip(
            "8-bit SDR plays everywhere. 10-bit (HEVC, AV1, ProRes, DNxHR) avoids banding in gradients. "
            "HDR keeps HDR footage in HDR (PQ or HLG, BT.2020); SDR clips and titles sit at HDR reference white."
        )
        self.color_combo.currentIndexChanged.connect(self._on_color_mode_changed)
        form.addRow("Color", self.color_combo)
        self.color_note_label = QLabel("")
        self.color_note_label.setWordWrap(True)
        self.color_note_label.setStyleSheet("color: #9aa1ad; font-size: 11px;")
        self.color_note_label.setVisible(False)
        form.addRow("", self.color_note_label)

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
        self.measure_btn = QPushButton("Measure")
        self.measure_btn.setToolTip(
            "Measure the mix this export would render (EBU R128: integrated loudness, true peak, "
            "loudness range), without rendering the picture."
        )
        self.measure_btn.clicked.connect(self.measure_loudness)
        loudness_row = QHBoxLayout()
        loudness_row.addWidget(self.loudness_combo, 1)
        loudness_row.addWidget(self.measure_btn)
        form.addRow("Loudness", loudness_row)
        self.loudness_reading_label = QLabel("")
        self.loudness_reading_label.setWordWrap(True)
        self.loudness_reading_label.setAccessibleName("Loudness measurement")
        form.addRow("", self.loudness_reading_label)

        self.subtitle_combo = QComboBox()
        for label, mode in SUBTITLE_MODE_LABELS:
            self.subtitle_combo.addItem(label, mode)
        self.subtitle_combo.setToolTip(
            "What happens to the \"Subtitles\" tracks: burned into the picture, embedded as a subtitle "
            "track viewers can switch on and off (MP4/MOV/MKV/WebM), or saved as a .srt file next to the video."
        )
        form.addRow("Subtitles", self.subtitle_combo)

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
        self.queue_btn = QPushButton("Add to Queue")
        self.queue_btn.setToolTip(
            "Queue this export with a snapshot of the project as it is now; queued exports render one "
            "after another in the background (View > Render Queue)."
        )
        self.queue_btn.clicked.connect(self._on_add_to_queue)
        self.queue_btn.setVisible(self._queue_available)
        self.gpu_checkbox.toggled.connect(self._on_color_mode_changed)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.queue_btn)
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
        self._refresh_color_combo()

    # ------------------------------------------------------------------
    # Colour (bit depth / HDR)
    # ------------------------------------------------------------------
    def current_color_mode(self) -> str:
        return self.color_combo.currentData() or "sdr8"

    def _refresh_color_combo(self) -> None:
        if not hasattr(self, "color_combo"):
            return
        current = self.current_color_mode()
        modes = color_modes_for(self.current_codec(), self.container_combo.currentText(), self._hdr_choice,
                                _cpu_encoder_10bit)
        self.color_combo.blockSignals(True)
        self.color_combo.clear()
        for mode in modes:
            self.color_combo.addItem(COLOR_MODE_LABELS[mode], mode)
        index = self.color_combo.findData(current)
        self.color_combo.setCurrentIndex(index if index >= 0 else 0)
        self.color_combo.setEnabled(len(modes) > 1)
        self.color_combo.blockSignals(False)
        self._on_color_mode_changed()

    def _on_color_mode_changed(self, *_args) -> None:
        mode = self.current_color_mode()
        note = ""
        if self.color_combo.count() <= 1 and self.container_combo.currentText() != GIF_EXTENSION:
            note = "10-bit and HDR need the HEVC, AV1, ProRes or DNxHR codec."
        elif mode == "pq":
            note = ("HDR10 mastering metadata (display primaries, MaxCLL) is written by the CPU HEVC encoder; "
                    "GPU and AV1 exports carry the HDR colour tags only.")
        elif mode in ("pq", "hlg", "sdr10") and self.gpu_checkbox.isChecked():
            note = "Uses the GPU encoder when it supports 10-bit, otherwise the CPU encoder."
        self.color_note_label.setText(note)
        self.color_note_label.setVisible(bool(note))

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
        self._refresh_subtitle_modes(text)
        self.gif_note_label.setVisible(is_gif)
        self._refresh_codec_combo()
        self._refresh_gpu_checkbox_enabled()
        # The combo is authoritative: keep the output path's extension in step.
        path = self.path_edit.text().strip()
        if path and container_of(path) != text:
            self.path_edit.setText(apply_container_extension(path, text))

    def _refresh_subtitle_modes(self, container: str) -> None:
        """"Embed" only for containers that carry a text subtitle stream; a
        selected embed falls back to burning in for the others."""
        embed_index = self.subtitle_combo.findData("embed")
        can_embed = container in SUBTITLE_CODECS
        self.subtitle_combo.model().item(embed_index).setEnabled(can_embed)
        if not can_embed and self.subtitle_combo.currentData() == "embed":
            self.subtitle_combo.setCurrentIndex(self.subtitle_combo.findData("burn"))

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
            "subtitles": self.subtitle_combo.currentData(),
            "color_mode": self.current_color_mode(),
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
        if "subtitles" in data:
            index = self.subtitle_combo.findData(data["subtitles"])
            if index >= 0 and self.subtitle_combo.model().item(index).isEnabled():
                self.subtitle_combo.setCurrentIndex(index)
        if "color_mode" in data:
            index = self.color_combo.findData(data["color_mode"])  # absent (unsupported here): left as is
            if index >= 0:
                self.color_combo.setCurrentIndex(index)
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

    def _on_add_to_queue(self) -> None:
        settings = self.build_settings()
        if settings is None:
            return
        self._remember_settings()
        self.queue_requested.emit(settings)
        self.accept()

    def _on_export(self) -> None:
        settings = self.build_settings()
        if settings is None:
            return
        self._remember_settings()
        self._start(settings)

    def build_settings(self) -> Optional[ExportSettings]:
        """The ExportSettings for the current controls, or None (after telling the user) when invalid."""
        output_path = self.path_edit.text().strip()
        if not output_path:
            QMessageBox.warning(self, "Export Video", "Please choose an output file.")
            return None
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
                return None

        trim_enabled = self.trim_checkbox.isChecked()
        if trim_enabled and self.trim_start_spin.value() >= self.trim_end_spin.value():
            QMessageBox.warning(self, "Export Video", "Trim Range Start must be less than End.")
            return None

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
            color_mode=self.current_color_mode(),
            subtitle_mode=self.subtitle_combo.currentData(),
        )
        return settings

    def _start(self, settings: ExportSettings) -> None:
        self.export_btn.setEnabled(False)
        self.queue_btn.setEnabled(False)
        self.status_label.setText("Starting export...")

        self._thread = QThread(self)
        self._worker = ExportWorker(self.project, settings)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        # Direct: a queued quit() never runs while cancel_and_wait() blocks the
        # GUI thread in wait(), which then always timed out (5 s and a warning).
        self._worker.finished.connect(self._thread.quit, Qt.DirectConnection)
        self._worker.failed.connect(self._thread.quit, Qt.DirectConnection)

        self._thread.start()

    def _on_progress(self, fraction: float, message: str) -> None:
        self.progress_bar.setValue(int(fraction * 100))
        self.status_label.setText(message)

    def _on_finished(self, path: str) -> None:
        self.status_label.setText(f"Export complete: {path}")
        self.export_btn.setEnabled(True)
        self.queue_btn.setEnabled(True)
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
        self.queue_btn.setEnabled(True)
        QMessageBox.critical(self, "Export Video", message)

    def _on_cancel(self) -> None:
        if self.is_running():
            self._worker.cancel()
            self.status_label.setText("Cancelling...")
        else:
            self.reject()

    # -- loudness meter -------------------------------------------------------
    def _measure_settings(self) -> ExportSettings:
        trim_enabled = self.trim_checkbox.isChecked()
        return ExportSettings(
            output_path="",
            width=self.width_spin.value(),
            height=self.height_spin.value(),
            fps=FPS_VALUES.get(self.fps_combo.currentText(), 30.0),
            reframe_override=ReframeMode.FILL if self.autofill_checkbox.isChecked() else None,
            trim_start=self.trim_start_spin.value() if trim_enabled else 0.0,
            trim_end=self.trim_end_spin.value() if trim_enabled else None,
            loudness_target=self.loudness_combo.currentData(),
        )

    def measure_loudness(self) -> None:
        if self._loudness_thread is not None:
            return
        self.measure_btn.setEnabled(False)
        self.loudness_reading_label.setText("Measuring loudness...")
        self._loudness_thread = QThread(self)
        self._loudness_worker = LoudnessWorker(self.project, self._measure_settings())
        self._loudness_worker.moveToThread(self._loudness_thread)
        self._loudness_thread.started.connect(self._loudness_worker.run)
        self._loudness_worker.finished.connect(self._on_loudness_measured)
        self._loudness_worker.failed.connect(self._on_loudness_failed)
        self._loudness_worker.finished.connect(self._loudness_thread.quit, Qt.DirectConnection)
        self._loudness_worker.failed.connect(self._loudness_thread.quit, Qt.DirectConnection)
        self._loudness_thread.finished.connect(self._clear_loudness_worker)
        self._loudness_thread.start()

    def _on_loudness_measured(self, reading) -> None:
        text = reading.describe()
        target = self.loudness_combo.currentData()
        if target is not None and math.isfinite(reading.integrated):
            text += f" -- {reading.integrated - target:+.1f} LU from the {target:.0f} LUFS target"
        self.loudness_reading_label.setText(text)

    def _on_loudness_failed(self, message: str) -> None:
        self.loudness_reading_label.setText(message)

    def _clear_loudness_worker(self) -> None:
        if self._loudness_thread is not None:
            self._loudness_thread.deleteLater()
        if self._loudness_worker is not None:
            self._loudness_worker.deleteLater()
        self._loudness_thread = None
        self._loudness_worker = None
        self.measure_btn.setEnabled(True)

    def cancel_and_wait(self, timeout_ms: int = 5000) -> bool:
        """Cancel a running export and wait for it; True when nothing is running any more."""
        if self._loudness_thread is not None:
            self._loudness_worker.cancel()
            self._loudness_thread.wait(timeout_ms)
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
