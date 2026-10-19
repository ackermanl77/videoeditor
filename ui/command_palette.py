"""Command search (F27): Ctrl+K lists every menu action, filtered as you type.

The list is collected from the menu bar each time the palette opens (``collect_commands``), so
it always matches the menus -- recent projects, checkable toggles and submenus included -- and
needs no second registry to keep in sync. Each entry shows its menu path ("Edit > Split Clip at
Playhead") and shortcut; disabled actions are listed greyed out and can't be run. Typing filters
by every word (in any order, ignoring case and the menus' "&" accelerators); entries whose name
starts with the query rank first. Enter (or a double-click) runs the selected action after the
palette closes; Escape closes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenuBar, QVBoxLayout, QWidget


@dataclass
class Command:
    path: str  # "Edit > Split Clip at Playhead"
    name: str  # "Split Clip at Playhead"
    shortcut: str
    action: QAction

    @property
    def enabled(self) -> bool:
        return self.action.isEnabled()


def _clean(text: str) -> str:
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&").strip()


def collect_commands(menu_bar: QMenuBar) -> List[Command]:
    """Every runnable action under the menu bar, depth first, in menu order."""
    commands: List[Command] = []

    def walk(menu, trail: List[str]) -> None:
        for action in menu.actions():
            if action.isSeparator() or not action.isVisible():
                continue
            submenu = action.menu()
            text = _clean(action.text())
            if submenu is not None:
                walk(submenu, trail + [text])
                continue
            name, _tab, key_hint = text.partition("\t")  # display-only key hints ("Next Frame\tRight")
            shortcut = action.shortcut().toString(QKeySequence.NativeText) or key_hint.strip()
            if name:
                commands.append(Command(" > ".join(trail + [name.strip()]), name.strip(), shortcut, action))

    for top in menu_bar.actions():
        if top.menu() is not None:
            walk(top.menu(), [_clean(top.text())])
    return commands


def filter_commands(commands: List[Command], query: str) -> List[Command]:
    """Commands whose path contains every word of ``query``; name-prefix matches first."""
    words = query.lower().split()
    if not words:
        return list(commands)
    matches = [command for command in commands if all(word in command.path.lower() for word in words)]
    query_text = " ".join(words)
    return sorted(matches, key=lambda c: (not c.name.lower().startswith(query_text), query_text not in c.name.lower()))


class CommandPalette(QDialog):
    def __init__(self, menu_bar: QMenuBar, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Command Search")
        self.setModal(True)
        self.resize(560, 420)
        self.commands = collect_commands(menu_bar)
        self.chosen: Optional[QAction] = None
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Type a command, e.g. split, export, marker...")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Command search")
        layout.addWidget(self.search)
        self.results = QListWidget()
        self.results.setAccessibleName("Matching commands")
        layout.addWidget(self.results, 1)
        self.hint = QLabel("Enter runs the command - Esc closes")
        self.hint.setStyleSheet("color: #9aa1ad;")
        layout.addWidget(self.hint)
        self.search.textChanged.connect(self._refresh)
        self.search.returnPressed.connect(self._accept_current)
        self.results.itemActivated.connect(lambda _item: self._accept_current())
        self.search.installEventFilter(self)
        self._refresh("")

    def _refresh(self, query: str) -> None:
        self.results.clear()
        for command in filter_commands(self.commands, query):
            label = command.path + (f"    {command.shortcut}" if command.shortcut else "")
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, command)
            if not command.enabled:
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.results.addItem(item)
        for row in range(self.results.count()):
            if self.results.item(row).flags() & Qt.ItemIsEnabled:
                self.results.setCurrentRow(row)
                break
        self.hint.setText(f"{self.results.count()} command(s) - Enter runs the selected one - Esc closes")

    def eventFilter(self, watched, event) -> bool:
        # Up/Down in the search field move through the results without leaving it.
        if watched is self.search and event.type() == event.Type.KeyPress and event.key() in (Qt.Key_Up, Qt.Key_Down):
            step = -1 if event.key() == Qt.Key_Up else 1
            row = self.results.currentRow()
            for _ in range(self.results.count()):
                row = (row + step) % max(1, self.results.count())
                if self.results.item(row).flags() & Qt.ItemIsEnabled:
                    self.results.setCurrentRow(row)
                    break
            return True
        return super().eventFilter(watched, event)

    def _accept_current(self) -> None:
        item = self.results.currentItem()
        if item is None or not item.flags() & Qt.ItemIsEnabled:
            return
        self.chosen = item.data(Qt.UserRole).action
        self.accept()


def run_command_palette(menu_bar: QMenuBar, parent: Optional[QWidget] = None) -> Optional[QAction]:
    """Show the palette; trigger and return the chosen action (None when closed)."""
    palette = CommandPalette(menu_bar, parent)
    palette.exec()
    if palette.chosen is not None:
        palette.chosen.trigger()
    return palette.chosen
