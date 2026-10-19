"""Premiere-style commands (F28): Lift, Extract, Add Edit to All Tracks, Extend Edit, Match Frame."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType
from core.edit_ops import (
    build_add_edit_all_tracks, build_extend_edit_command, build_extract_command, build_lift_command, match_frame,
)
from core.history import History
from core.media_asset import MediaAsset
from core.project import Project
from core.track import TrackType

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PYSIDE6_AVAILABLE = False


def _project():
    """V1: a [0-4] b [4-8] c [10-14];  A1: d [0-12]."""
    project = Project()
    video = next(t for t in project.tracks if t.track_type == TrackType.VIDEO)
    audio = next(t for t in project.tracks if t.track_type == TrackType.AUDIO)
    for name, start in (("a", 0.0), ("b", 4.0), ("c", 10.0)):
        video.clips.append(Clip(media_type=MediaType.VIDEO, name=name, source_path=f"/m/{name}.mp4",
                                source_in=0, source_out=4, timeline_start=start, source_duration=20.0))
    audio.clips.append(Clip(media_type=MediaType.AUDIO, name="d", source_path="/m/d.wav", source_in=0, source_out=12,
                            timeline_start=0, source_duration=60.0))
    return project, video, audio


def _spans(track):
    return [(c.name, round(c.timeline_start, 3), round(c.timeline_end, 3)) for c in track.sorted_clips()]


class LiftExtractTests(unittest.TestCase):
    def test_lift_leaves_a_gap_on_every_unlocked_track_and_undoes(self):
        project, video, audio = _project()
        history = History()
        history.execute(build_lift_command(project, 2.0, 6.0))
        self.assertEqual(_spans(video), [("a", 0, 2), ("b", 6, 8), ("c", 10, 14)])
        self.assertEqual(_spans(audio), [("d", 0, 2), ("d", 6, 12)])
        self.assertAlmostEqual(video.sorted_clips()[1].source_in, 2.0)  # b keeps the frames after Out
        history.undo()
        self.assertEqual(_spans(video), [("a", 0, 4), ("b", 4, 8), ("c", 10, 14)])
        self.assertEqual(_spans(audio), [("d", 0, 12)])

    def test_extract_closes_the_gap_and_spares_locked_tracks(self):
        project, video, audio = _project()
        audio.locked = True
        history = History()
        history.execute(build_extract_command(project, 2.0, 6.0))
        self.assertEqual(_spans(video), [("a", 0, 2), ("b", 2, 4), ("c", 6, 10)])
        self.assertEqual(_spans(audio), [("d", 0, 12)])  # locked: untouched
        history.undo()
        self.assertEqual(_spans(video), [("a", 0, 4), ("b", 4, 8), ("c", 10, 14)])

    def test_empty_ranges_are_refused(self):
        project, _video, _audio = _project()
        self.assertIsNone(build_lift_command(project, 3.0, 3.0))
        self.assertIsNone(build_lift_command(project, 14.5, 15.5))  # past every clip


class AddEditExtendTests(unittest.TestCase):
    def test_add_edit_cuts_every_track_under_the_playhead(self):
        project, video, audio = _project()
        History().execute(build_add_edit_all_tracks(project, 11.0))
        self.assertEqual(_spans(video)[-2:], [("c", 10, 11), ("c", 11, 14)])
        self.assertEqual(_spans(audio), [("d", 0, 11), ("d", 11, 12)])
        self.assertIsNone(build_add_edit_all_tracks(project, 30.0))

    def test_extend_edit_rolls_touching_edges_and_trims_free_ones(self):
        project, video, _audio = _project()
        a, b, c = video.sorted_clips()
        History().execute(build_extend_edit_command(project, a.id, 5.0))  # a|b touch: roll
        self.assertEqual(_spans(video)[:2], [("a", 0, 5), ("b", 5, 8)])
        History().execute(build_extend_edit_command(project, b.id, 9.5))  # free right edge: extend into the gap
        self.assertEqual(_spans(video)[1], ("b", 5, 9.5))
        self.assertIsNone(build_extend_edit_command(project, b.id, 12.0))  # would run into c
        History().execute(build_extend_edit_command(project, c.id, 12.0))  # nearer the end: trim c's right edge
        self.assertEqual(_spans(video)[2], ("c", 10, 12))


class MatchFrameTests(unittest.TestCase):
    def test_source_time_of_the_frame_under_the_playhead(self):
        project, video, _audio = _project()
        clip, source_t = match_frame(project, 5.5)
        self.assertEqual((clip.name, round(source_t, 3)), ("b", 1.5))
        video.sorted_clips()[1].reverse = True
        self.assertAlmostEqual(match_frame(project, 5.5)[1], 2.5)  # reversed: counts back from source_out
        self.assertEqual(match_frame(project, 9.0)[0].name, "d")  # gap on video: the audio clip
        self.assertIsNone(match_frame(project, 30.0))


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class MainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_menu_shortcuts_and_match_frame_then_insert(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        self.addCleanup(window.deleteLater)
        from PySide6.QtGui import QAction

        shortcuts = {a.text(): a.shortcut().toString() for a in window.findChildren(QAction)}
        self.assertEqual((shortcuts["Lift (In to Out)"], shortcuts["Extract (In to Out)"], shortcuts["Match Frame"],
                          shortcuts["Extend Edit to Playhead"], shortcuts["Add Edit to All Tracks"]),
                         (";", "'", "F", "E", "Shift+S"))
        asset = MediaAsset(path="/m/a.mp4", duration=20.0, has_video=True, has_audio=False)
        window.project.add_media_asset(asset)
        video = next(t for t in window.project.tracks if t.track_type == TrackType.VIDEO)
        video.clips.append(Clip(media_type=MediaType.VIDEO, source_path=asset.path, source_in=3.0, source_out=8.0,
                                timeline_start=0.0, source_duration=20.0, asset_id=asset.id))
        window.timeline.rebuild()
        window.timeline.set_playhead(2.0)
        self.assertTrue(window.match_frame())
        self.assertEqual(window.timeline.matched_source, (asset.id, 5.0))
        window.timeline.set_playhead(10.0)
        self.assertTrue(window.timeline.insert_asset_at_playhead(asset))
        inserted = [c for c in video.clips if c.timeline_start == 10.0][0]
        self.assertAlmostEqual(inserted.source_in, 5.0)  # from the matched frame
        self.assertIsNone(window.timeline.matched_source)  # one use
        self.assertFalse(window.timeline.lift_in_out())  # no In/Out yet


if __name__ == "__main__":
    unittest.main()
