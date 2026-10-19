"""Tracked effect masks (F32): the track data, the geq expression rendered by FFmpeg, the conversion
from tracked points, and the Track Mask command."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType, ReframeMode
from core.effect_masks import clean_mask, clean_track, masked_filter, tracked_mask_expr
from core.effects import EffectInstance
from core.media_asset import MediaAsset
from core.motion_track import TrackPoint, mask_track_keyframes
from core.project import Project
from tests.integration import test_p2_real_ffmpeg as p2

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PYSIDE6_AVAILABLE = False

MOVING = {"shape": "ellipse", "cx": 0.5, "cy": 0.5, "w": 0.3, "h": 0.5, "angle": 0, "feather": 0.02, "invert": False,
          "track": [[0.0, 0.2, 0.5], [2.0, 0.8, 0.5]]}


class DataTests(unittest.TestCase):
    def test_clean_track_keeps_a_valid_sorted_list(self):
        self.assertEqual(clean_track([[1, 0.5, 0.5], [0, 0.1, 0.2], ["x", 1, 1], [0.5]]), [[0.0, 0.1, 0.2], [1.0, 0.5, 0.5]])
        self.assertEqual(clean_track([[0, 0.5, 0.5]]), [])  # one point isn't a track
        self.assertEqual(clean_mask(MOVING)["track"], MOVING["track"])
        self.assertNotIn("track", clean_mask({**MOVING, "track": None}))
        restored = EffectInstance.from_dict(EffectInstance(kind="color_correction", params={"brightness": 0.3}, mask=MOVING).to_dict())
        self.assertEqual(restored.mask["track"], MOVING["track"])

    def test_tracked_masks_use_geq_static_ones_the_cached_image(self):
        self.assertIn("geq=lum='255*clip(", masked_filter("eq=brightness=0.3", clean_mask(MOVING), 16 / 9))
        self.assertIn("movie=filename=", masked_filter("eq=brightness=0.3", clean_mask({**MOVING, "track": None}), 16 / 9))
        self.assertIn("(T+1.500000)", masked_filter("eq", clean_mask(MOVING), 16 / 9, t_offset=1.5))
        self.assertIn("(1-clip(", tracked_mask_expr(clean_mask({**MOVING, "invert": True})))

    def test_points_become_frame_fractions_in_clip_time(self):
        clip = Clip(media_type=MediaType.VIDEO, source_path="/m/a.mp4", source_in=10.0, source_out=14.0, timeline_start=3.0, speed=2.0)
        points = [TrackPoint(10.0, 0.2, 0.4, 1), TrackPoint(12.0, 0.55, 0.5, 1), TrackPoint(14.0, 0.8, 0.6, 1)]
        self.assertEqual(mask_track_keyframes(points, clip, (1920, 1080), (1920, 1080), tolerance=0),
                         [[0.0, 0.2, 0.4], [1.0, 0.55, 0.5], [2.0, 0.8, 0.6]])  # 2x speed: 4 s of source in 2 s
        clip.reframe_mode = ReframeMode.FILL  # a 16:9 source on a 9:16 canvas: the centre third is shown
        filled = mask_track_keyframes(points, clip, (1920, 1080), (1080, 1920), tolerance=0)
        self.assertAlmostEqual(filled[1][1], 0.5 + 0.05 / 0.31640625, places=3)
        self.assertLess(filled[0][1], 0.0)  # x 0.2 of the source lies left of the visible crop


@unittest.skipUnless(p2.FFMPEG and _PYSIDE6_AVAILABLE, "FFmpeg or PySide6 not available")
class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_the_brightened_area_moves_with_the_track(self):
        from core.frame_render import render_frame

        folder = tempfile.mkdtemp(prefix="ve_tracked_")
        path = os.path.join(folder, "grey.mp4")
        subprocess.run([p2.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x404040:s=320x180:r=25:d=3",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", path], check=True)
        project = Project()
        project.settings.width, project.settings.height = 320, 180
        project.add_media_asset(MediaAsset(path=path, duration=3.0, has_video=True, has_audio=False, width=320, height=180))
        clip = Clip(media_type=MediaType.VIDEO, source_path=path, source_in=0, source_out=3, timeline_start=0, source_duration=3.0)
        clip.effects = [EffectInstance(kind="color_correction", params={"brightness": 0.5}, mask=clean_mask(MOVING))]
        project.tracks[0].clips.append(clip)

        def brightness(t, x):
            image = QImage()
            self.assertTrue(image.loadFromData(render_frame(project, t, (320, 180)), "PNG"))
            return image.pixelColor(int(x * 319), 90).lightness()

        self.assertGreater(brightness(0.0, 0.2), brightness(0.0, 0.8) + 60)  # starts on the left
        self.assertGreater(brightness(2.0, 0.8), brightness(2.0, 0.2) + 60)  # ends on the right
        self.assertGreater(brightness(1.0, 0.5), brightness(1.0, 0.15) + 60)  # in the middle half way


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class CommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_track_mask_sets_the_track_in_one_undo_step(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        self.addCleanup(window.deleteLater)
        asset = MediaAsset(path="/m/a.mp4", duration=4.0, has_video=True, has_audio=False, width=640, height=360)
        window.project.add_media_asset(asset)
        clip = Clip(media_type=MediaType.VIDEO, source_path=asset.path, source_in=0, source_out=4, timeline_start=0,
                    source_duration=4.0, asset_id=asset.id)
        clip.effects = [EffectInstance(kind="blur", params={"sigma": 8}, mask=clean_mask({"shape": "ellipse", "w": 0.2, "h": 0.3}))]
        window.project.tracks[0].clips.append(clip)
        window.timeline.rebuild()
        window.timeline.select_clips([clip.id])
        window.properties.set_selected_clip(clip.id)
        points = [TrackPoint(t / 10, 0.3 + t / 100, 0.5, 1) for t in range(0, 41)]
        with patch("core.motion_track.track_object", return_value=points):
            self.assertTrue(window.track_effect_mask(0, box=(0.25, 0.4, 0.1, 0.2)))
        track = clip.effects[0].mask["track"]
        self.assertEqual((track[0][1], track[-1][1]), (0.3, 0.7))
        self.assertEqual(clip.effects[0].mask["w"], 0.2)  # the shape is kept
        window.undo()
        self.assertNotIn("track", clip.effects[0].mask)


if __name__ == "__main__":
    unittest.main()
