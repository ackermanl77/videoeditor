"""Help menu dialogs: Keyboard Shortcuts (generated from the live QActions) and the User Guide."""

from __future__ import annotations

import os
from typing import Iterable, List, Optional, Tuple

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

GUIDE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "USER_GUIDE.md")


def shortcut_rows(actions: Iterable[QAction]) -> List[Tuple[str, str]]:
    """``(command, key)`` for every action with a shortcut, sorted by command, without duplicates."""
    rows = {}
    for action in actions:
        key = action.shortcut().toString(QKeySequence.NativeText)
        if not key:
            continue
        name = action.text().replace("&", "").split("\t")[0].strip()
        if name:
            rows[(name, key)] = None
    return sorted(rows, key=lambda r: r[0].lower())


class ShortcutsDialog(QDialog):
    def __init__(self, actions: Iterable[QAction], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard Shortcuts")
        self.resize(460, 560)
        layout = QVBoxLayout(self)
        rows = shortcut_rows(actions)
        self.table = QTableWidget(len(rows), 2)
        self.table.setHorizontalHeaderLabels(["Command", "Shortcut"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for i, (name, key) in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(name))
            self.table.setItem(i, 1, QTableWidgetItem(key))
        layout.addWidget(self.table)


class UserGuideDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None, path: str = GUIDE_PATH):
        super().__init__(parent)
        self.setWindowTitle("User Guide")
        self.resize(820, 680)
        layout = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.browser.setMarkdown(f.read())
        except OSError:
            self.browser.setPlainText(f"The user guide wasn't found at {path}.")
        layout.addWidget(self.browser)
