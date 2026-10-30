"""Editable keyboard shortcuts (UI8), in the pattern of Media Player Pro's shortcuts dialog.

Every named QAction of the main window can get a new key, lose its key or go back to its default;
the defaults are what the window built, recorded before the saved keys are applied. Only what
differs from the defaults is saved, as portable text in QSettings ("shortcuts/overrides", JSON;
"" = no key). A key another action already uses isn't assigned: the dialog says which one has it.

An action is identified by its menu text without "&" ("Split at Playhead"), the one name the
window gives each command. Display-only menu entries ("Back 1 Second\\tShift+Left": the hint for a
timeline-scoped action) aren't actions of their own and are left out; the window refreshes their
hint text after a change (see MainWindow._refresh_scoped_key_hints).
"""

from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional

from PySide6.QtCore import QSettings
from PySide6.QtGui import QAction, QKeySequence

SETTINGS_KEY = "shortcuts/overrides"
PORTABLE = QKeySequence.SequenceFormat.PortableText


def action_name(action: QAction) -> str:
    return action.text().replace("&", "").split("\t")[0].strip()


class ShortcutBook:
    """The window's named actions by name, with their default keys."""

    def __init__(self, actions: Iterable[QAction]):
        self._actions: Dict[str, QAction] = {}
        self._defaults: Dict[str, QKeySequence] = {}
        for action in actions:
            name = action_name(action)
            if not name or action.isSeparator() or "\t" in action.text() or action.menu() is not None:
                continue
            # The same command can appear twice (a menu and a context menu): the one with a key
            # wins, so editing it changes the key the user actually presses.
            current = self._actions.get(name)
            if current is not None and (not current.shortcut().isEmpty() or action.shortcut().isEmpty()):
                continue
            self._actions[name] = action
            self._defaults[name] = QKeySequence(action.shortcut())

    def names(self) -> List[str]:
        return sorted(self._actions, key=str.lower)

    def __getitem__(self, name: str) -> QAction:
        return self._actions[name]

    def __contains__(self, name: str) -> bool:
        return name in self._actions

    def default_shortcut(self, name: str) -> QKeySequence:
        return QKeySequence(self._defaults[name])

    def is_customized(self, name: str) -> bool:
        return self._actions[name].shortcut() != self._defaults[name]

    def conflict(self, sequence, except_name: Optional[str] = None) -> Optional[str]:
        """The name of the action that already uses `sequence`, or None."""
        sequence = QKeySequence(sequence)
        if sequence.isEmpty():
            return None
        for name, action in self._actions.items():
            if name != except_name and action.shortcut() == sequence:
                return name
        return None

    def set_shortcut(self, name: str, sequence) -> None:
        self._actions[name].setShortcut(QKeySequence(sequence))

    def overrides(self) -> Dict[str, str]:
        return {name: action.shortcut().toString(PORTABLE)
                for name, action in self._actions.items() if action.shortcut() != self._defaults[name]}

    def apply_overrides(self, overrides) -> List[str]:
        """Applies saved keys; returns the names it skipped (unknown, not a key, or taken)."""
        skipped = []
        for name, text in (overrides or {}).items():
            if name not in self._actions or not isinstance(text, str):
                skipped.append(name)
                continue
            sequence = QKeySequence(text, PORTABLE)
            # Unparsable text prints as "" but isn't isEmpty() (it holds an invalid key).
            if (text and not sequence.toString(PORTABLE)) or self.conflict(sequence, name):
                skipped.append(name)
                continue
            self.set_shortcut(name, sequence)
        return skipped

    def reset(self) -> None:
        for name, action in self._actions.items():
            action.setShortcut(QKeySequence(self._defaults[name]))

    # -- QSettings ------------------------------------------------------------
    def load(self, settings: QSettings) -> List[str]:
        try:
            overrides = json.loads(settings.value(SETTINGS_KEY, "", type=str) or "{}")
        except (TypeError, ValueError):
            return []
        return self.apply_overrides(overrides if isinstance(overrides, dict) else {})

    def save(self, settings: QSettings) -> None:
        overrides = self.overrides()
        if overrides:
            settings.setValue(SETTINGS_KEY, json.dumps(overrides))
        else:
            settings.remove(SETTINGS_KEY)
