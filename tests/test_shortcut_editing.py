"""Editable keyboard shortcuts (UI8): ui/shortcut_editing.py and the Help > Keyboard Shortcuts dialog."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QAction, QKeySequence
    from PySide6.QtWidgets import QApplication, QMenu

    from ui.help_dialogs import ShortcutsDialog
    from ui.shortcut_editing import SETTINGS_KEY, ShortcutBook

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PYSIDE6_AVAILABLE = False


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class ShortcutEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = tempfile.mkdtemp(prefix="ve_settings_")
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, folder)
        self.addCleanup(QSettings.setDefaultFormat, QSettings.Format.NativeFormat)
        QSettings().clear()

    def _actions(self):
        menu = QMenu("&Edit")
        split = QAction("&Split at Playhead", menu)
        split.setShortcut("S")
        copy = QAction("Copy", menu)
        copy.setShortcut("Ctrl+C")
        hint = QAction("Back 1 Second\tShift+Left", menu)  # display-only menu hint
        bare = QAction("Ripple Delete", menu)
        duplicate = QAction("Copy", menu)  # the same command in a context menu, without a key
        self.addCleanup(menu.deleteLater)
        return menu, [menu.menuAction(), split, copy, hint, bare, duplicate]

    def test_the_book_names_commands_and_keeps_their_defaults(self):
        _menu, actions = self._actions()
        book = ShortcutBook(actions)
        self.assertEqual(book.names(), ["Copy", "Ripple Delete", "Split at Playhead"])  # no "Edit", no hint
        self.assertIs(book["Copy"], actions[2])  # the copy that has the key
        self.assertEqual(book.conflict(QKeySequence("S"), "Copy"), "Split at Playhead")
        self.assertIsNone(book.conflict(QKeySequence("S"), "Split at Playhead"))

        book.set_shortcut("Ripple Delete", QKeySequence("Shift+Delete"))
        book.set_shortcut("Copy", QKeySequence())
        self.assertEqual(book.overrides(), {"Ripple Delete": "Shift+Del", "Copy": ""})
        book.save(QSettings())

        fresh = ShortcutBook(self._actions()[1])
        skipped = fresh.apply_overrides({**book.overrides(), "Gone": "X", "Split at Playhead": "%%bad%%"})
        self.assertEqual(sorted(skipped), ["Gone", "Split at Playhead"])
        self.assertTrue(fresh["Copy"].shortcut().isEmpty())
        again = ShortcutBook(self._actions()[1])
        self.assertEqual(again.load(QSettings()), [])
        self.assertEqual(again["Ripple Delete"].shortcut(), QKeySequence("Shift+Delete"))
        again.reset()
        self.assertEqual(again.overrides(), {})
        again.save(QSettings())
        self.assertFalse(QSettings().contains(SETTINGS_KEY))

    def test_the_dialog_assigns_refuses_a_taken_key_and_restores(self):
        _menu, actions = self._actions()
        book = ShortcutBook(actions)
        changes = []
        dialog = ShortcutsDialog(book, lambda: changes.append(1))
        self.addCleanup(dialog.deleteLater)
        dialog.select("Ripple Delete")
        dialog.key_edit.setKeySequence(QKeySequence("S"))
        self.assertFalse(dialog.assign())
        self.assertIn("Split at Playhead", dialog.status.text())
        dialog.key_edit.setKeySequence(QKeySequence("Shift+Delete"))
        self.assertTrue(dialog.assign())
        self.assertEqual(changes, [1])
        row = next(r for r in range(dialog.table.rowCount()) if dialog.table.item(r, 0).text() == "Ripple Delete")
        self.assertTrue(dialog.table.item(row, 1).text().endswith("•"))
        dialog.filter.setText("ripple")
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertTrue(dialog.restore_default())
        dialog.restore_all()
        self.assertEqual(book.overrides(), {})

    def test_the_window_saves_edited_keys_and_updates_the_menu_hints(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), \
                patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        self.addCleanup(window.deleteLater)
        book = window.shortcut_book
        self.assertIn("Back 1 Second", book.names())
        book.set_shortcut("Back 1 Second", QKeySequence("Ctrl+Alt+Left"))
        window._on_shortcuts_changed()
        hint = next(m for a, m in window._scoped_key_hints if a.text() == "Back 1 Second")
        self.assertEqual(hint.text().split("\t")[1], QKeySequence("Ctrl+Alt+Left").toString(QKeySequence.NativeText))
        self.assertIn("Back 1 Second", QSettings().value(SETTINGS_KEY, "", type=str))


if __name__ == "__main__":
    unittest.main()
