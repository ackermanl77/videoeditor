"""Close Gaps, Select Left/Right of Playhead and the Razor's box blade (UI9)."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType
from core.edit_ops import (
    build_box_blade_command, build_close_all_gaps_command, build_close_track_gaps_command, clips_beside_playhead,
    common_gaps,
)
from core.history import History
from core.project import Project
from core.track import TrackType

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QApplication

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PYSIDE6_AVAILABLE = False


def _project():
    """V1: a [1-5] b [7-11] c [14-18];  A1: d [0-3] e [7-9]."""
    project = Project()
    video = next(t for t in project.tracks if t.track_type == TrackType.VIDEO)
    audio = next(t for t in project.tracks if t.track_type == TrackType.AUDIO)
    for name, start in (("a", 1.0), ("b", 7.0), ("c", 14.0)):
        video.clips.append(Clip(media_type=MediaType.VIDEO, name=name, source_path=f"/m/{name}.mp4",
                                source_in=0, source_out=4, timeline_start=start, source_duration=20.0))
    for name, start, length in (("d", 0.0, 3.0), ("e", 7.0, 2.0)):
        audio.clips.append(Clip(media_type=MediaType.AUDIO, name=name, source_path=f"/m/{name}.wav", source_in=0,
                                source_out=length, timeline_start=start, source_duration=60.0))
    return project, video, audio


def _spans(track):
    return [(c.name, round(c.timeline_start, 3), round(c.timeline_end, 3)) for c in track.sorted_clips()]


class CloseGapsTests(unittest.TestCase):
    def test_one_track_packs_its_clips_and_leaves_the_others(self):
        project, video, audio = _project()
        history = History()
        history.execute(build_close_track_gaps_command(project, video.id))
        self.assertEqual(_spans(video), [("a", 0, 4), ("b", 4, 8), ("c", 8, 12)])
        self.assertEqual(_spans(audio), [("d", 0, 3), ("e", 7, 9)])
        history.undo()
        self.assertEqual(_spans(video), [("a", 1, 5), ("b", 7, 11), ("c", 14, 18)])

    def test_all_tracks_removes_only_what_is_empty_everywhere(self):
        project, video, audio = _project()
        # Empty on every track: 5-7 (d ends at 3, a at 5) and 11-14.
        self.assertEqual(common_gaps(project), [(5.0, 7.0), (11.0, 14.0)])
        history = History()
        history.execute(build_close_all_gaps_command(project))
        self.assertEqual(_spans(video), [("a", 1, 5), ("b", 5, 9), ("c", 9, 13)])
        self.assertEqual(_spans(audio), [("d", 0, 3), ("e", 5, 7)])  # still under b, in sync
        self.assertIsNone(build_close_all_gaps_command(project))
        history.undo()
        self.assertEqual(_spans(audio), [("d", 0, 3), ("e", 7, 9)])

    def test_a_locked_track_neither_moves_nor_blocks(self):
        project, video, audio = _project()
        audio.locked = True
        self.assertIsNone(build_close_track_gaps_command(project, audio.id))
        self.assertEqual(common_gaps(project), [(0.0, 1.0), (5.0, 7.0), (11.0, 14.0)])
        History().execute(build_close_all_gaps_command(project))
        self.assertEqual(_spans(video), [("a", 0, 4), ("b", 4, 8), ("c", 8, 12)])
        self.assertEqual(_spans(audio), [("d", 0, 3), ("e", 7, 9)])


class SelectBesideAndBladeTests(unittest.TestCase):
    def test_select_right_and_left_of_the_playhead(self):
        project, video, audio = _project()
        names = lambda ids: sorted(project.find_clip(i).name for i in ids)  # noqa: E731
        self.assertEqual(names(clips_beside_playhead(project, 7.0, "right")), ["b", "c", "e"])
        self.assertEqual(names(clips_beside_playhead(project, 8.0, "left")), ["a", "d"])  # b and e cross 8
        with self.assertRaises(ValueError):
            clips_beside_playhead(project, 1.0, "up")

    def test_box_blade_cuts_the_spanned_tracks_once_each(self):
        project, video, audio = _project()
        history = History()
        history.execute(build_box_blade_command(project, [video.id, audio.id], 8.0))
        self.assertEqual(_spans(video)[1:3], [("b", 7, 8), ("b", 8, 11)])
        self.assertEqual(_spans(audio)[1:], [("e", 7, 8), ("e", 8, 9)])
        history.undo()
        self.assertEqual(len(video.clips) + len(audio.clips), 5)
        self.assertIsNone(build_box_blade_command(project, [video.id], 6.0))  # nothing under 6 s on V1


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class TimelineUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_menu_commands_and_a_razor_drag_across_tracks(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), \
                patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        self.addCleanup(window.deleteLater)
        project, video, audio = _project()
        window.project = project
        window.timeline.set_project(project, window.history)
        timeline = window.timeline
        names = {a.text().replace("&", "") for a in window.findChildren(type(window.split_action))}
        for name in ("Close Gaps on Track", "Close Gaps on All Tracks", "Select Right of Playhead", "Select Left of Playhead"):
            self.assertIn(name, names)

        timeline.set_playhead(7.0)
        self.assertEqual(timeline.select_beside_playhead("right"), 3)
        self.assertTrue(timeline.close_all_gaps())
        self.assertEqual(_spans(audio), [("d", 0, 3), ("e", 5, 7)])

        # Razor drag from the V1 row down into the A1 row at 6 s: both clips under 6 s are cut.
        timeline.set_razor_mode(True)
        rows = timeline._row_tracks
        top = timeline.row_layout.top(rows.index(video)) + 5
        bottom = timeline.row_layout.top(rows.index(audio)) + 5
        from ui.widgets.timeline_widget import HEADER_WIDTH

        x = HEADER_WIDTH + 6.0 * timeline.pixels_per_second
        self.assertTrue(timeline.box_blade(QPointF(x, top), QPointF(x, bottom)))
        self.assertEqual(len(video.clips), 4)
        self.assertEqual(len(audio.clips), 3)

        # The same through the mouse: a press on empty V1 space at 0.5 s, released on A1, cuts d.
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        window.resize(1200, 760)
        window.show()
        view = timeline.view
        press = view.mapFromScene(QPointF(HEADER_WIDTH + 0.5 * timeline.pixels_per_second, top))
        release = view.mapFromScene(QPointF(HEADER_WIDTH + 0.5 * timeline.pixels_per_second, bottom))
        QTest.mousePress(view.viewport(), Qt.LeftButton, Qt.NoModifier, press)
        QTest.mouseRelease(view.viewport(), Qt.LeftButton, Qt.NoModifier, release)
        self.assertEqual([c.name for c in audio.sorted_clips()][:2], ["d", "d"])
        self.assertEqual(audio.sorted_clips()[0].timeline_end, 0.5)


if __name__ == "__main__":
    unittest.main()
