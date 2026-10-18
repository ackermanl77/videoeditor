"""The right-hand inspector: edit properties of the selected clip."""

from __future__ import annotations

import copy
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFontComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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
from core.commands import UpdateClipPropertyCommand, UpdateTextClipCommand, property_change_overlaps
from core.effects import LEGACY_FIELDS, REGISTRY, EffectInstance
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


class CollapsibleGroupBox(QGroupBox):
    """A ``QGroupBox`` whose content can be collapsed to just its title bar.

    Plain ``QGroupBox.setCheckable()`` only disables its children when
    unchecked — they stay visible at full size, still taking up the exact
    same vertical space. That doesn't help: with every group always fully
    expanded, selecting a video/image clip renders ~1,300px of stacked
    controls (Clip Adjustments through Chroma Key) against a Properties
    panel that typically only has ~500px of actual height, forcing several
    screens' worth of scrolling just to reach the last group. This
    subclass instead makes the checkbox an explicit expand/collapse
    control: unchecking it ``setVisible(False)``s every direct child
    widget, shrinking the group to just its title bar. The checkbox stays
    visible (rather than styling it away) so there's always a guaranteed,
    native, always-clickable hit target — a "▼"/"▶" prefix on the title
    text is the second, clearer visual cue that this is about
    showing/hiding the section, not enabling/disabling a feature (a real
    ambiguity risk for e.g. "Chroma Key," which already has its own
    internal "Enable" checkbox).
    """

    def __init__(self, title: str, parent: Optional[QWidget] = None, start_expanded: bool = True):
        super().__init__(parent)
        self._base_title = title
        self.setCheckable(True)
        self.toggled.connect(self._apply_expanded_state)
        self.setChecked(start_expanded)
        self._apply_expanded_state(start_expanded)

    def _apply_expanded_state(self, expanded: bool) -> None:
        self.setTitle(f"{'▼' if expanded else '▶'} {self._base_title}")
        for child in self.findChildren(QWidget):
            if child.parentWidget() is self:
                child.setVisible(expanded)


MEDIA_TYPE_LABEL = {
    MediaType.VIDEO: "Video Clip",
    MediaType.AUDIO: "Audio Clip",
    MediaType.IMAGE: "Image Clip",
    MediaType.TEXT: "Text Clip",
}

# Every transition supported by build_filter_graph's xfade path (see
# core/filter_graph.py::_XFADE_TRANSITION_NAMES), in the order offered by
# the combo box. NONE is first ("no transition"); CROSSFADE is the original,
# simplest option, followed by the newer wipe/slide/circle/dissolve variety.
TRANSITION_LABELS = [
    (TransitionType.NONE, "None"),
    (TransitionType.CROSSFADE, "Crossfade"),
    (TransitionType.WIPE_LEFT, "Wipe Left"),
    (TransitionType.WIPE_RIGHT, "Wipe Right"),
    (TransitionType.WIPE_UP, "Wipe Up"),
    (TransitionType.WIPE_DOWN, "Wipe Down"),
    (TransitionType.SLIDE_LEFT, "Slide Left"),
    (TransitionType.SLIDE_RIGHT, "Slide Right"),
    (TransitionType.SLIDE_UP, "Slide Up"),
    (TransitionType.SLIDE_DOWN, "Slide Down"),
    (TransitionType.CIRCLE_CROP, "Circle Crop"),
    (TransitionType.DISSOLVE, "Dissolve"),
]


class PropertiesPanel(QWidget):
    """Displays and edits the properties of the currently selected clip."""

    project_modified = Signal()
    # A property of this clip changed (so the timeline can refresh its item:
    # width after a speed change, effect badges, ...).
    clip_changed = Signal(str)
    # One-line user feedback, e.g. why an edit was refused (-> status bar).
    status_message = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project: Optional[Project] = None
        self.history: Optional[History] = None
        self.clip_id: Optional[str] = None
        self._loading = False
        # Timeline playhead, for the "key at playhead" buttons (F12).
        self.playhead_time = 0.0

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
        self.clip_group = CollapsibleGroupBox("Clip Adjustments", start_expanded=True)
        clip_form = QFormLayout(self.clip_group)

        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 200)
        clip_form.addRow("Volume", self._with_key_button(self.volume_slider, "volume"))

        volume_keys_row = QHBoxLayout()
        self.volume_keys_label = QLabel("No volume keyframes")
        self.volume_keys_label.setWordWrap(True)
        self.volume_keys_label.setStyleSheet("color: #9aa1ad;")
        clear_volume_keys_btn = QPushButton("Clear")
        clear_volume_keys_btn.setToolTip("Remove every volume keyframe (back to the Volume slider)")
        clear_volume_keys_btn.clicked.connect(lambda: self._clear_keyframes("volume_keyframes", "Clear Volume Keyframes"))
        volume_keys_row.addWidget(self.volume_keys_label, 1)
        volume_keys_row.addWidget(clear_volume_keys_btn)
        clip_form.addRow("Volume Keys", volume_keys_row)

        self.mute_checkbox = QCheckBox("Mute audio")
        clip_form.addRow("", self.mute_checkbox)

        self.denoise_checkbox = QCheckBox("Reduce noise (afftdn)")
        self.denoise_checkbox.setToolTip(
            "Applies FFmpeg's afftdn audio noise-reduction filter to this clip's audio."
        )
        clip_form.addRow("", self.denoise_checkbox)

        self.denoise_strength_slider = QSlider(Qt.Horizontal)
        self.denoise_strength_slider.setRange(1, 97)  # afftdn's nr option range x1 (whole dB)
        clip_form.addRow("Denoise Strength", self.denoise_strength_slider)

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.25, 4.0)
        self.speed_spin.setSingleStep(0.05)
        self.speed_spin.setSuffix(" x")
        clip_form.addRow("Speed", self._with_key_button(self.speed_spin, "speed"))

        self.reverse_checkbox = QCheckBox("Reverse playback")
        self.reverse_checkbox.setToolTip(
            "Play the clip back to front (its sound too). FFmpeg buffers the whole clip to reverse it, "
            "so keep reversed clips short."
        )
        clip_form.addRow("", self.reverse_checkbox)

        self.freeze_spin = QDoubleSpinBox()
        self.freeze_spin.setRange(0.0, 30.0)
        self.freeze_spin.setSingleStep(0.1)
        self.freeze_spin.setSuffix(" s")
        self.freeze_spin.setToolTip(
            "Extend the clip by holding its last displayed frame for this long (silent). "
            "Dragging the clip's right edge in shortens the freeze first."
        )
        clip_form.addRow("Freeze Frame", self.freeze_spin)

        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(0, 100)
        clip_form.addRow("Opacity", self._with_key_button(self.opacity_slider, "opacity"))

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
        self.transform_group = CollapsibleGroupBox("Transform", start_expanded=True)
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
        key_position_btn = QPushButton("\u25c6 Key Position at Playhead")
        key_position_btn.setToolTip("Add/update a position keyframe at the playhead from the X/Y sliders")
        key_position_btn.clicked.connect(lambda: self.key_at_playhead("position"))
        transform_row = QHBoxLayout()
        transform_row.addWidget(reset_transform_btn)
        transform_row.addWidget(key_position_btn)
        transform_form.addRow("", transform_row)

        layout.addWidget(self.transform_group)

        # ---- Reframe group (video/image only) ----
        self.reframe_group = CollapsibleGroupBox("Reframe (Vertical / Shorts)", start_expanded=False)
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
        self.keyframes_group = CollapsibleGroupBox("Keyframes", start_expanded=False)
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
        self.transition_group = CollapsibleGroupBox("Transition", start_expanded=False)
        transition_form = QFormLayout(self.transition_group)

        self.transition_combo = QComboBox()
        self.transition_combo.setToolTip(
            "Blends this clip's tail into the next clip on the same track, "
            "if it immediately follows with no gap."
        )
        for value, label in TRANSITION_LABELS:
            self.transition_combo.addItem(label, value.value)
        transition_form.addRow("Transition Out", self.transition_combo)

        self.transition_duration_spin = QDoubleSpinBox()
        self.transition_duration_spin.setRange(0.1, 10.0)
        self.transition_duration_spin.setSingleStep(0.1)
        self.transition_duration_spin.setSuffix(" s")
        transition_form.addRow("Duration", self.transition_duration_spin)

        layout.addWidget(self.transition_group)

        # ---- Color correction group (video/image only) ----
        self.color_group = CollapsibleGroupBox("Color Correction", start_expanded=True)
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
        self.chroma_group = CollapsibleGroupBox("Chroma Key (Green Screen)", start_expanded=False)
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

        # ---- Effects group (video/image only): the generic effect stack (B7) ----
        self.effects_group = CollapsibleGroupBox("Effects (Color, Keys, LUT, Stabilize...)", start_expanded=False)
        effects_layout = QVBoxLayout(self.effects_group)
        add_row = QHBoxLayout()
        self.effect_kind_combo = QComboBox()
        for kind, spec in REGISTRY.items():
            self.effect_kind_combo.addItem(spec.label, kind)
            self.effect_kind_combo.setItemData(self.effect_kind_combo.count() - 1, spec.description, Qt.ToolTipRole)
        add_effect_btn = QPushButton("+ Add")
        add_effect_btn.clicked.connect(self._add_effect)
        add_row.addWidget(self.effect_kind_combo, 1)
        add_row.addWidget(add_effect_btn)
        effects_layout.addLayout(add_row)
        self.effects_list = QListWidget()
        self.effects_list.setMaximumHeight(96)
        self.effects_list.currentRowChanged.connect(self._on_effect_row_changed)
        effects_layout.addWidget(self.effects_list)
        effect_buttons = QHBoxLayout()
        self.effect_enabled_checkbox = QCheckBox("Enabled")
        self.effect_enabled_checkbox.toggled.connect(self._on_effect_enabled_toggled)
        for text, slot in (("Up", lambda: self._move_effect(-1)), ("Down", lambda: self._move_effect(1)),
                           ("Remove", self._remove_effect)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            effect_buttons.addWidget(button)
        effect_buttons.addWidget(self.effect_enabled_checkbox)
        effects_layout.addLayout(effect_buttons)
        self.effect_params_widget = QWidget()
        self.effect_params_form = QFormLayout(self.effect_params_widget)
        self.effect_params_form.setContentsMargins(0, 0, 0, 0)
        effects_layout.addWidget(self.effect_params_widget)
        layout.addWidget(self.effects_group)

        # ---- Text group ----
        self.text_group = QGroupBox("Text")
        text_form = QFormLayout(self.text_group)

        # A styled subtitle line (imported from an ASS/SSA script with its
        # styling): libass renders it from the script's own style, so the
        # look controls below only affect this editor's preview.
        self.styled_note = QLabel(
            "Styled subtitle line: the export draws it with the subtitle file's own style "
            "(font, colour, outline, position). The settings below only change this editor's preview. "
            "Editing the text keeps the style but drops the line's inline effects."
        )
        self.styled_note.setWordWrap(True)
        self.styled_note.setVisible(False)
        text_form.addRow(self.styled_note)

        self.text_content = _PlainTextEdit()
        self.text_content.setFixedHeight(60)
        text_form.addRow("Content", self.text_content)

        # Title styles (F23): built-in looks and the user's saved ones (core/title_templates.py).
        style_row = QHBoxLayout()
        self.title_style_combo = QComboBox()
        self.title_style_combo.setAccessibleName("Title style")
        style_row.addWidget(self.title_style_combo, 1)
        self.apply_title_style_btn = QPushButton("Apply")
        self.apply_title_style_btn.setToolTip("Give this text clip the chosen style (its text is kept).")
        self.apply_title_style_btn.clicked.connect(lambda: self.apply_title_style(self.title_style_combo.currentText()))
        style_row.addWidget(self.apply_title_style_btn)
        self.save_title_style_btn = QPushButton("Save…")
        self.save_title_style_btn.setToolTip("Save this clip's look as a style for other text clips and projects.")
        self.save_title_style_btn.clicked.connect(lambda: self.save_title_style())
        style_row.addWidget(self.save_title_style_btn)
        self.delete_title_style_btn = QPushButton("Delete")
        self.delete_title_style_btn.setToolTip("Delete the chosen saved style (built-in styles can't be deleted).")
        self.delete_title_style_btn.clicked.connect(lambda: self.delete_title_style(self.title_style_combo.currentText()))
        style_row.addWidget(self.delete_title_style_btn)
        text_form.addRow("Style", style_row)
        self.refresh_title_styles()

        self.font_combo = QFontComboBox()
        self.font_combo.setToolTip("Font family used in the export (resolved to the installed font file)")
        text_form.addRow("Font", self.font_combo)

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(8, 300)
        self.font_size_spin.setToolTip("Pixels at 1080p; scaled with the export resolution")
        text_form.addRow("Font Size", self.font_size_spin)

        self.color_btn = QPushButton("Text Color")
        text_form.addRow("Color", self.color_btn)

        outline_row = QHBoxLayout()
        self.outline_width_spin = QSpinBox()
        self.outline_width_spin.setRange(0, 40)
        self.outline_color_btn = QPushButton("Outline Color")
        outline_row.addWidget(self.outline_width_spin)
        outline_row.addWidget(self.outline_color_btn)
        text_form.addRow("Outline", outline_row)

        shadow_row = QHBoxLayout()
        self.shadow_checkbox = QCheckBox("Shadow")
        self.shadow_offset_spin = QSpinBox()
        self.shadow_offset_spin.setRange(0, 60)
        self.shadow_color_btn = QPushButton("Shadow Color")
        shadow_row.addWidget(self.shadow_checkbox)
        shadow_row.addWidget(self.shadow_offset_spin)
        shadow_row.addWidget(self.shadow_color_btn)
        text_form.addRow("", shadow_row)

        box_row = QHBoxLayout()
        self.box_checkbox = QCheckBox("Background box")
        self.box_opacity_spin = QSpinBox()
        self.box_opacity_spin.setRange(0, 100)
        self.box_opacity_spin.setSuffix(" %")
        self.box_color_btn = QPushButton("Box Color")
        box_row.addWidget(self.box_checkbox)
        box_row.addWidget(self.box_opacity_spin)
        box_row.addWidget(self.box_color_btn)
        text_form.addRow("", box_row)

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
        self.alignment_combo.setToolTip("How the lines of a multi-line text line up with each other")
        text_form.addRow("Alignment", self.alignment_combo)

        layout.addWidget(self.text_group)
        layout.addStretch(1)

        # Each collapsible group's initial expand/collapse state was applied
        # in its own __init__, before this method had added any of its rows
        # -- hiding "the current children" did nothing, since there were none
        # yet. Re-apply it now that every group is fully populated, so a
        # group that defaults to collapsed actually starts collapsed.
        for group in (
            self.clip_group, self.transform_group, self.reframe_group,
            self.keyframes_group, self.transition_group, self.color_group,
            self.chroma_group, self.effects_group,
        ):
            group._apply_expanded_state(group.isChecked())

        self._selected_color = QColor("#FFFFFF")
        self._outline_color = QColor("#000000")
        self._shadow_color = QColor("#000000")
        self._box_color = QColor("#000000")
        self._chroma_color = QColor("#00FF00")
        self._connect_signals()

    def _connect_signals(self) -> None:
        # Every live control executes a *mergeable* command on each
        # valueChanged (see _edit_live), whatever produced it: mouse drag,
        # groove click, wheel, arrow/PageUp keys. Previously only a real
        # drag (sliderPressed/sliderReleased) was recorded, so keyboard and
        # wheel edits changed the clip without an undo step.
        live_sliders = (
            (self.volume_slider, "volume", 100.0, "Edit Volume"),
            (self.opacity_slider, "opacity", 100.0, "Edit Opacity"),
            (self.denoise_strength_slider, "denoise_strength", 1.0, "Edit Denoise Strength"),
            (self.transform_x_slider, "transform_x", 100.0, "Edit Transform Position"),
            (self.transform_y_slider, "transform_y", 100.0, "Edit Transform Position"),
            (self.transform_scale_slider, "transform_scale", 100.0, "Edit Transform Scale"),
            (self.transform_rotation_slider, "transform_rotation", 1.0, "Edit Transform Rotation"),
            (self.crop_focus_x_slider, "crop_focus_x", 100.0, "Edit Crop Focus"),
            (self.crop_focus_y_slider, "crop_focus_y", 100.0, "Edit Crop Focus"),
            (self.brightness_slider, "brightness", 100.0, "Edit Brightness"),
            (self.contrast_slider, "contrast", 100.0, "Edit Contrast"),
            (self.saturation_slider, "saturation", 100.0, "Edit Saturation"),
            (self.gamma_slider, "gamma", 100.0, "Edit Gamma"),
            (self.chroma_similarity_slider, "chroma_key_similarity", 100.0, "Edit Chroma Key Similarity"),
            (self.chroma_blend_slider, "chroma_key_blend", 100.0, "Edit Chroma Key Softness"),
        )
        for slider, attribute, scale, label in live_sliders:
            slider.valueChanged.connect(
                lambda v, a=attribute, sc=scale, lb=label: self._edit_live(a, v / sc, lb)
            )
            # A released drag is one finished gesture: the next edit of the
            # same property is a new undo step even within the merge window.
            slider.sliderReleased.connect(self._end_gesture)

        self.mute_checkbox.toggled.connect(lambda checked: self._commit_now("muted", checked, "Toggle Mute"))
        self.denoise_checkbox.toggled.connect(lambda checked: self._commit_now("denoise_enabled", checked, "Toggle Noise Reduction"))
        self.reverse_checkbox.toggled.connect(lambda checked: self._commit_now("reverse", checked, "Toggle Reverse"))

        self.speed_spin.editingFinished.connect(self._commit_speed)
        self.freeze_spin.editingFinished.connect(self._commit_freeze)
        self.fade_in_spin.editingFinished.connect(lambda: self._commit_now("fade_in", self.fade_in_spin.value(), "Edit Fade In"))
        self.fade_out_spin.editingFinished.connect(lambda: self._commit_now("fade_out", self.fade_out_spin.value(), "Edit Fade Out"))

        self.fill_frame_checkbox.toggled.connect(self._toggle_fill_frame)

        self.transition_combo.currentIndexChanged.connect(self._on_transition_changed)
        self.transition_duration_spin.editingFinished.connect(
            lambda: self._commit_now("transition_duration", self.transition_duration_spin.value(), "Edit Transition Duration")
        )

        self.chroma_enabled_checkbox.toggled.connect(lambda checked: self._commit_now("chroma_key_enabled", checked, "Toggle Chroma Key"))
        self.chroma_color_btn.clicked.connect(self._pick_chroma_color)

        self.color_btn.clicked.connect(self._pick_color)
        self.outline_color_btn.clicked.connect(lambda: self._pick_text_color("_outline_color", "Outline Color"))
        self.shadow_color_btn.clicked.connect(lambda: self._pick_text_color("_shadow_color", "Shadow Color"))
        self.box_color_btn.clicked.connect(lambda: self._pick_text_color("_box_color", "Box Color"))
        for widget, signal_name in (
            (self.text_content, "editingFinished"),
            (self.font_size_spin, "editingFinished"),
            (self.outline_width_spin, "editingFinished"),
            (self.shadow_offset_spin, "editingFinished"),
            (self.box_opacity_spin, "editingFinished"),
            (self.font_combo, "currentFontChanged"),
            (self.bold_checkbox, "toggled"),
            (self.italic_checkbox, "toggled"),
            (self.shadow_checkbox, "toggled"),
            (self.box_checkbox, "toggled"),
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
        self.effects_group.setVisible(False)
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
        self._end_gesture()  # an edit of another clip never merges with this one
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
        self.effects_group.setVisible(is_visual)

        self.volume_slider.setValue(int(clip.volume * 100))
        self.mute_checkbox.setChecked(clip.muted)
        self.denoise_checkbox.setChecked(clip.denoise_enabled)
        self.denoise_strength_slider.setValue(int(clip.denoise_strength))
        self.speed_spin.setValue(clip.speed)
        self.reverse_checkbox.setChecked(clip.reverse)
        self.freeze_spin.setValue(clip.freeze_frame_duration)
        # Reverse needs footage or sound; a freeze frame needs a picture to hold.
        self.reverse_checkbox.setEnabled(clip.media_type in (MediaType.VIDEO, MediaType.AUDIO) or clip.reverse)
        self.freeze_spin.setEnabled(clip.media_type == MediaType.VIDEO or clip.freeze_frame_duration > 0)
        self.opacity_slider.setValue(int(clip.opacity * 100))
        self.fade_in_spin.setValue(clip.fade_in)
        self.fade_out_spin.setValue(clip.fade_out)
        self._refresh_volume_keys_label(clip)

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

            combo_idx = self.transition_combo.findData(clip.transition_out.value)
            self.transition_combo.setCurrentIndex(combo_idx if combo_idx >= 0 else 0)
            self.transition_duration_spin.setValue(clip.transition_duration)
            self.transition_duration_spin.setEnabled(clip.transition_out != TransitionType.NONE)

            self.brightness_slider.setValue(int(clip.brightness * 100))
            self.contrast_slider.setValue(int(clip.contrast * 100))
            self.saturation_slider.setValue(int(clip.saturation * 100))
            self.gamma_slider.setValue(int(clip.gamma * 100))

            self.chroma_enabled_checkbox.setChecked(clip.chroma_key_enabled)
            self._chroma_color = QColor(clip.chroma_key_color)
            self.chroma_similarity_slider.setValue(int(clip.chroma_key_similarity * 100))
            self.chroma_blend_slider.setValue(int(clip.chroma_key_blend * 100))
            self._refresh_effects_list(clip)

        self.styled_note.setVisible(is_text and clip.ass_event is not None)
        if is_text and clip.text_style is not None:
            style = clip.text_style
            self.text_content.blockSignals(True)
            self.text_content.setPlainText(style.content)
            self.text_content.blockSignals(False)
            self.font_size_spin.setValue(style.font_size)
            self.font_combo.setCurrentFont(QFont(style.font_family))
            self.outline_width_spin.setValue(style.outline_width)
            self._outline_color = QColor(style.outline_color)
            self.shadow_checkbox.setChecked(style.shadow_enabled)
            self.shadow_offset_spin.setValue(style.shadow_offset)
            self._shadow_color = QColor(style.shadow_color)
            self.box_checkbox.setChecked(style.box_enabled)
            self.box_opacity_spin.setValue(int(round(style.box_opacity * 100)))
            self._box_color = QColor(style.box_color)
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

    def _end_gesture(self) -> None:
        if self.history is not None:
            self.history.end_merge()

    def _refuse_if_overlapping(self, attribute: str, value) -> bool:
        """True (and a status message) if the edit would run the clip into its neighbour."""
        if self.project is None or self.clip_id is None:
            return False
        if property_change_overlaps(self.project, self.clip_id, attribute, value):
            self.status_message.emit(
                "Not applied: the clip would become longer and overlap the next clip on its track. "
                "Make room first."
            )
            return True
        return False

    def _execute(self, attribute: str, value, label: str, mergeable: bool) -> bool:
        if self._loading or self.history is None or self.project is None or self.clip_id is None:
            return False
        clip = self._current_clip()
        if clip is None or getattr(clip, attribute, None) == value:
            return False
        if self._refuse_if_overlapping(attribute, value):
            return False
        kinds_before = [e.kind for e in clip.effects]
        self.history.execute(
            UpdateClipPropertyCommand(self.project, self.clip_id, attribute, value, label, mergeable=mergeable)
        )
        if attribute in LEGACY_FIELDS and [e.kind for e in clip.effects] != kinds_before:
            # The Color Correction / Chroma Key groups edit effects in the
            # stack; the first edit adds one, so show it in the Effects list.
            self._refresh_effects_list(clip)
        self.clip_changed.emit(self.clip_id)
        self.project_modified.emit()
        return True

    def _edit_live(self, attribute: str, value, label: str) -> None:
        """A continuous control moved: one mergeable, undoable command per change."""
        self._execute(attribute, value, label, mergeable=True)

    def _commit_now(self, attribute: str, value, label: str) -> bool:
        """A discrete edit (checkbox, combo, spin box, button): its own undo step."""
        self._end_gesture()
        return self._execute(attribute, value, label, mergeable=False)

    def _commit_speed(self) -> None:
        if not self._commit_now("speed", self.speed_spin.value(), "Edit Speed"):
            clip = self._current_clip()
            if clip is not None and abs(clip.speed - self.speed_spin.value()) > 1e-9:
                self._loading = True
                self.speed_spin.setValue(clip.speed)  # refused: show the real value again
                self._loading = False

    def _commit_freeze(self) -> None:
        """Freeze Frame spin box: refused (and reset) when the longer clip would overlap the next one."""
        if not self._commit_now("freeze_frame_duration", self.freeze_spin.value(), "Edit Freeze Frame"):
            clip = self._current_clip()
            if clip is not None and abs(clip.freeze_frame_duration - self.freeze_spin.value()) > 1e-9:
                self._loading = True
                self.freeze_spin.setValue(clip.freeze_frame_duration)
                self._loading = False

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
    # Key at playhead (F12)
    # ------------------------------------------------------------------
    def set_playhead_time(self, t: float) -> None:
        self.playhead_time = max(0.0, t)

    def _with_key_button(self, control: QWidget, kind: str) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(control, 1)
        button = QPushButton("\u25c6")
        button.setFixedWidth(28)
        button.setToolTip(f"Add/update a {kind} keyframe at the playhead from the current value")
        button.setAccessibleName(f"Key {kind} at playhead")
        button.clicked.connect(lambda: self.key_at_playhead(kind))
        row.addWidget(button)
        setattr(self, f"key_{kind}_btn", button)
        return holder

    def key_at_playhead(self, kind: str) -> bool:
        """Add or replace a ``kind`` keyframe at the playhead, from the control's current value.

        Times are clip-relative timeline seconds, except speed, whose
        keyframes are source seconds -- converted through the clip's speed
        segments so the user never does that arithmetic.
        """
        clip = self._current_clip()
        if clip is None:
            return False
        local = self.playhead_time - clip.timeline_start
        if not (-1e-6 <= local <= clip.duration + 1e-6):
            self.status_message.emit("Move the playhead over the selected clip to add a keyframe there.")
            return False
        local = min(max(0.0, local), clip.duration)
        if kind == "opacity":
            attribute, entry = "opacity_keyframes", (local, self.opacity_slider.value() / 100.0)
        elif kind == "position":
            attribute, entry = "position_keyframes", (
                local, self.transform_x_slider.value() / 100.0, self.transform_y_slider.value() / 100.0)
        elif kind == "volume":
            attribute, entry = "volume_keyframes", (local, self.volume_slider.value() / 100.0)
        elif kind == "speed":
            # The source frame on screen at the playhead (reverse/freeze aware).
            attribute, entry = "speed_keyframes", (clip.source_time_at(local) - clip.source_in, self.speed_spin.value())
        else:
            return False
        keyframes = [tuple(k) for k in getattr(clip, attribute) if abs(k[0] - entry[0]) > 1e-3]
        keyframes.append(entry)
        keyframes.sort()
        if not self._commit_now(attribute, keyframes, f"Key {kind.title()} at Playhead"):
            return False
        if len(keyframes) == 1:
            self.status_message.emit(f"{kind.title()} keyframe added. Add a second one to start animating.")
        self._refresh_keyframe_views(clip)
        return True

    def _clear_keyframes(self, attribute: str, label: str) -> None:
        clip = self._current_clip()
        if clip is not None and getattr(clip, attribute):
            self._commit_now(attribute, [], label)
            self._refresh_keyframe_views(clip)

    def _refresh_keyframe_views(self, clip: Clip) -> None:
        self._refresh_opacity_keyframe_table(clip.opacity_keyframes)
        self._refresh_position_keyframe_table(clip.position_keyframes)
        self._refresh_speed_keyframe_table(clip.speed_keyframes)
        self._refresh_volume_keys_label(clip)

    def _refresh_volume_keys_label(self, clip: Clip) -> None:
        keys = sorted(clip.volume_keyframes)
        if not keys:
            self.volume_keys_label.setText("No volume keyframes (use \u25c6 next to Volume)")
        else:
            text = ", ".join(f"{t:.2f}s {v * 100:.0f}%" for t, v in keys[:6]) + (" ..." if len(keys) > 6 else "")
            note = "" if len(keys) >= 2 else " (add one more to animate)"
            self.volume_keys_label.setText(text + note)

    # ------------------------------------------------------------------
    # Effect stack (B7): a form generated from each effect's parameter schema
    # ------------------------------------------------------------------
    def _effects_copy(self):
        clip = self._current_clip()
        return copy.deepcopy(clip.effects) if clip is not None else None

    def _commit_effects(self, effects, label: str, select_row: Optional[int] = None) -> None:
        if self._commit_now("effects", effects, label):
            clip = self._current_clip()
            self._refresh_effects_list(clip, select_row)

    def _refresh_effects_list(self, clip: Optional[Clip], select_row: Optional[int] = None) -> None:
        previous = self.effects_list.currentRow() if select_row is None else select_row
        self.effects_list.blockSignals(True)
        self.effects_list.clear()
        for effect in clip.effects if clip is not None else []:
            spec = effect.spec
            label = spec.label if spec else f"{effect.kind} (unknown, kept as is)"
            if not effect.enabled:
                label += " (off)"
            elif spec and not effect.is_renderable():
                label += " (needs a file)"
            self.effects_list.addItem(QListWidgetItem(label))
        self.effects_list.blockSignals(False)
        count = self.effects_list.count()
        row = min(max(previous, 0), count - 1) if count else -1
        self.effects_list.setCurrentRow(row)
        self._on_effect_row_changed(row)

    def _add_effect(self) -> None:
        effects = self._effects_copy()
        if effects is None:
            return
        effects.append(EffectInstance.create(self.effect_kind_combo.currentData()))
        self._commit_effects(effects, "Add Effect", len(effects) - 1)

    def _remove_effect(self) -> None:
        effects, row = self._effects_copy(), self.effects_list.currentRow()
        if effects is None or not (0 <= row < len(effects)):
            return
        del effects[row]
        self._commit_effects(effects, "Remove Effect", max(0, row - 1))

    def _move_effect(self, step: int) -> None:
        effects, row = self._effects_copy(), self.effects_list.currentRow()
        if effects is None or not (0 <= row < len(effects)) or not (0 <= row + step < len(effects)):
            return
        effects[row], effects[row + step] = effects[row + step], effects[row]
        self._commit_effects(effects, "Reorder Effects", row + step)

    def _on_effect_enabled_toggled(self, checked: bool) -> None:
        effects, row = self._effects_copy(), self.effects_list.currentRow()
        if self._loading or effects is None or not (0 <= row < len(effects)) or effects[row].enabled == checked:
            return
        effects[row].enabled = checked
        self._commit_effects(effects, "Toggle Effect", row)

    def set_effect_param(self, row: int, name: str, value) -> bool:
        effects = self._effects_copy()
        if effects is None or not (0 <= row < len(effects)) or effects[row].params.get(name) == value:
            return False
        effects[row].params[name] = value
        self._commit_effects(effects, "Edit Effect", row)
        return True

    def set_effect_mask(self, row: int, mask) -> bool:
        """Set (or clear, with None / shape "none") the selected effect's mask (F16), undoable."""
        from core.effect_masks import clean_mask

        effects = self._effects_copy()
        if effects is None or not (0 <= row < len(effects)):
            return False
        mask = clean_mask(mask)
        if effects[row].mask == mask:
            return False
        effects[row].mask = mask
        self._commit_effects(effects, "Edit Effect Mask", row)
        return True

    def _mask_editor(self, row: int, effect) -> QWidget:
        """Shape, centre, size, angle, feather and invert for the effect's mask."""
        from core.effect_masks import DEFAULT_MASK, MASK_SHAPES, clean_mask

        current = clean_mask(effect.mask) or dict(DEFAULT_MASK)
        box = QWidget()
        form = QFormLayout(box)
        form.setContentsMargins(0, 0, 0, 0)
        shape = QComboBox()
        labels = {"none": "Whole frame", "rectangle": "Rectangle", "ellipse": "Ellipse", "linear": "Linear gradient"}
        for name in MASK_SHAPES:
            shape.addItem(labels[name], name)
        shape.setCurrentIndex(MASK_SHAPES.index(current["shape"]))
        shape.setAccessibleName("Mask shape")
        form.addRow("Mask", shape)
        fields = {}
        for key, label, low, high, step, scale in (
            ("cx", "Centre X (%)", -50, 150, 1, 100), ("cy", "Centre Y (%)", -50, 150, 1, 100),
            ("w", "Width (%)", 0, 200, 1, 100), ("h", "Height (%)", 0, 200, 1, 100),
            ("angle", "Angle (°)", -360, 360, 1, 1), ("feather", "Feather (%)", 0, 50, 0.5, 100),
        ):
            spin = QDoubleSpinBox()
            spin.setRange(low, high)
            spin.setSingleStep(step)
            spin.setDecimals(1)
            spin.setValue(current[key] * scale)
            spin.setAccessibleName(f"Mask {label}")
            fields[key] = (spin, scale)
            form.addRow(label, spin)
        invert = QCheckBox("Invert (apply outside)")
        invert.setChecked(current["invert"])
        form.addRow("", invert)

        def commit(*_args):
            chosen = shape.currentData()
            mask = None if chosen == "none" else {"shape": chosen, "invert": invert.isChecked(),
                                                  **{key: spin.value() / scale for key, (spin, scale) in fields.items()}}
            self.set_effect_mask(row, mask)

        def refresh_enabled():
            on = shape.currentData() != "none"
            sized = shape.currentData() in ("rectangle", "ellipse")
            for key, (spin, _scale) in fields.items():
                spin.setEnabled(on and (sized or key not in ("w", "h")))
            invert.setEnabled(on)

        shape.currentIndexChanged.connect(lambda _i: (refresh_enabled(), commit()))
        for spin, _scale in fields.values():
            spin.editingFinished.connect(commit)
        invert.toggled.connect(commit)
        refresh_enabled()
        box.mask_controls = {"shape": shape, "invert": invert, **{key: spin for key, (spin, _s) in fields.items()}}
        return box

    def _on_effect_row_changed(self, row: int) -> None:
        while self.effect_params_form.rowCount():
            self.effect_params_form.removeRow(0)
        clip = self._current_clip()
        effect = clip.effects[row] if clip is not None and 0 <= row < len(clip.effects) else None
        self.effect_enabled_checkbox.blockSignals(True)
        self.effect_enabled_checkbox.setChecked(bool(effect and effect.enabled))
        self.effect_enabled_checkbox.setEnabled(effect is not None)
        self.effect_enabled_checkbox.blockSignals(False)
        if effect is None or effect.spec is None:
            return
        values = effect.resolved_params()
        for spec in effect.spec.params:
            self.effect_params_form.addRow(spec.label, self._param_editor(row, spec, values[spec.name]))
        if effect.spec.stage != "source":  # stabilization works on whole frames
            self.effect_mask_editor = self._mask_editor(row, effect)
            self.effect_params_form.addRow(self.effect_mask_editor)

    def key_effect_param_at_playhead(self, row: int, name: str) -> bool:
        """Add/replace a keyframe of effect ``row``'s parameter ``name`` at the playhead (its current value).

        Same rules as the other keyframe buttons: clip-local seconds, the
        playhead must be over the clip, 2+ keyframes animate the parameter.
        """
        clip = self._current_clip()
        effects = self._effects_copy()
        if clip is None or effects is None or not (0 <= row < len(effects)):
            return False
        spec = effects[row].param_spec(name)
        if spec is None or not spec.animatable:
            return False
        local = self.playhead_time - clip.timeline_start
        if not (-1e-6 <= local <= clip.duration + 1e-6):
            self.status_message.emit("Move the playhead over the selected clip to add a keyframe there.")
            return False
        local = min(max(0.0, local), clip.duration)
        value = float(effects[row].resolved_params()[name])
        keys = [tuple(k) for k in effects[row].keyframes.get(name, []) if abs(k[0] - local) > 1e-3]
        effects[row].keyframes[name] = sorted(keys + [(local, value)])
        self._commit_effects(effects, f"Key {spec.label} at Playhead", row)
        if len(effects[row].keyframes[name]) == 1:
            self.status_message.emit(f"{spec.label} keyframe added. Add a second one to start animating.")
        return True

    def clear_effect_param_keyframes(self, row: int, name: str) -> bool:
        effects = self._effects_copy()
        if effects is None or not (0 <= row < len(effects)) or not effects[row].keyframes.get(name):
            return False
        del effects[row].keyframes[name]
        self._commit_effects(effects, "Clear Effect Keyframes", row)
        return True

    @staticmethod
    def _keyframe_summary(keys) -> str:
        if not keys:
            return "not animated"
        text = f"{len(keys)} key{'s' if len(keys) != 1 else ''}"
        return text + (" (add one more to animate)" if len(keys) == 1 else "")

    def _with_effect_key_buttons(self, row: int, spec, editor: QWidget, keys) -> QWidget:
        """``editor`` plus a keyframe-at-playhead button, a key count and a Clear button."""
        holder = QWidget()
        line = QHBoxLayout(holder)
        line.setContentsMargins(0, 0, 0, 0)
        line.addWidget(editor, 1)
        key_btn = QPushButton("\u25c6")
        key_btn.setFixedWidth(28)
        key_btn.setToolTip(f"Add/update a {spec.label} keyframe at the playhead from the current value")
        key_btn.setAccessibleName(f"Key {spec.label} at playhead")
        key_btn.clicked.connect(lambda: self.key_effect_param_at_playhead(row, spec.name))
        count = QLabel(self._keyframe_summary(keys))
        count.setToolTip(", ".join(f"{t:.2f}s = {v:.2f}" for t, v in sorted(keys)) or "No keyframes")
        clear_btn = QPushButton("Clear")
        clear_btn.setToolTip(f"Remove every {spec.label} keyframe")
        clear_btn.setEnabled(bool(keys))
        clear_btn.clicked.connect(lambda: self.clear_effect_param_keyframes(row, spec.name))
        for widget in (key_btn, count, clear_btn):
            line.addWidget(widget)
        holder.key_button, holder.key_count_label = key_btn, count  # for tests
        return holder

    def _param_editor(self, row: int, spec, value) -> QWidget:
        editor = self._plain_param_editor(row, spec, value)
        if not spec.animatable:
            return editor
        clip = self._current_clip()
        keys = clip.effects[row].keyframes.get(spec.name, []) if clip is not None and row < len(clip.effects) else []
        return self._with_effect_key_buttons(row, spec, editor, keys)

    def _plain_param_editor(self, row: int, spec, value) -> QWidget:
        name = spec.name
        if spec.kind in ("float", "int"):
            box = QDoubleSpinBox() if spec.kind == "float" else QSpinBox()
            box.setRange(spec.minimum, spec.maximum)
            box.setSingleStep(spec.step)
            if spec.kind == "float":
                box.setDecimals(2)
            box.setValue(value)
            box.editingFinished.connect(lambda b=box: self.set_effect_param(row, name, b.value()))
            return box
        if spec.kind == "bool":
            box = QCheckBox()
            box.setChecked(bool(value))
            box.toggled.connect(lambda checked: self.set_effect_param(row, name, checked))
            return box
        if spec.kind == "choice":
            box = QComboBox()
            box.addItems(list(spec.choices))
            box.setCurrentText(value)
            box.currentTextChanged.connect(lambda text: self.set_effect_param(row, name, text))
            return box
        if spec.kind == "color":
            button = QPushButton(value)
            button.setStyleSheet(f"background-color: {value};")

            def pick():
                color = QColorDialog.getColor(QColor(value), self, spec.label)
                if color.isValid():
                    self.set_effect_param(row, name, color.name().upper())
            button.clicked.connect(pick)
            return button
        # file
        holder = QWidget()
        line = QHBoxLayout(holder)
        line.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit(value)
        edit.setPlaceholderText("Choose a .cube file")
        edit.editingFinished.connect(lambda: self.set_effect_param(row, name, edit.text().strip()))
        browse = QPushButton("Browse...")

        def choose():
            path, _ = QFileDialog.getOpenFileName(self, spec.label, edit.text(), "3D LUT (*.cube *.3dl);;All Files (*)")
            if path:
                self.set_effect_param(row, name, path)
        browse.clicked.connect(choose)
        line.addWidget(edit, 1)
        line.addWidget(browse)
        return holder

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
        if self._commit_now("speed_keyframes", keyframes, "Add Speed Keyframe"):
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
        if self._commit_now("speed_keyframes", keyframes, "Remove Speed Keyframe"):
            self._refresh_speed_keyframe_table(keyframes)

    def _on_transition_changed(self, index: int) -> None:
        if self._loading or index < 0:
            return
        new_value = TransitionType(self.transition_combo.itemData(index))
        self.transition_duration_spin.setEnabled(new_value != TransitionType.NONE)
        self._commit_now("transition_out", new_value, "Edit Transition")

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

    def _pick_text_color(self, attribute: str, title: str) -> None:
        color = QColorDialog.getColor(getattr(self, attribute), self, title)
        if color.isValid():
            setattr(self, attribute, color)
            self._apply_text_style()

    # -- title styles (F23) ---------------------------------------------------------
    def refresh_title_styles(self, select: str = "") -> None:
        from core.title_templates import BUILTIN_STYLES, all_styles

        self.title_style_combo.blockSignals(True)
        self.title_style_combo.clear()
        for name in all_styles():
            self.title_style_combo.addItem(name)
        if select:
            self.title_style_combo.setCurrentText(select)
        self.title_style_combo.blockSignals(False)
        self.delete_title_style_btn.setEnabled(bool(self.title_style_combo.count()))
        self._builtin_title_styles = set(BUILTIN_STYLES)

    def _canvas_height(self) -> int:
        return int(getattr(getattr(self.project, "settings", None), "height", 1080) or 1080)

    def apply_title_style(self, name: str) -> bool:
        """Give the selected text clip the style ``name`` (one undo step), keeping its text."""
        from core.title_templates import all_styles, apply_style

        clip = self._current_clip()
        style = all_styles().get(name)
        if clip is None or clip.media_type != MediaType.TEXT or style is None or self.history is None:
            return False
        new_style = apply_style(clip.text_style or TextStyle(), style, self._canvas_height())
        if new_style == clip.text_style:
            return False
        self._end_gesture()
        self.history.execute(UpdateTextClipCommand(self.project, self.clip_id, new_style))
        self.set_selected_clip(self.clip_id)
        self.clip_changed.emit(self.clip_id)
        self.project_modified.emit()
        return True

    def save_title_style(self, name: Optional[str] = None) -> Optional[str]:
        from core.title_templates import save_user_style, style_of

        clip = self._current_clip()
        if clip is None or clip.media_type != MediaType.TEXT or clip.text_style is None:
            return None
        if name is None:
            name, ok = QInputDialog.getText(self, "Save Title Style", "Name for this style:")
            if not ok:
                return None
        name = name.strip()
        if not name:
            return None
        save_user_style(name, style_of(clip.text_style, self._canvas_height()))
        self.refresh_title_styles(select=name)
        return name

    def delete_title_style(self, name: str) -> bool:
        from core.title_templates import delete_user_style

        if name in getattr(self, "_builtin_title_styles", set()) and not self._is_user_style(name):
            return False
        removed = delete_user_style(name)
        self.refresh_title_styles()
        return removed

    @staticmethod
    def _is_user_style(name: str) -> bool:
        from core.title_templates import load_user_styles

        return name in load_user_styles()

    def _apply_text_style(self, *_args) -> None:
        if self._loading or self.history is None or self.project is None or self.clip_id is None:
            return
        clip = self._current_clip()
        if clip is None or clip.media_type != MediaType.TEXT:
            return
        new_style = TextStyle(
            content=self.text_content.toPlainText(),
            font_family=self.font_combo.currentFont().family() or "Arial",
            font_size=self.font_size_spin.value(),
            color=self._selected_color.name(),
            outline_color=self._outline_color.name(),
            outline_width=self.outline_width_spin.value(),
            bold=self.bold_checkbox.isChecked(),
            italic=self.italic_checkbox.isChecked(),
            position_x=self.pos_x_slider.value() / 100.0,
            position_y=self.pos_y_slider.value() / 100.0,
            alignment=self.alignment_combo.currentText(),
            shadow_enabled=self.shadow_checkbox.isChecked(),
            shadow_color=self._shadow_color.name(),
            shadow_offset=self.shadow_offset_spin.value(),
            box_enabled=self.box_checkbox.isChecked(),
            box_color=self._box_color.name(),
            box_opacity=self.box_opacity_spin.value() / 100.0,
            box_padding=clip.text_style.box_padding if clip.text_style else 12,
        )
        if clip.text_style is not None and new_style == clip.text_style:
            return  # e.g. focus left the text box without a change
        self._end_gesture()
        self.history.execute(UpdateTextClipCommand(self.project, self.clip_id, new_style))
        self.name_label.setText(f"{new_style.content[:24] or 'Text'}  ·  Text Clip")
        self.clip_changed.emit(self.clip_id)
        self.project_modified.emit()
