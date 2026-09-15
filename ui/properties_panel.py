"""The right-hand inspector: edit properties of the selected clip."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.clip import Clip, MediaType, TextStyle
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
        self.text_group.setVisible(is_text)
        self.clip_group.setVisible(not is_text)

        self.volume_slider.setValue(int(clip.volume * 100))
        self.mute_checkbox.setChecked(clip.muted)
        self.speed_spin.setValue(clip.speed)
        self.opacity_slider.setValue(int(clip.opacity * 100))
        self.fade_in_spin.setValue(clip.fade_in)
        self.fade_out_spin.setValue(clip.fade_out)

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
