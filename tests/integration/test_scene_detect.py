"""Scene-cut detection (F19) with the real FFmpeg: three 2-second shots."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType
from core.history import History
from core.media_asset import MediaAsset
from core.project import Project, ProjectSettings
from core.scene_detect import (
    SceneDetectionCancelled,
    build_scene_cut_command,
    detect_scene_cuts,
    timeline_offset_for_source,
)
from tests.integration import test_p2_real_ffmpeg as p2

FFMPEG = p2.FFMPEG
_TMP = None
SHOTS = None


def setUpModule():
    global _TMP, SHOTS
    if not FFMPEG:
        return
    _TMP = tempfile.mkdtemp(prefix="ve_scenes_")
    SHOTS = os.path.join(_TMP, "shots.mp4")
    subprocess.run([FFMPEG, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=c=red:s=320x180:r=25:d=2",
                    "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=2",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=25:d=2",
                    "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0", "-c:v", "libx264", "-preset", "ultrafast", SHOTS],
                   check=True, capture_output=True)


def tearDownModule():
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)


def _project(**clip_kw):
    asset = MediaAsset(path=SHOTS, duration=6.0, width=320, height=180, has_video=True, has_audio=False)
    clip = Clip(media_type=MediaType.VIDEO, source_path=SHOTS, source_in=0.0, source_out=6.0, timeline_start=1.0,
                source_duration=6.0, asset_id=asset.id, **clip_kw)
    project = Project()
    project.settings = ProjectSettings(width=320, height=180, fps=25.0)
    project.add_media_asset(asset)
    project.tracks[0].clips.append(clip)
    return project, clip


@unittest.skipUnless(FFMPEG, "FFmpeg not found")
class SceneDetectTests(unittest.TestCase):
    def test_cuts_are_found_in_the_clips_own_source_range(self):
        self.assertEqual(detect_scene_cuts(SHOTS, ffmpeg=FFMPEG), [2.0, 4.0])
        self.assertEqual(detect_scene_cuts(SHOTS, 2.3, 6.0, ffmpeg=FFMPEG), [4.0])  # absolute source time
        self.assertEqual(detect_scene_cuts(SHOTS, 0.0, 2.2, ffmpeg=FFMPEG), [])  # a cut 0.2 s from the end: dropped
        with self.assertRaises(SceneDetectionCancelled):
            detect_scene_cuts(SHOTS, should_cancel=lambda: True, ffmpeg=FFMPEG)

    def test_source_times_map_through_speed_and_reverse(self):
        _project_, clip = _project()
        self.assertAlmostEqual(timeline_offset_for_source(clip, 2.0), 2.0, places=3)
        _project_, fast = _project(speed=2.0)
        self.assertAlmostEqual(timeline_offset_for_source(fast, 2.0), 1.0, places=3)
        _project_, back = _project(reverse=True)
        self.assertAlmostEqual(timeline_offset_for_source(back, 2.0), 4.0, places=3)
        self.assertIsNone(timeline_offset_for_source(clip, 9.0))

    def test_split_and_markers_are_one_undo_step_each(self):
        project, clip = _project()
        history = History()
        history.execute(build_scene_cut_command(project, clip.id, [2.0, 4.0], "split"))
        pieces = sorted(project.tracks[0].clips, key=lambda c: c.timeline_start)
        self.assertEqual([(round(c.timeline_start, 3), round(c.source_in, 3)) for c in pieces], [(1.0, 0.0), (3.0, 2.0), (5.0, 4.0)])
        self.assertEqual(pieces[0].id, clip.id)
        history.undo()
        self.assertEqual(len(project.tracks[0].clips), 1)
        history.execute(build_scene_cut_command(project, clip.id, [2.0, 4.0], "markers"))
        self.assertEqual([(round(m.time, 3), m.name) for m in project.markers], [(3.0, "Cut 1"), (5.0, "Cut 2")])
        history.undo()
        self.assertEqual(project.markers, [])
        self.assertIsNone(build_scene_cut_command(project, clip.id, [0.1], "split"))  # nothing inside

    def test_the_edit_menu_action_runs_end_to_end(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        home = tempfile.mkdtemp(prefix="ve_home_")
        # The whole test runs with a temporary home: the window's autosave (and the start-up offer to
        # recover one) must never touch the real ~/.video_editor -- a test killed mid-run left an
        # orphaned autosave there once, which every later window offered in a blocking dialog.
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}),                 patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
            window._autosave_timer.stop()
            try:
                source_project, clip = _project()
                window.project.add_media_asset(source_project.asset_for_clip(clip))
                window.project.tracks[0].clips.append(clip)
                window.timeline.rebuild()
                window.timeline.selected_clip_id = clip.id
                with patch("core.scene_detect.find_binary", return_value=FFMPEG):
                    self.assertTrue(window.detect_scene_cuts(choice="split"))
                self.assertEqual(len(window.project.tracks[0].clips), 3)
                window.history.undo()
                self.assertEqual(len(window.project.tracks[0].clips), 1)
            finally:
                window._clear_autosave()
                window.deleteLater()
                QApplication.clipboard().clear()
                app.processEvents()
        shutil.rmtree(home, ignore_errors=True)
