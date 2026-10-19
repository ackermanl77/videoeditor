"""Workspaces (F30): the built-in layouts, saving/deleting your own, remembering the choice."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QApplication

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PYSIDE6_AVAILABLE = False


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # Keep every QSettings write of this test in a temporary ini file (not the registry).
        folder = tempfile.mkdtemp(prefix="ve_settings_")
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, folder)
        self.addCleanup(QSettings.setDefaultFormat, QSettings.Format.NativeFormat)
        QSettings().clear()

    def _window(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        self.addCleanup(window.deleteLater)
        window.resize(2600, 1200)  # room for the preview panel's minimum width (offscreen fonts are wide)
        window.show()
        QApplication.processEvents()
        return window

    def test_built_in_workspaces_open_their_docks_with_shortcuts(self):
        window = self._window()
        shortcuts = {a.text(): a.shortcut().toString() for a in window.findChildren(QAction)}
        self.assertEqual([shortcuts[name] for name in ("Editing", "Color", "Audio", "Captions")],
                         ["Alt+Shift+1", "Alt+Shift+2", "Alt+Shift+3", "Alt+Shift+4"])
        self.assertTrue(window.apply_workspace("Color"))
        self.assertTrue(window.scopes_dock.isVisible() and not window.mixer_dock.isVisible())
        bin_width, _preview, properties_width = window.top_splitter.sizes()
        self.assertGreater(properties_width, bin_width)
        window.apply_workspace("Audio")
        self.assertTrue(window.mixer_dock.isVisible() and not window.scopes_dock.isVisible())
        top, timeline = window.main_splitter.sizes()
        self.assertLess(abs(top - timeline), 120)  # the timeline gets half the height
        window.apply_workspace("Captions")
        self.assertTrue(window.markers_dock.isVisible())
        self.assertTrue(window.workspace_actions["Captions"].isChecked())
        self.assertFalse(window.apply_workspace("Nope"))

    def test_save_apply_delete_and_remember(self):
        window = self._window()
        window.apply_workspace("Audio")
        window.scopes_dock.show()  # a custom tweak
        self.assertTrue(window.save_workspace_as("Mine"))
        self.assertFalse(window.save_workspace_as("Color"))  # built-in names are taken
        self.assertIn("Mine", window.workspace_actions)
        window.apply_workspace("Editing")
        self.assertFalse(window.scopes_dock.isVisible())
        window.apply_workspace("Mine")
        QApplication.processEvents()
        self.assertTrue(window.scopes_dock.isVisible() and window.mixer_dock.isVisible())
        second = self._window()  # the choice is remembered for the next start
        self.assertEqual(second.current_workspace, "Mine")
        second.delete_workspace("Mine")
        self.assertNotIn("Mine", second.workspace_actions)
        self.assertEqual(second.current_workspace, "Editing")


if __name__ == "__main__":
    unittest.main()
