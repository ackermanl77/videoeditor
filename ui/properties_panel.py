"""The right-hand inspector: edit properties of the selected clip."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.clip import Clip, MediaType, ReframeMode, TextStyle, TransitionType
from core.commands import UpdateClipPropertyCommand, UpdateTextClipCommand
from core.history import History
from core.project import Project


class _PlainTextEdit(QTextEdit):
    """A QTextEdit that emits ``editingFinished`` when it loses focus.

    QTextEdit has no built-in equivalent to QLineEdit's ``editingFinished``,
    which we need so text-overlay edits are committed to undo history once
    per edit session rather than on every keystroke.
    """

    editingFinished = Signal()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.editingFinished.emit()


MEDIA_TYPE_LABEL = {
    MediaType.VIDEO: "Video Clip",
    MediaType.AUDIO: "Audio Clip",
    MediaType.IMAGE: "Image Clip",
    MediaType.TEXT: "Text Clip",
}


class PropertiesPanel(QWidget):
    """Displays and edits the properties of the currently selected clip."""

    project_modified = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project: Optional[Project] = None
        self.history: Optional[History] = None
        self.clip_id: Optional[str] = None
        self._loading = False
        self._pre_drag_value = None

        self._build_ui()
        self.show_empty_state()

    # ------------------------------------------------------------------
    @staticmethod
    def _make_keyframe_table(headers: list) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setMaximumHeight(110)
        return table

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(3, 3, 3, 3)

        title = QLabel("PROPERTIES")
        title.setObjectName("SectionTitle")
        title.setContentsMargins(8, 8, 8, 0)
        outer.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll, 1)

        container = QWidget()
        scroll.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 6, 10, 10)
        layout.setSpacing(10)

        self.empty_label = QLabel("Select a clip on the timeline to edit its properties.")
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet("color: #9aa1ad; padding: 12px;")
        layout.addWidget(self.empty_label)

        self.name_label = QLabel("")
        font = self.name_label.font()
        font.setBold(True)
        font.setPointSize(12)
        self.name_label.setFont(font)
        layout.addWidget(self.name_label)

        # ---- Clip group ----
        self.clip_group = QGroupBox("Clip Adjustments")
        clip_form = QFormLayout(self.clip_group)

        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 200)
        clip_form.addRow("Volume", self.volume_slider)

        self.mute_checkbox = QCheckBox("Mute audio")
        clip_form.addRow("", self.mute_checkbox)

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.25, 4.0)
        self.speed_spin.setSingleStep(0.05)
        self.speed_spin.setSuffix(" x")
        clip_form.addRow("Speed", self.speed_spin)

        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(0, 100)
        clip_form.addRow("Opacity", self.opacity_slider)

        self.fade_in_spin = QDoubleSpinBox()
        self.fade_in_spin.setRange(0.0, 30.0)
        self.fade_in_spin.setSingleStep(0.1)
        self.fade_in_spin.setSuffix(" s")
        clip_form.addRow("Fade In", self.fade_in_spin)

        self.fade_out_spin = QDoubleSpinBox()
        self.fade_out_spin.setRange(0.0, 30.0)
        self.fade_out_spin.setSingleStep(0.1)
        self.fade_out_spin.setSuffix(" s")
        clip_form.addRow("Fade Out", self.fade_out_spin)

        layout.addWidget(self.clip_group)

        # ---- Transform group (video/image only) ----
        self.transform_group = QGroupBox("Transform")
        transform_form = QFormLayout(self.transform_group)

        self.transform_x_slider = QSlider(Qt.Horizontal)
        self.transform_x_slider.setRange(0, 100)
        transform_form.addRow("Position X", self.transform_x_slider)

        self.transform_y_slider = QSlider(Qt.Horizontal)
        self.transform_y_slider.setRange(0, 100)
        transform_form.addRow("Position Y", self.transform_y_slider)

        self.transform_scale_slider = QSlider(Qt.Horizontal)
        self.transform_scale_slider.setRange(10, 300)
        transform_form.addRow("Scale", self.transform_scale_slider)

        self.transform_rotation_slider = QSlider(Qt.Horizontal)
        self.transform_rotation_slider.setRange(-180, 180)
        transform_form.addRow("Rotation", self.transform_rotation_slider)

        reset_transform_btn = QPushButton("Reset Transform")
        reset_transform_btn.clicked.connect(self._reset_transform)
        transform_form.addRow("", reset_transform_btn)

        layout.addWidget(self.transform_group)

        # ---- Reframe group (video/image only) ----
        self.reframe_group = QGroupBox("Reframe (Vertical / Shorts)")
        reframe_form = QFormLayout(self.reframe_group)

        self.fill_frame_checkbox = QCheckBox("Fill Frame (crop instead of letterbox)")
        reframe_form.addRow("", self.fill_frame_checkbox)

        self.crop_focus_x_slider = QSlider(Qt.Horizontal)
        self.crop_focus_x_slider.setRange(0, 100)
        reframe_form.addRow("Crop Focus X", self.crop_focus_x_slider)

        self.crop_focus_y_slider = QSlider(Qt.Horizontal)
        self.crop_focus_y_slider.setRange(0, 100)
        reframe_form.addRow("Crop Focus Y", self.crop_focus_y_slider)

        layout.addWidget(self.reframe_group)

        # ---- Keyframes group (video/image only) ----
        self.keyframes_group = QGroupBox("Keyframes")
        kf_layout = QVBoxLayout(self.keyframes_group)

        kf_layout.addWidget(QLabel("Opacity (2+ keyframes override the Opacity slider above)"))
        self.opacity_kf_table = self._make_keyframe_table(["Time (s)", "Opacity %"])
        kf_layout.addWidget(self.opacity_kf_table)

        opacity_kf_row = QHBoxLayout()
        self.opacity_kf_time_spin = QDoubleSpinBox()
        self.opacity_kf_time_spin.setRange(0.0, 3600.0)
        self.opacity_kf_time_spin.setSuffix(" s")
        opacity_kf_row.addWidget(QLabel("At"))
        opacity_kf_row.addWidget(self.opacity_kf_time_spin)
        add_opacity_kf_btn = QPushButton("+ Add (uses Opacity slider)")
        add_opacity_kf_btn.clicked.connect(self._add_opacity_keyframe)
        remove_opacity_kf_btn = QPushButton("- Remove Selected")
        remove_opacity_kf_btn.clicked.connect(self._remove_selected_opacity_keyframe)
        opacity_kf_row.addWidget(add_opacity_kf_btn)
        opacity_kf_row.addWidget(remove_opacity_kf_btn)
        kf_layout.addLayout(opacity_kf_row)

        kf_layout.addWidget(QLabel("Position (2+ keyframes override Transform Position above)"))
        self.position_kf_table = self._make_keyframe_table(["Time (s)", "X %", "Y %"])
        kf_layout.addWidget(self.position_kf_table)

        position_kf_row = QHBoxLayout()
        self.position_kf_time_spin = QDoubleSpinBox()
        self.position_kf_time_spin.setRange(0.0, 3600.0)
        self.position_kf_time_spin.setSuffix(" s")
        position_kf_row.addWidget(QLabel("At"))
        position_kf_row.addWidget(self.position_kf_time_spin)
        add_position_kf_btn = QPushButton("+ Add (uses Transform Position)")
        add_position_kf_btn.clicked.connect(self._add_position_keyframe)
        remove_position_kf_btn = QPushButton("- Remove Selected")
        remove_position_kf_btn.clicked.connect(self._remove_selected_position_keyframe)
        position_kf_row.addWidget(add_position_kf_btn)
        position_kf_row.addWidget(remove_position_kf_btn)
        kf_layout.addLayout(position_kf_row)

        kf_layout.addWidget(QLabel(
            "Speed Ramp — 2+ keyframes override the Speed field above with step changes "
            "(not a smooth ramp); time is seconds from the clip's SOURCE start, not its "
            "timeline position"
        ))
        self.speed_kf_table = self._make_keyframe_table(["Time (s)", "Speed x"])
        kf_layout.addWidget(self.speed_kf_table)

        speed_kf_row = QHBoxLayout()
        self.speed_kf_time_spin = QDoubleSpinBox()
        self.speed_kf_time_spin.setRange(0.0, 3600.0)
        self.speed_kf_time_spin.setSuffix(" s")
        speed_kf_row.addWidget(QLabel("At"))
        speed_kf_row.addWidget(self.speed_kf_time_spin)
        add_speed_kf_btn = QPushButton("+ Add (uses Speed field)")
        add_speed_kf_btn.clicked.connect(self._add_speed_keyframe)
        remove_speed_kf_btn = QPushButton("- Remove Selected")
        remove_speed_kf_btn.clicked.connect(self._remove_selected_speed_keyframe)
        speed_kf_row.addWidget(add_speed_kf_btn)
        speed_kf_row.addWidget(remove_speed_kf_btn)
        kf_layout.addLayout(speed_kf_row)

        layout.addWidget(self.keyframes_group)

        # ---- Transition group (video/image only) ----
        self.transition_group = QGroupBox("Transition")
        transition_form = QFormLayout(self.transition_group)

        self.crossfade_checkbox = QCheckBox("Crossfade into next clip")
        self.crossfade_checkbox.setToolTip(
            "Blends this clip's tail into the next clip on the same track, "
            "if it immediately follows with no gap."
        )
        transition_form.addRow("", self.crossfade_checkbox)

        self.transition_duration_spin = QDoubleSpinBox()
        self.transition_duration_spin.setRange(0.1, 10.0)
        self.transition_duration_spin.setSingleStep(0.1)
        self.transition_duration_spin.setSuffix(" s")
        transition_form.addRow("Duration", self.transition_duration_spin)

        layout.addWidget(self.transition_group)

        # ---- Color correction group (video/image only) ----
        self.color_group = QGroupBox("Color Correction")
        color_form = QFormLayout(self.color_group)

        self.brightness_slider = QSlider(Qt.Horizontal)
        self.brightness_slider.setRange(-100, 100)
        color_form.addRow("Brightness", self.brightness_slider)

        self.contrast_slider = QSlider(Qt.Horizontal)
        self.contrast_slider.setRange(0, 200)
        color_form.addRow("Contrast", self.contrast_slider)

        self.saturation_slider = QSlider(Qt.Horizontal)
        self.saturation_slider.setRange(0, 300)
        color_form.addRow("Saturation", self.saturation_slider)

        self.gamma_slider = QSlider(Qt.Horizontal)
        self.gamma_slider.setRange(10, 300)
        color_form.addRow("Gamma", self.gamma_slider)

        layout.addWidget(self.color_group)

        # ---- Chroma key group (video/image only) ----
        self.chroma_group = QGroupBox("Chroma Key (Green Screen)")
        chroma_form = QFormLayout(self.chroma_group)

        self.chroma_enabled_checkbox = QCheckBox("Enable chroma key")
        chroma_form.addRow("", self.chroma_enabled_checkbox)

        self.chroma_color_btn = QPushButton("Key Color")
        chroma_form.addRow("Color", self.chroma_color_btn)

        self.chroma_similarity_slider = QSlider(Qt.Horizontal)
        self.chroma_similarity_slider.setRange(0, 100)
        chroma_form.addRow("Similarity", self.chroma_similarity_slider)

        self.chroma_blend_slider = QSlider(Qt.Horizontal)
        self.chroma_blend_slider.setRange(0, 100)
        chroma_form.addRow("Softness", self.chroma_blend_slider)

        layout.addWidget(self.chroma_group)

        # ---- Text group ----
        self.text_group = QGroupBox("Text")
        text_form = QFormLayout(self.text_group)

        self.text_content = _PlainTextEdit()
        self.text_content.setFixedHeight(60)
        text_form.addRow("Content", self.text_content)

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(8, 300)
        text_form.addRow("Font Size", self.font_size_spin)

        self.color_btn = QPushButton("Text Color")
        text_form.addRow("Color", self.color_btn)

        self.bold_checkbox = QCheckBox("Bold")
        self.italic_checkbox = QCheckBox("Italic")
        text_form.addRow("", self.bold_checkbox)
        text_form.addRow("", self.italic_checkbox)

        self.pos_x_slider = QSlider(Qt.Horizontal)
        self.pos_x_slider.setRange(0, 100)
        text_form.addRow("Horizontal Pos.", self.pos_x_slider)

        self.pos_y_slider = QSlider(Qt.Horizontal)
        self.pos_y_slider.setRange(0, 100)
        text_form.addRow("Vertical Pos.", self.pos_y_slider)

        self.alignment_combo = QComboBox()
        self.alignment_combo.addItems(["left", "center", "right"])
        text_form.addRow("Alignment", self.alignment_combo)

        layout.addWidget(self.text_group)
        layout.addStretch(1)

        self._connect_signals()
        self._selected_color = QColor("#FFFFFF")
        self._chroma_color = QColor("#00FF00")

    def _connect_signals(self) -> None:
        self.volume_slider.sliderPressed.connect(lambda: self._remember("volume", self.volume_slider.value() / 100.0))
        self.volume_slider.valueChanged.connect(lambda v: self._apply_live("volume", v / 100.0))
        self.volume_slider.sliderReleased.connect(lambda: self._commit("volume", self.volume_slider.value() / 100.0, "Edit Volume"))

        self.opacity_slider.sliderPressed.connect(lambda: self._remember("opacity", self.opacity_slider.value() / 100.0))
        self.opacity_slider.valueChanged.connect(lambda v: self._apply_live("opacity", v / 100.0))
        self.opacity_slider.sliderReleased.connect(lambda: self._commit("opacity", self.opacity_slider.value() / 100.0, "Edit Opacity"))

        self.mute_checkbox.toggled.connect(lambda checked: self._commit_now("muted", checked, "Toggle Mute"))
        self.speed_spin.editingFinished.connect(lambda: self._commit_now("speed", self.speed_spin.value(), "Edit Speed"))
        self.fade_in_spin.editingFinished.connect(lambda: self._commit_now("fade_in", self.fade_in_spin.value(), "Edit Fade In"))
        self.fade_out_spin.editingFinished.connect(lambda: self._commit_now("fade_out", self.fade_out_spin.value(), "Edit Fade Out"))

        self.transform_x_slider.sliderPressed.connect(lambda: self._remember("transform_x", self.transform_x_slider.value() / 100.0))
        self.transform_x_slider.valueChanged.connect(lambda v: self._apply_live("transform_x", v / 100.0))
        self.transform_x_slider.sliderReleased.connect(lambda: self._commit("transform_x", self.transform_x_slider.value() / 100.0, "Edit Transform Position"))

        self.transform_y_slider.sliderPressed.connect(lambda: self._remember("transform_y", self.transform_y_slider.value() / 100.0))
        self.transform_y_slider.valueChanged.connect(lambda v: self._apply_live("transform_y", v / 100.0))
        self.transform_y_slider.sliderReleased.connect(lambda: self._commit("transform_y", self.transform_y_slider.value() / 100.0, "Edit Transform Position"))

        self.transform_scale_slider.sliderPressed.connect(lambda: self._remember("transform_scale", self.transform_scale_slider.value() / 100.0))
        self.transform_scale_slider.valueChanged.connect(lambda v: self._apply_live("transform_scale", v / 100.0))
        self.transform_scale_slider.sliderReleased.connect(lambda: self._commit("transform_scale", self.transform_scale_slider.value() / 100.0, "Edit Transform Scale"))

        self.transform_rotation_slider.sliderPressed.connect(lambda: self._remember("transform_rotation", float(self.transform_rotation_slider.value())))
        self.transform_rotation_slider.valueChanged.connect(lambda v: self._apply_live("transform_rotation", float(v)))
        self.transform_rotation_slider.sliderReleased.connect(lambda: self._commit("transform_rotation", float(self.transform_rotation_slider.value()), "Edit Transform Rotation"))

        self.fill_frame_checkbox.toggled.connect(self._toggle_fill_frame)

        self.crop_focus_x_slider.sliderPressed.connect(lambda: self._remember("crop_focus_x", self.crop_focus_x_slider.value() / 100.0))
        self.crop_focus_x_slider.valueChanged.connect(lambda v: self._apply_live("crop_focus_x", v / 100.0))
        self.crop_focus_x_slider.sliderReleased.connect(lambda: self._commit("crop_focus_x", self.crop_focus_x_slider.value() / 100.0, "Edit Crop Focus"))

        self.crop_focus_y_slider.sliderPressed.connect(lambda: self._remember("crop_focus_y", self.crop_focus_y_slider.value() / 100.0))
        self.crop_focus_y_slider.valueChanged.connect(lambda v: self._apply_live("crop_focus_y", v / 100.0))
        self.crop_focus_y_slider.sliderReleased.connect(lambda: self._commit("crop_focus_y", self.crop_focus_y_slider.value() / 100.0, "Edit Crop Focus"))

        self.crossfade_checkbox.toggled.connect(self._on_crossfade_toggled)
        self.transition_duration_spin.editingFinished.connect(
            lambda: self._commit_now("transition_duration", self.transition_duration_spin.value(), "Edit Transition Duration")
        )

        self.brightness_slider.sliderPressed.connect(lambda: self._remember("brightness", self.brightness_slider.value() / 100.0))
        self.brightness_slider.valueChanged.connect(lambda v: self._apply_live("brightness", v / 100.0))
        self.brightness_slider.sliderReleased.connect(lambda: self._commit("brightness", self.brightness_slider.value() / 100.0, "Edit Brightness"))

        self.contrast_slider.sliderPressed.connect(lambda: self._remember("contrast", self.contrast_slider.value() / 100.0))
        self.contrast_slider.valueChanged.connect(lambda v: self._apply_live("contrast", v / 100.0))
        self.contrast_slider.sliderReleased.connect(lambda: self._commit("contrast", self.contrast_slider.value() / 100.0, "Edit Contrast"))

        self.saturation_slider.sliderPressed.connect(lambda: self._remember("saturation", self.saturation_slider.value() / 100.0))
        self.saturation_slider.valueChanged.connect(lambda v: self._apply_live("saturation", v / 100.0))
        self.saturation_slider.sliderReleased.connect(lambda: self._commit("saturation", self.saturation_slider.value() / 100.0, "Edit Saturation"))

        self.gamma_slider.sliderPressed.connect(lambda: self._remember("gamma", self.gamma_slider.value() / 100.0))
        self.gamma_slider.valueChanged.connect(lambda v: self._apply_live("gamma", v / 100.0))
        self.gamma_slider.sliderReleased.connect(lambda: self._commit("gamma", self.gamma_slider.value() / 100.0, "Edit Gamma"))

        self.chroma_enabled_checkbox.toggled.connect(lambda checked: self._commit_now("chroma_key_enabled", checked, "Toggle Chroma Key"))
        self.chroma_color_btn.clicked.connect(self._pick_chroma_color)

        self.chroma_similarity_slider.sliderPressed.connect(
            lambda: self._remember("chroma_key_similarity", self.chroma_similarity_slider.value() / 100.0)
        )
        self.chroma_similarity_slider.valueChanged.connect(lambda v: self._apply_live("chroma_key_similarity", v / 100.0))
        self.chroma_similarity_slider.sliderReleased.connect(
            lambda: self._commit("chroma_key_similarity", self.chroma_similarity_slider.value() / 100.0, "Edit Chroma Key Similarity")
        )

        self.chroma_blend_slider.sliderPressed.connect(lambda: self._remember("chroma_key_blend", self.chroma_blend_slider.value() / 100.0))
        self.chroma_blend_slider.valueChanged.connect(lambda v: self._apply_live("chroma_key_blend", v / 100.0))
        self.chroma_blend_slider.sliderReleased.connect(
            lambda: self._commit("chroma_key_blend", self.chroma_blend_slider.value() / 100.0, "Edit Chroma Key Softness")
        )

        self.color_btn.clicked.connect(self._pick_color)
        for widget, signal_name in (
            (self.text_content, "editingFinished"),
            (self.font_size_spin, "editingFinished"),
            (self.bold_checkbox, "toggled"),
            (self.italic_checkbox, "toggled"),
            (self.pos_x_slider, "sliderReleased"),
            (self.pos_y_slider, "sliderReleased"),
            (self.alignment_combo, "currentTextChanged"),
        ):
            getattr(widget, signal_name).connect(self._apply_text_style)

    # ------------------------------------------------------------------
    def set_project(self, project: Project, history: History) -> None:
        self.project = project
        self.history = history

    def show_empty_state(self) -> None:
        self.clip_id = None
        self.empty_label.setVisible(True)
        self.name_label.setVisible(False)
        self.clip_group.setVisible(False)
        self.transform_group.setVisible(False)
        self.reframe_group.setVisible(False)
        self.keyframes_group.setVisible(False)
        self.transition_group.setVisible(False)
        self.color_group.setVisible(False)
        self.chroma_group.setVisible(False)
        self.text_group.setVisible(False)

    def set_selected_clip(self, clip_id: Optional[str]) -> None:
        if self.project is None or clip_id is None:
            self.show_empty_state()
            return
        clip = self.project.find_clip(clip_id)
        if clip is None:
            self.show_empty_state()
            return

        self.clip_id = clip_id
        self._loading = True

        self.empty_label.setVisible(False)
        self.name_label.setVisible(True)
        self.name_label.setText(f"{clip.name}  ·  {MEDIA_TYPE_LABEL.get(clip.media_type, 'Clip')}")

        is_text = clip.media_type == MediaType.TEXT
        is_visual = clip.media_type in (MediaType.VIDEO, MediaType.IMAGE)
        self.text_group.setVisible(is_text)
        self.clip_group.setVisible(not is_text)
        self.transform_group.setVisible(is_visual)
        self.reframe_group.setVisible(is_visual)
        self.keyframes_group.setVisible(is_visual)
        self.transition_group.setVisible(is_visual)
        self.color_group.setVisible(is_visual)
        self.chroma_group.setVisible(is_visual)

        self.volume_slider.setValue(int(clip.volume * 100))
        self.mute_checkbox.setChecked(clip.muted)
        self.speed_spin.setValue(clip.speed)
        self.opacity_slider.setValue(int(clip.opacity * 100))
        self.fade_in_spin.setValue(clip.fade_in)
        self.fade_out_spin.setValue(clip.fade_out)

        if is_visual:
            self.transform_x_slider.setValue(int(clip.transform_x * 100))
            self.transform_y_slider.setValue(int(clip.transform_y * 100))
            self.transform_scale_slider.setValue(int(clip.transform_scale * 100))
            self.transform_rotation_slider.setValue(int(clip.transform_rotation))

            is_fill = clip.reframe_mode == ReframeMode.FILL
            self.fill_frame_checkbox.setChecked(is_fill)
            self.crop_focus_x_slider.setValue(int(clip.crop_focus_x * 100))
            self.crop_focus_y_slider.setValue(int(clip.crop_focus_y * 100))
            self.crop_focus_x_slider.setEnabled(is_fill)
            self.crop_focus_y_slider.setEnabled(is_fill)

            self._refresh_opacity_keyframe_table(clip.opacity_keyframes)
            self._refresh_position_keyframe_table(clip.position_keyframes)
            self._refresh_speed_keyframe_table(clip.speed_keyframes)

            self.crossfade_checkbox.setChecked(clip.transition_out == TransitionType.CROSSFADE)
            self.transition_duration_spin.setValue(clip.transition_duration)
            self.transition_duration_spin.setEnabled(clip.transition_out == TransitionType.CROSSFADE)

            self.brightness_slider.setValue(int(clip.brightness * 100))
            self.contrast_slider.setValue(int(clip.contrast * 100))
            self.saturation_slider.setValue(int(clip.saturation * 100))
            self.gamma_slider.setValue(int(clip.gamma * 100))

            self.chroma_enabled_checkbox.setChecked(clip.chroma_key_enabled)
            self._chroma_color = QColor(clip.chroma_key_color)
            self.chroma_similarity_slider.setValue(int(clip.chroma_key_similarity * 100))
            self.chroma_blend_slider.setValue(int(clip.chroma_key_blend * 100))

        if is_text and clip.text_style is not None:
            style = clip.text_style
            self.text_content.blockSignals(True)
            self.text_content.setPlainText(style.content)
            self.text_content.blockSignals(False)
            self.font_size_spin.setValue(style.font_size)
            self.bold_checkbox.setChecked(style.bold)
            self.italic_checkbox.setChecked(style.italic)
            self.pos_x_slider.setValue(int(style.position_x * 100))
            self.pos_y_slider.setValue(int(style.position_y * 100))
            idx = self.alignment_combo.findText(style.alignment)
            if idx >= 0:
                self.alignment_combo.setCurrentIndex(idx)
            self._selected_color = QColor(style.color)

        self._loading = False

    # ------------------------------------------------------------------
    def _current_clip(self) -> Optional[Clip]:
        if self.project is None or self.clip_id is None:
            return None
        return self.project.find_clip(self.clip_id)

    def _remember(self, attribute: str, _value) -> None:
        clip = self._current_clip()
        self._pre_drag_value = getattr(clip, attribute, None) if clip else None

    def _apply_live(self, attribute: str, value) -> None:
        if self._loading:
            return
        clip = self._current_clip()
        if clip is not None:
            setattr(clip, attribute, value)
            self.project_modified.emit()

    def _commit(self, attribute: str, value, label: str) -> None:
        if self._loading or self.history is None or self.project is None or self.clip_id is None:
            return
        clip = self._current_clip()
        if clip is None:
            return
        # Value already applied live; push a command that records the
        # before/after so undo restores the pre-drag value correctly.
        setattr(clip, attribute, self._pre_drag_value if self._pre_drag_value is not None else value)
        self.history.execute(UpdateClipPropertyCommand(self.project, self.clip_id, attribute, value, label))
        self.project_modified.emit()

    def _commit_now(self, attribute: str, value, label: str) -> None:
        if self._loading or self.history is None or self.project is None or self.clip_id is None:
            return
        self.history.execute(UpdateClipPropertyCommand(self.project, self.clip_id, attribute, value, label))
        self.project_modified.emit()

    def _toggle_fill_frame(self, checked: bool) -> None:
        mode = ReframeMode.FILL if checked else ReframeMode.FIT
        self._commit_now("reframe_mode", mode, "Toggle Fill Frame")
        self.crop_focus_x_slider.setEnabled(checked)
        self.crop_focus_y_slider.setEnabled(checked)

    def _reset_transform(self) -> None:
        for attribute, value, slider in (
            ("transform_x", 0.5, self.transform_x_slider),
            ("transform_y", 0.5, self.transform_y_slider),
            ("transform_scale", 1.0, self.transform_scale_slider),
            ("transform_rotation", 0.0, self.transform_rotation_slider),
        ):
            self._commit_now(attribute, value, "Reset Transform")
        self._loading = True
        self.transform_x_slider.setValue(50)
        self.transform_y_slider.setValue(50)
        self.transform_scale_slider.setValue(100)
        self.transform_rotation_slider.setValue(0)
        self._loading = False

    # ------------------------------------------------------------------
    # Keyframes
    # ------------------------------------------------------------------
    def _refresh_opacity_keyframe_table(self, keyframes) -> None:
        table = self.opacity_kf_table
        table.setRowCount(0)
        for t, value in sorted(keyframes):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(f"{t:.2f}"))
            table.setItem(row, 1, QTableWidgetItem(f"{value * 100:.0f}"))

    def _refresh_position_keyframe_table(self, keyframes) -> None:
        table = self.position_kf_table
        table.setRowCount(0)
        for t, x, y in sorted(keyframes):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(f"{t:.2f}"))
            table.setItem(row, 1, QTableWidgetItem(f"{x * 100:.0f}"))
            table.setItem(row, 2, QTableWidgetItem(f"{y * 100:.0f}"))

    def _add_opacity_keyframe(self) -> None:
        clip = self._current_clip()
        if clip is None:
            return
        t = self.opacity_kf_time_spin.value()
        value = self.opacity_slider.value() / 100.0
        keyframes = [kf for kf in clip.opacity_keyframes if abs(kf[0] - t) > 1e-6]
        keyframes.append((t, value))
        keyframes.sort()
        self._commit_now("opacity_keyframes", keyframes, "Add Opacity Keyframe")
        self._refresh_opacity_keyframe_table(keyframes)

    def _remove_selected_opacity_keyframe(self) -> None:
        clip = self._current_clip()
        if clip is None:
            return
        row = self.opacity_kf_table.currentRow()
        if row < 0:
            return
        keyframes = sorted(clip.opacity_keyframes)
        if row < len(keyframes):
            del keyframes[row]
        self._commit_now("opacity_keyframes", keyframes, "Remove Opacity Keyframe")
        self._refresh_opacity_keyframe_table(keyframes)

    def _add_position_keyframe(self) -> None:
        clip = self._current_clip()
        if clip is None:
            return
        t = self.position_kf_time_spin.value()
        x = self.transform_x_slider.value() / 100.0
        y = self.transform_y_slider.value() / 100.0
        keyframes = [kf for kf in clip.position_keyframes if abs(kf[0] - t) > 1e-6]
        keyframes.append((t, x, y))
        keyframes.sort()
        self._commit_now("position_keyframes", keyframes, "Add Position Keyframe")
        self._refresh_position_keyframe_table(keyframes)

    def _remove_selected_position_keyframe(self) -> None:
        clip = self._current_clip()
        if clip is None:
            return
        row = self.position_kf_table.currentRow()
        if row < 0:
            return
        keyframes = sorted(clip.position_keyframes)
        if row < len(keyframes):
            del keyframes[row]
        self._commit_now("position_keyframes", keyframes, "Remove Position Keyframe")
        self._refresh_position_keyframe_table(keyframes)

    def _refresh_speed_keyframe_table(self, keyframes) -> None:
        table = self.speed_kf_table
        table.setRowCount(0)
        for t, speed in sorted(keyframes):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(f"{t:.2f}"))
            table.setItem(row, 1, QTableWidgetItem(f"{speed:.2f}"))

    def _add_speed_keyframe(self) -> None:
        clip = self._current_clip()
        if clip is None:
            return
        t = self.speed_kf_time_spin.value()
        speed = self.speed_spin.value()
        keyframes = [kf for kf in clip.speed_keyframes if abs(kf[0] - t) > 1e-6]
        keyframes.append((t, speed))
        keyframes.sort()
        self._commit_now("speed_keyframes", keyframes, "Add Speed Keyframe")
        self._refresh_speed_keyframe_table(keyframes)

    def _remove_selected_speed_keyframe(self) -> None:
        clip = self._current_clip()
        if clip is None:
            return
        row = self.speed_kf_table.currentRow()
        if row < 0:
            return
        keyframes = sorted(clip.speed_keyframes)
        if row < len(keyframes):
            del keyframes[row]
        self._commit_now("speed_keyframes", keyframes, "Remove Speed Keyframe")
        self._refresh_speed_keyframe_table(keyframes)

    def _on_crossfade_toggled(self, checked: bool) -> None:
        self.transition_duration_spin.setEnabled(checked)
        new_value = TransitionType.CROSSFADE if checked else TransitionType.NONE
        self._commit_now("transition_out", new_value, "Toggle Crossfade")

    def _pick_chroma_color(self) -> None:
        color = QColorDialog.getColor(self._chroma_color, self, "Chroma Key Color")
        if color.isValid():
            self._chroma_color = color
            self._commit_now("chroma_key_color", color.name(), "Edit Chroma Key Color")

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(self._selected_color, self, "Text Color")
        if color.isValid():
            self._selected_color = color
            self._apply_text_style()

    def _apply_text_style(self, *_args) -> None:
        if self._loading or self.history is None or self.project is None or self.clip_id is None:
            return
        clip = self._current_clip()
        if clip is None or clip.media_type != MediaType.TEXT:
            return
        new_style = TextStyle(
            content=self.text_content.toPlainText(),
            font_family=clip.text_style.font_family if clip.text_style else "Arial",
            font_size=self.font_size_spin.value(),
            color=self._selected_color.name(),
            outline_color=clip.text_style.outline_color if clip.text_style else "#000000",
            outline_width=clip.text_style.outline_width if clip.text_style else 2,
            bold=self.bold_checkbox.isChecked(),
            italic=self.italic_checkbox.isChecked(),
            position_x=self.pos_x_slider.value() / 100.0,
            position_y=self.pos_y_slider.value() / 100.0,
            alignment=self.alignment_combo.currentText(),
        )
        self.history.execute(UpdateTextClipCommand(self.project, self.clip_id, new_style))
        self.name_label.setText(f"{new_style.content[:24] or 'Text'}  ·  Text Clip")
        self.project_modified.emit()
