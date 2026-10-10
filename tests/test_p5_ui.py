"""Styled ASS/SSA subtitle import in the main window, and the Properties panel's note on a styled line."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from core.history import History
    from core.project import Project
    from ui.properties_panel import PropertiesPanel

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover - PySide6 missing
    _PYSIDE6_AVAILABLE = False

SCRIPT = """[Script Info]
PlayResY: 360

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, Alignment
Style: Default,Arial,24,&H0000FFFF,2

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,One
Dialogue: 0,0:00:02.00,0:00:04.00,Default,,0,0,0,,{\\an8}Two
"""


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class StyledSubtitleImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        QApplication.clipboard().clear()

    def _window(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), \
                patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        self.addCleanup(window.deleteLater)
        return window

    def _ass(self):
        path = os.path.join(tempfile.mkdtemp(prefix="ve_ass_"), "subs.ass")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SCRIPT)
        return path

    def test_keep_styling_imports_styled_lines_over_two_tracks_in_one_undo_step(self):
        window = self._window()
        tracks_before = len(window.project.tracks)
        self.assertEqual(window.import_subtitle_file(self._ass(), keep_styling=True), 2)
        subtitle_tracks = [t for t in window.project.tracks if t.name.startswith("Subtitles")]
        self.assertEqual([t.name for t in subtitle_tracks], ["Subtitles", "Subtitles 2"])
        clips = [c for t in subtitle_tracks for c in t.clips]
        self.assertTrue(all(c.ass_event is not None for c in clips))
        self.assertEqual(len(window.project.subtitle_scripts), 1)
        self.assertIn(clips[0].ass_event.script_id, window.project.subtitle_scripts)
        window.history.undo()
        self.assertEqual(len(window.project.tracks), tracks_before)

    def test_plain_text_keeps_the_old_import_and_cancel_imports_nothing(self):
        window = self._window()
        self.assertEqual(window.import_subtitle_file(self._ass(), keep_styling=False), 2)
        clips = [c for t in window.project.tracks if t.name.startswith("Subtitles") for c in t.clips]
        self.assertTrue(all(c.ass_event is None for c in clips))
        with patch.object(window, "_ask_keep_subtitle_styling", return_value=None):
            self.assertEqual(window.import_subtitle_file(self._ass()), 0)
        with patch.object(window, "_ask_keep_subtitle_styling", return_value=True) as ask:
            self.assertEqual(window.import_subtitle_file(self._ass()), 2)
        ask.assert_called_once()

    def test_properties_panel_notes_a_styled_line(self):
        from core.subtitles import split_ass_script, styled_subtitle_tracks

        project = Project()
        styled = styled_subtitle_tracks(split_ass_script(SCRIPT), "s1")[0][0]
        project.tracks[0].clips.append(styled)
        panel = PropertiesPanel()
        self.addCleanup(panel.deleteLater)
        panel.set_project(project, History(validator=project.validate))
        panel.set_selected_clip(styled.id)
        self.assertFalse(panel.styled_note.isHidden())
        plain = type(styled)(media_type=styled.media_type, timeline_start=10.0, source_out=1.0)
        project.tracks[0].clips.append(plain)
        panel.set_selected_clip(plain.id)
        self.assertTrue(panel.styled_note.isHidden())


if __name__ == "__main__":
    unittest.main()
