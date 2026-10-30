"""Help menu dialogs: Keyboard Shortcuts (the live QActions, editable: ui/shortcut_editing.py) and the User Guide."""

from __future__ import annotations

import os
from typing import Callable, Iterable, List, Optional, Tuple

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPushButton,
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
    """Help > Keyboard Shortcuts, editable (UI8): pick a command, press the new key, Assign. Remove
    leaves it without a key, Default puts its own back, Restore All resets every key. A key that
    another command uses isn't assigned; the line under the table says which command has it."""

    def __init__(self, book, on_changed: Optional[Callable[[], None]] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard Shortcuts")
        self.resize(520, 600)
        self._book = book
        self._on_changed = on_changed or (lambda: None)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Pick a command, press the new key, then Assign. A • marks a changed key."))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter commands or keys")
        self.filter.setAccessibleName("Filter shortcuts")
        self.filter.textChanged.connect(lambda _text: self.refresh(self.selected_name()))
        layout.addWidget(self.filter)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Command", "Shortcut"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.setAccessibleName("Keyboard shortcuts")
        self.table.itemSelectionChanged.connect(self._on_selection)
        layout.addWidget(self.table)

        editor = QHBoxLayout()
        self.key_edit = QKeySequenceEdit()
        self.key_edit.setMaximumSequenceLength(1)
        self.key_edit.setAccessibleName("New shortcut")
        editor.addWidget(self.key_edit, 1)
        self.assign_button = QPushButton("Assign")
        self.assign_button.clicked.connect(self.assign)
        self.clear_button = QPushButton("Remove")
        self.clear_button.clicked.connect(self.clear)
        self.default_button = QPushButton("Default")
        self.default_button.clicked.connect(self.restore_default)
        for button in (self.assign_button, self.clear_button, self.default_button):
            editor.addWidget(button)
        layout.addLayout(editor)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        bottom = QHBoxLayout()
        self.reset_all_button = QPushButton("Restore All")
        self.reset_all_button.clicked.connect(self.restore_all)
        bottom.addWidget(self.reset_all_button)
        bottom.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        layout.addLayout(bottom)
        self.refresh()

    def refresh(self, select_name: Optional[str] = None) -> None:
        needle = self.filter.text().strip().lower()
        rows = []
        for name in self._book.names():
            key = self._book[name].shortcut().toString(QKeySequence.NativeText)
            if needle and needle not in name.lower() and needle not in key.lower():
                continue
            rows.append((name, key + ("  •" if self._book.is_customized(name) else "")))
        self.table.setRowCount(len(rows))
        for row, (name, key) in enumerate(rows):
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem(key))
            if name == select_name:
                self.table.selectRow(row)
        self._on_selection()

    def selected_name(self) -> Optional[str]:
        items = self.table.selectedItems()
        return self.table.item(items[0].row(), 0).text() if items else None

    def select(self, name: str) -> None:
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).text() == name:
                self.table.selectRow(row)
                return

    def _on_selection(self) -> None:
        name = self.selected_name()
        for widget in (self.key_edit, self.assign_button, self.clear_button, self.default_button):
            widget.setEnabled(name is not None)
        if name is not None:
            self.key_edit.setKeySequence(self._book[name].shortcut())

    def _apply(self, name: str, sequence: QKeySequence) -> bool:
        taken_by = self._book.conflict(sequence, name)
        if taken_by:
            self.status.setText(f"{sequence.toString(QKeySequence.NativeText)} is already the shortcut for {taken_by}.")
            return False
        self._book.set_shortcut(name, sequence)
        self.status.setText("")
        self._on_changed()
        self.refresh(name)
        return True

    def assign(self) -> bool:
        name = self.selected_name()
        return name is not None and self._apply(name, self.key_edit.keySequence())

    def clear(self) -> bool:
        name = self.selected_name()
        return name is not None and self._apply(name, QKeySequence())

    def restore_default(self) -> bool:
        name = self.selected_name()
        return name is not None and self._apply(name, self._book.default_shortcut(name))

    def restore_all(self) -> None:
        self._book.reset()
        self.status.setText("Every shortcut is back to its default.")
        self._on_changed()
        self.refresh()


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
