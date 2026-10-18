"""The Markers panel with notes, and restyling every caption on a track (F15)."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType, TextStyle
from core.commands import AddMarkerCommand, EditMarkerNoteCommand
from core.history import History
from core.project import Marker, Project


class MarkerNoteTests(unittest.TestCase):
    def test_notes_round_trip_and_undo(self):
        project = Project()
        marker = project.add_marker(2.0, "Intro")
        self.assertNotIn("note", marker.to_dict())  # no key for an empty note
        history = History()
        history.execute(EditMarkerNoteCommand(project, marker.id, "Check the audio here"))
        self.assertEqual(Marker.from_dict(marker.to_dict()).note, "Check the audio here")
        history.undo()
        self.assertEqual(project.find_marker(marker.id).note, "")
        self.assertEqual(Marker.from_dict({"time": 1.0}).note, "")


class _WindowTest(unittest.TestCase):
    def setUp(self):
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PySide6 not installed")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        self.home = tempfile.mkdtemp(prefix="ve_home_")
        patcher = patch.dict(os.environ, {"USERPROFILE": self.home, "HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        nvenc = patch("ui.main_window.nvenc_available", return_value=False)
        nvenc.start()
        self.addCleanup(nvenc.stop)
        from ui.main_window import MainWindow

        self.window = MainWindow()
        self.window._autosave_timer.stop()

    def tearDown(self):
        self.window._clear_autosave()
        self.window.deleteLater()
        self.app.processEvents()
        shutil.rmtree(self.home, ignore_errors=True)


class MarkersPanelTests(_WindowTest):
    def test_the_list_follows_edits_seeks_and_edits_in_place(self):
        window, panel = self.window, self.window.markers_panel
        self.assertEqual(panel.table.rowCount(), 0)
        window.history.execute(AddMarkerCommand(window.project, 4.0, "Outro"))
        window.history.execute(AddMarkerCommand(window.project, 1.0, "Intro"))
        self.assertEqual([panel.table.item(r, 2).text() for r in range(2)], ["Intro", "Outro"])  # time order
        seeks = []
        panel.seek_requested.connect(seeks.append)
        panel.seek_to_row(1)
        self.assertEqual(seeks, [4.0])
        panel.table.item(0, 3).setText("Fade in from black")  # the note cell
        intro = next(m for m in window.project.markers if m.name == "Intro")
        self.assertEqual(intro.note, "Fade in from black")
        panel.table.item(0, 2).setText("Opening")
        self.assertEqual(intro.name, "Opening")
        panel.table.item(0, 2).setText("   ")  # an empty name is refused
        self.assertEqual(intro.name, "Opening")
        self.assertEqual(panel.table.item(0, 2).text(), "Opening")
        window.undo()  # the rename
        self.assertEqual(panel.table.item(0, 2).text(), "Intro")
        panel.table.selectRow(1)
        self.assertTrue(panel.delete_selected())
        self.assertEqual([m.name for m in window.project.markers], ["Intro"])
        self.assertFalse(window.markers_dock.isVisible())
        self.assertEqual(window.markers_action.text(), "Markers Panel")


class TrackStyleTests(_WindowTest):
    def test_apply_to_track_restyles_every_caption_in_one_step(self):
        window = self.window
        track = window.project.tracks[0]
        captions = [Clip(media_type=MediaType.TEXT, name=f"c{i}", text_style=TextStyle(content=f"line {i}"),
                         source_out=1.0, timeline_start=float(i), source_duration=1.0) for i in range(3)]
        track.clips += captions
        window.timeline.rebuild()
        window.timeline.select_clips([captions[1].id])
        panel = window.properties
        self.assertEqual(panel.apply_title_style_to_track("Caption"), 3)
        for i, clip in enumerate(captions):
            style = window.project.find_clip(clip.id).text_style
            self.assertEqual((style.content, style.box_enabled), (f"line {i}", True))
        self.assertEqual(panel.apply_title_style_to_track("Caption"), 0)  # nothing left to change
        window.history.undo()
        self.assertFalse(any(window.project.find_clip(c.id).text_style.box_enabled for c in captions))
        self.assertEqual(panel.apply_title_style_to_track("No Such Style"), 0)


if __name__ == "__main__":
    unittest.main()
