"""The Markers panel (F15): every marker in time order -- colour, timecode, name and note.

Double-clicking a row (or pressing Enter) moves the playhead there; the name and the note are edited
in place, each edit one undo step (RenameMarkerCommand / EditMarkerNoteCommand); Delete removes the
selected marker. The panel refreshes whenever the history changes, so ruler edits and undo show up.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.commands import EditMarkerNoteCommand, RemoveMarkerCommand, RenameMarkerCommand

COL_COLOR, COL_TIME, COL_NAME, COL_NOTE = range(4)


class MarkersPanel(QWidget):
    seek_requested = Signal(float)
    project_modified = Signal()

    def __init__(self, format_time: Callable[[float], str], parent=None):
        super().__init__(parent)
        self.project = None
        self.history = None
        self._format_time = format_time
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["", "Time", "Name", "Note"])
        self.table.setAccessibleName("Markers")
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_COLOR, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(COL_COLOR, 18)
        header.setSectionResizeMode(COL_TIME, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_NOTE, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.cellDoubleClicked.connect(self._on_double_clicked)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        self.empty_label = QLabel("No markers yet: Timeline > Add Marker at Playhead (M).")
        row.addWidget(self.empty_label, 1)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setToolTip("Delete the selected marker")
        self.delete_button.clicked.connect(self.delete_selected)
        row.addWidget(self.delete_button)
        layout.addLayout(row)

    def set_project(self, project, history) -> None:
        self.project, self.history = project, history
        self.refresh()

    def refresh(self) -> None:
        markers = self.project.sorted_markers() if self.project is not None else []
        self._loading = True
        self.table.setRowCount(len(markers))
        for row, marker in enumerate(markers):
            swatch = QTableWidgetItem("")
            swatch.setBackground(QColor(marker.color))
            swatch.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            swatch.setData(Qt.ItemDataRole.UserRole, marker.id)
            time_item = QTableWidgetItem(self._format_time(marker.time))
            time_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            time_item.setToolTip("Double-click to move the playhead here")
            self.table.setItem(row, COL_COLOR, swatch)
            self.table.setItem(row, COL_TIME, time_item)
            self.table.setItem(row, COL_NAME, QTableWidgetItem(marker.name))
            self.table.setItem(row, COL_NOTE, QTableWidgetItem(marker.note))
        self._loading = False
        self.empty_label.setVisible(not markers)
        self.delete_button.setEnabled(bool(markers))

    def marker_id_at(self, row: int) -> Optional[str]:
        item = self.table.item(row, COL_COLOR)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _commit(self, command) -> None:
        self.history.execute(command)
        self.project_modified.emit()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or self.project is None or self.history is None:
            return
        marker = self.project.find_marker(self.marker_id_at(item.row()) or "")
        if marker is None:
            return
        text = item.text().strip()
        if item.column() == COL_NAME:
            if text and text != marker.name:
                self._commit(RenameMarkerCommand(self.project, marker.id, text))
            else:
                self.refresh()  # an empty name isn't allowed: show the old one again
        elif item.column() == COL_NOTE and text != marker.note:
            self._commit(EditMarkerNoteCommand(self.project, marker.id, text))

    def _on_double_clicked(self, row: int, column: int) -> None:
        if column in (COL_COLOR, COL_TIME):
            self.seek_to_row(row)

    def seek_to_row(self, row: int) -> None:
        marker = self.project.find_marker(self.marker_id_at(row) or "") if self.project else None
        if marker is not None:
            self.seek_requested.emit(marker.time)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.table.currentRow() >= 0:
            self.seek_to_row(self.table.currentRow())
            return
        if event.key() == Qt.Key.Key_Delete:
            self.delete_selected()
            return
        super().keyPressEvent(event)

    def delete_selected(self) -> bool:
        row = self.table.currentRow()
        marker_id = self.marker_id_at(row) if row >= 0 else None
        if marker_id is None or self.history is None:
            return False
        self._commit(RemoveMarkerCommand(self.project, marker_id))
        return True
