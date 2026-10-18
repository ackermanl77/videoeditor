"""Motion tracking (F18): FFmpeg find_rect follows a moving square; a clip above follows it."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType, ReframeMode
from core.media_asset import MediaAsset
from core.motion_track import (
    TrackPoint,
    follow_keyframes,
    frame_to_canvas,
    parse_track_log,
    template_box_pixels,
    track_object,
    track_size,
    trackable_problem,
)
from core.project import Project, ProjectSettings
from core.track import TrackType
from tests.integration import test_p2_real_ffmpeg as p2

FFMPEG = p2.FFMPEG
_TMP = None
MOVING = None


def _true_centre(t):
    """The square's centre (pixels of the 640x360 source) at source time ``t``."""
    return 70 + 150 * t, 130 + 30 * t


def setUpModule():
    global _TMP, MOVING
    if not FFMPEG:
        return
    _TMP = tempfile.mkdtemp(prefix="ve_track_")
    MOVING = os.path.join(_TMP, "moving.mp4")
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=3",
                    "-f", "lavfi", "-i", "color=c=red:s=60x60:r=30:d=3",
                    "-filter_complex", "[1]drawgrid=w=20:h=20:t=4:c=yellow[o];[0][o]overlay=x='40+t*150':y='100+t*30'",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", MOVING],
                   check=True, capture_output=True)


def tearDownModule():
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)


BOX = (40 / 640, 100 / 360, 60 / 640, 60 / 360)


class MotionTrackMathTests(unittest.TestCase):
    def test_the_log_is_parsed_into_centres(self):
        log = ["[Parsed_metadata_3 @ 0x1] frame:0    pts:0       pts_time:0\n",
               "[Parsed_metadata_3 @ 0x1] lavfi.rect.w=30\n", "lavfi.rect.h=30\n", "lavfi.rect.x=20\n",
               "lavfi.rect.y=50\n", "lavfi.rect.score=0.0001\n",
               "frame:1 pts:1 pts_time:0.0666667\n",  # not found: no rect lines
               "frame:2 pts:2 pts_time:0.133333\n", "lavfi.rect.w=30\n", "lavfi.rect.h=30\n",
               "lavfi.rect.x=40\n", "lavfi.rect.y=60\n"]
        points = parse_track_log(log, 1.0, (480, 270))
        self.assertEqual(len(points), 2)
        self.assertAlmostEqual(points[0].source_time, 1.0)
        self.assertAlmostEqual(points[0].x, 35 / 480)
        self.assertAlmostEqual(points[1].y, 75 / 270)

    def test_template_boxes_are_clamped_and_must_be_big_enough(self):
        self.assertEqual(track_size(1920, 1080), (480, 270))
        self.assertEqual(template_box_pixels((0.5, 0.5, 0.25, 0.25), (480, 270)), (240, 135, 120, 68))
        self.assertEqual(template_box_pixels((0.9, 0.9, 0.5, 0.5), (480, 270))[2:], (48, 27))
        with self.assertRaises(ValueError):
            template_box_pixels((0.5, 0.5, 0.005, 0.005), (480, 270))

    def test_frame_points_map_through_the_fit_and_transform(self):
        clip = Clip(media_type=MediaType.VIDEO)
        # a 4:3 source on a 16:9 canvas is pillarboxed: its width is 0.75 of the canvas
        self.assertEqual(frame_to_canvas(clip, 0.0, 1.0, 0.5, (640, 480), (1920, 1080)), (0.875, 0.5))
        clip.transform_scale, clip.transform_x = 0.5, 0.25
        x, y = frame_to_canvas(clip, 0.0, 1.0, 1.0, (1920, 1080), (1920, 1080))
        self.assertAlmostEqual(x, 0.5)
        self.assertAlmostEqual(y, 0.75)
        self.assertIsNone(trackable_problem(Clip(media_type=MediaType.VIDEO)))
        self.assertIn("Fill", trackable_problem(Clip(media_type=MediaType.VIDEO, reframe_mode=ReframeMode.FILL)))
        self.assertIn("rotated", trackable_problem(Clip(media_type=MediaType.VIDEO, transform_rotation=5)))
        self.assertIsNotNone(trackable_problem(Clip(media_type=MediaType.IMAGE)))

    def test_follower_keyframes_use_timeline_time_offset_and_are_thinned(self):
        tracked = Clip(media_type=MediaType.VIDEO, source_in=1.0, source_out=5.0, timeline_start=10.0, speed=2.0)
        follower = Clip(media_type=MediaType.IMAGE, source_out=1.5, timeline_start=10.5, transform_x=0.6,
                        transform_y=0.5)
        # straight-line motion, sampled every 0.1 s of source
        points = [TrackPoint(1.0 + i / 10, 0.2 + i / 100, 0.5, 0.0) for i in range(41)]
        keyframes = follow_keyframes(points, tracked, follower, (1920, 1080), (1920, 1080))
        self.assertEqual(len(keyframes), 2)  # a straight line needs only its ends
        (t0, x0, y0), (t1, x1, _y1) = keyframes
        # source 2.0 is on screen at timeline 10.5 (2x speed): the follower's first frame
        self.assertAlmostEqual(t0, 0.0, places=3)
        self.assertAlmostEqual((x0, y0), (0.6, 0.5))  # the follower keeps its own starting place
        self.assertAlmostEqual(t1, 1.5, places=3)
        self.assertAlmostEqual(x1 - x0, 0.03 * 10 * 1.0, places=3)  # 1.5 s at 2x = 3 s of source = 0.3
        centred = follow_keyframes(points, tracked, follower, (1920, 1080), (1920, 1080), keep_offset=False)
        self.assertAlmostEqual(centred[0][1], 0.3, places=3)


@unittest.skipUnless(FFMPEG, "FFmpeg not found")
class MotionTrackFfmpegTests(unittest.TestCase):
    def test_find_rect_follows_the_square(self):
        points = track_object(MOVING, 0.5, 3.0, 0.0, BOX, (640, 360), ffmpeg=FFMPEG)
        self.assertGreater(len(points), 30)
        self.assertAlmostEqual(points[0].source_time, 0.5, places=2)
        for point in points:
            x, y = _true_centre(point.source_time)
            self.assertLess(abs(point.x * 640 - x), 4)
            self.assertLess(abs(point.y * 360 - y), 4)

    def test_track_motion_writes_the_followers_position_keyframes(self):
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PySide6 not installed")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), \
                patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
            window._autosave_timer.stop()
            try:
                project = window.project
                project.settings = ProjectSettings(width=640, height=360, fps=30.0)
                asset = MediaAsset(path=MOVING, duration=3.0, width=640, height=360, has_video=True, has_audio=False)
                project.add_media_asset(asset)
                tracked = Clip(media_type=MediaType.VIDEO, source_path=MOVING, source_out=3.0, source_duration=3.0,
                               asset_id=asset.id, name="moving")
                project.tracks[0].clips.append(tracked)
                upper = next((t for t in project.tracks if t.track_type == TrackType.VIDEO and t is not project.tracks[0]),
                             None) or project.add_track(TrackType.VIDEO)
                follower = Clip(media_type=MediaType.VIDEO, source_path=MOVING, source_out=3.0, source_duration=3.0,
                                asset_id=asset.id, name="logo", transform_scale=0.2)
                upper.clips.append(follower)
                window.timeline.rebuild()
                window.timeline.select_clips([follower.id])
                window.timeline.set_playhead(0.0)
                self.assertTrue(window.track_motion(box=BOX, keep_offset=False))
                keyframes = project.find_clip(follower.id).position_keyframes
                self.assertGreaterEqual(len(keyframes), 2)
                for t, x, y in keyframes:
                    cx, cy = _true_centre(t)
                    self.assertLess(abs(x * 640 - cx), 5, (t, x))
                    self.assertLess(abs(y * 360 - cy), 5, (t, y))
                window.history.undo()
                self.assertEqual(project.find_clip(follower.id).position_keyframes, [])

                from PySide6.QtGui import QImage

                from ui.motion_track_dialog import MotionTrackDialog

                dialog = MotionTrackDialog(QImage(480, 270, QImage.Format.Format_RGB32), "moving", "logo", window)
                self.assertIsNone(dialog.picker.box())
                dialog.picker.set_box((0.25, 0.5, 0.5, 0.25))
                for got, want in zip(dialog.picker.box(), (0.25, 0.5, 0.5, 0.25)):
                    self.assertAlmostEqual(got, want, delta=0.005)
                self.assertTrue(dialog.keep_offset.isChecked())
                dialog.deleteLater()
            finally:
                window._clear_autosave()
                window.deleteLater()
                app.processEvents()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
