"""Ctrl+K command search (F27): collected from the real menu bar, filtered, and run."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QAction, QKeyEvent
    from PySide6.QtWidgets import QApplication, QMenuBar

    from ui.command_palette import CommandPalette, collect_commands, filter_commands

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover - PySide6 missing
    _PYSIDE6_AVAILABLE = False


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class CommandPaletteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _menu_bar(self):
        bar = QMenuBar()
        self.addCleanup(bar.deleteLater)
        edit = bar.addMenu("&Edit")
        self.split = edit.addAction("Split Clip at Playhead")
        self.split.setShortcut("S")
        edit.addSeparator()
        disabled = edit.addAction("Paste &Attributes")
        disabled.setEnabled(False)
        playback = bar.addMenu("&Playback")
        playback.addAction("Next Frame\tRight")
        gpu = playback.addMenu("GPU Decoding")
        gpu.addAction("Auto")
        return bar

    def test_collects_every_action_with_its_path_and_shortcut(self):
        commands = collect_commands(self._menu_bar())
        self.assertEqual([c.path for c in commands], [
            "Edit > Split Clip at Playhead", "Edit > Paste Attributes", "Playback > Next Frame", "Playback > GPU Decoding > Auto",
        ])
        self.assertEqual(commands[0].shortcut, "S")
        self.assertEqual(commands[2].shortcut, "Right")  # the display-only key hint
        self.assertFalse(commands[1].enabled)

    def test_filtering_matches_every_word_and_ranks_name_prefixes_first(self):
        commands = collect_commands(self._menu_bar())
        self.assertEqual([c.name for c in filter_commands(commands, "play split")], ["Split Clip at Playhead"])
        self.assertEqual([c.name for c in filter_commands(commands, "gpu")], ["Auto"])  # matched by its menu path
        self.assertEqual(filter_commands(commands, "nothing here"), [])
        self.assertEqual(len(filter_commands(commands, "")), 4)

    def test_enter_runs_the_selected_enabled_action(self):
        bar = self._menu_bar()
        fired = []
        self.split.triggered.connect(lambda: fired.append(True))
        palette = CommandPalette(bar)
        self.addCleanup(palette.deleteLater)
        palette.search.setText("paste")
        self.assertEqual(palette.results.count(), 1)
        palette._accept_current()  # the only match is disabled: nothing is chosen
        self.assertIsNone(palette.chosen)
        palette.search.setText("split")
        palette.search.returnPressed.emit()
        self.assertIs(palette.chosen, self.split)
        palette.search.setText("")
        QApplication.sendEvent(palette.search, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Down, Qt.NoModifier))
        self.assertEqual(palette.results.currentItem().text(), "Playback > Next Frame    Right")  # skipped the disabled row

    def test_main_window_has_ctrl_k_and_finds_its_menus(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        self.addCleanup(window.deleteLater)
        actions = {action.text(): action for action in window.findChildren(QAction)}
        self.assertEqual(actions["Command Search..."].shortcut().toString(), "Ctrl+K")
        paths = [c.path for c in collect_commands(window.menuBar())]
        self.assertIn("Edit > Split Clip at Playhead", paths)
        self.assertIn("Help > Command Search...", paths)
        def search_and_run(palette):
            palette.search.setText("add text clip")
            palette._accept_current()
            return 0

        def text_clips():
            return sum(1 for track in window.project.tracks for clip in track.clips if clip.media_type.value == "text")

        before = text_clips()
        with patch("ui.command_palette.CommandPalette.exec", search_and_run):
            window.show_command_palette()
        self.assertEqual(text_clips(), before + 1)  # Edit > Add Text Clip ran

if __name__ == "__main__":
    unittest.main()
