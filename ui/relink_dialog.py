"""File > Relink Media: point offline assets at moved files (see core/relink.py)."""

from __future__ import annotations

import os
from typing import Dict, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.history import History
from core.project import Project
from core.relink import build_relink_command, relink_from_folder, verify_candidate


class RelinkDialog(QDialog):
    """Lists offline media; Locate one file (siblings in its folder are then found
    automatically) or search a whole folder. Each relink is one undo step."""

    relinked = Signal(int)  # number of assets relinked

    def __init__(self, project: Project, history: History, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project = project
        self.history = history
        self.setWindowTitle("Relink Media")
        self.setMinimumWidth(620)

        layout = QVBoxLayout(self)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["File", "Last known location"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.locate_btn = QPushButton("Locate File...")
        self.locate_btn.setToolTip("Pick the moved file; other missing files in the same folder are linked too")
        self.locate_btn.clicked.connect(self._on_locate)
        self.search_btn = QPushButton("Search Folder...")
        self.search_btn.setToolTip("Find every missing file by name in a folder and its subfolders")
        self.search_btn.clicked.connect(self._on_search_folder)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(self.locate_btn)
        buttons.addWidget(self.search_btn)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        offline = self.project.offline_assets()
        self.table.setRowCount(0)
        for asset in offline:
            row = self.table.rowCount()
            self.table.insertRow(row)
            name = QTableWidgetItem(asset.display_name)
            name.setData(0x0100, asset.id)  # Qt.UserRole
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, QTableWidgetItem(os.path.dirname(asset.path)))
        if offline:
            self.table.selectRow(0)
            self.summary_label.setText(
                f"{len(offline)} file(s) used by this project can't be found. Locate one, or search a folder; "
                "matches are checked by duration and frame size."
            )
        else:
            self.summary_label.setText("All media is online.")
        self.locate_btn.setEnabled(bool(offline))
        self.search_btn.setEnabled(bool(offline))

    def selected_asset_id(self) -> Optional[str]:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.data(0x0100) if item is not None else None

    def apply_mapping(self, mapping: Dict[str, str]) -> int:
        command = build_relink_command(self.project, mapping)
        if command is None:
            return 0
        self.history.execute(command)
        self.relinked.emit(len(mapping))
        self.refresh()
        return len(mapping)

    def relink_with_siblings(self, asset_id: str, path: str) -> int:
        """Link ``asset_id`` to ``path``, then look for the other offline files in the same folder."""
        mapping = {asset_id: path}
        others = [a for a in self.project.offline_assets() if a.id != asset_id]
        mapping.update(relink_from_folder(self.project, os.path.dirname(path), assets=others))
        return self.apply_mapping(mapping)

    def _on_locate(self) -> None:
        asset_id = self.selected_asset_id()
        asset = self.project.find_asset(asset_id) if asset_id else None
        if asset is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, f"Locate {asset.display_name}", "", "All Files (*)")
        if not path:
            return
        if not verify_candidate(asset, path):
            answer = QMessageBox.question(
                self, "Relink Media",
                f"{os.path.basename(path)} doesn't match the original's duration or frame size.\n\nLink it anyway?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.relink_with_siblings(asset.id, path)

    def _on_search_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Search Folder for Missing Media")
        if not folder:
            return
        found = self.apply_mapping(relink_from_folder(self.project, folder))
        if not found:
            QMessageBox.information(self, "Relink Media", "No matching files were found in that folder.")
