"""Background preview renders of an In/Out range (F10c): rendered by the export
engine at preview size, played by the preview over the range, and invalidated
by any edit that touches the range."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType
from core.preview_render import (
    cached_render,
    preview_size,
    prune_cache,
    range_key,
    render_preview,
)
from core.project import Project

try:
    from core.ffmpeg_utils import find_binary, run

    FFMPEG = find_binary("ffmpeg")
    FFPROBE = find_binary("ffprobe")
except FileNotFoundError:  # pragma: no cover
    FFMPEG = FFPROBE = None

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from ui.preview_player import PreviewPlayer

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PYSIDE6_AVAILABLE = False


def _project(path="/m/a.mp4"):
    project = Project()
    project.settings.width, project.settings.height = 1280, 720
    first = Clip(media_type=MediaType.VIDEO, source_path=path, source_in=0.0, source_out=3.0, timeline_start=0.0,
                 source_duration=3.0)
    second = Clip(media_type=MediaType.VIDEO, source_path=path, source_in=0.0, source_out=3.0, timeline_start=20.0,
                  source_duration=3.0)
    project.tracks[0].clips.extend([first, second])
    return project, first, second


class RangeKeyTests(unittest.TestCase):
    def test_edits_inside_the_range_change_the_key_and_outside_ones_dont(self):
        project, first, second = _project()
        key = range_key(project, 0.0, 3.0)
        self.assertEqual(key, range_key(project, 0.0, 3.0))
        self.assertNotEqual(key, range_key(project, 0.0, 2.0))  # another range
        second.volume = 0.3  # 17 s past the range end: outside the margin
        self.assertEqual(key, range_key(project, 0.0, 3.0))
        first.volume = 0.4
        self.assertNotEqual(key, range_key(project, 0.0, 3.0))
        first.volume = 1.0
        self.assertEqual(key, range_key(project, 0.0, 3.0))  # back to the same state: same key (undo)
        project.tracks[0].muted = True
        self.assertNotEqual(key, range_key(project, 0.0, 3.0))
        project.tracks[0].muted = False
        project.settings.fps = 25.0
        self.assertNotEqual(key, range_key(project, 0.0, 3.0))

    def test_a_snapshot_has_the_same_key_as_the_live_project(self):
        project, _first, _second = _project()
        snapshot = Project.from_dict(json.loads(json.dumps(project.to_dict())))
        self.assertEqual(range_key(snapshot, 0.0, 3.0), range_key(project, 0.0, 3.0))

    def test_preview_size_keeps_the_aspect_at_540_lines(self):
        project, _f, _s = _project()
        self.assertEqual(preview_size(project), (960, 540))
        project.settings.width, project.settings.height = 1080, 1920
        self.assertEqual(preview_size(project), (304, 540))
        project.settings.width, project.settings.height = 640, 360
        self.assertEqual(preview_size(project), (640, 360))  # never upscaled

    def test_old_renders_are_pruned_least_recently_used_first(self):
        folder = tempfile.mkdtemp(prefix="ve_prune_")
        paths = []
        for i in range(5):
            path = os.path.join(folder, f"{i}.mp4")
            with open(path, "wb") as f:
                f.write(b"x")
            os.utime(path, (1000 + i, 1000 + i))
            paths.append(path)
        removed = prune_cache(folder, keep=paths[0], limit=2)
        self.assertEqual(sorted(removed), sorted(paths[1:3]))
        self.assertTrue(os.path.exists(paths[0]) and os.path.exists(paths[4]))


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg is not available")
class RealRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ve_render_")
        self.source = os.path.join(self.tmp, "src.mp4")
        run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-f", "lavfi",
             "-i", "sine=frequency=440", "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-shortest", self.source], timeout=120)
        self.project, self.first, _second = _project(self.source)
        self.cache = os.path.join(self.tmp, "renders")

    def test_a_range_renders_at_preview_size_and_is_reused(self):
        render = render_preview(self.project, 0.5, 2.5, cache_dir=self.cache)
        self.assertTrue(os.path.isfile(render.path))
        probe = run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                     "-of", "json", render.path], timeout=60)
        info = json.loads(probe.stdout)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (960, 540))
        self.assertTrue(any(s["codec_type"] == "audio" for s in info["streams"]))
        self.assertAlmostEqual(float(info["format"]["duration"]), 2.0, delta=0.15)
        self.assertEqual(cached_render(self.project, 0.5, 2.5, self.cache).path, render.path)
        with patch("core.preview_render.Exporter", side_effect=AssertionError("rendered again")):
            self.assertEqual(render_preview(self.project, 0.5, 2.5, cache_dir=self.cache).path, render.path)
        self.first.brightness = 0.3
        self.assertIsNone(cached_render(self.project, 0.5, 2.5, self.cache))


@unittest.skipUnless(_PYSIDE6_AVAILABLE and FFMPEG, "PySide6 or FFmpeg is not available")
class PreviewPlaysRendersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ve_render_ui_")
        self.project, self.first, self.second = _project(os.path.join(self.tmp, "missing.mp4"))
        self.render_file = os.path.join(self.tmp, "render.mp4")
        with open(self.render_file, "wb") as f:
            f.write(b"stand-in")
        from core.preview_render import RenderedRange

        self.render = RenderedRange(0.0, 3.0, self.render_file, range_key(self.project, 0.0, 3.0))
        self.preview = PreviewPlayer()
        self.addCleanup(self.preview.deleteLater)
        self.preview.set_project(self.project)
        self.signals = []
        self.preview.render_ranges_changed.connect(self.signals.append)

    def test_a_valid_render_stands_in_for_the_clips_over_its_range(self):
        self.preview.add_rendered_range(self.render)
        self.assertEqual(self.signals[-1], [(0.0, 3.0, True)])
        clip, track = self.preview._resolve_active_clip(1.0)
        self.assertEqual((clip.source_path, clip.timeline_start, clip.source_out, track), (self.render_file, 0.0, 3.0, None))
        self.assertIs(self.preview._resolve_active_clip(21.0)[0], self.second)  # outside: the real clip

    def test_an_edit_in_the_range_makes_it_stale_and_undo_restores_it(self):
        self.preview.add_rendered_range(self.render)
        self.first.speed = 2.0
        self.preview.refresh_after_edit()
        self.assertEqual(self.signals[-1], [(0.0, 3.0, False)])
        self.assertIs(self.preview._resolve_active_clip(1.0)[0], self.first)
        self.first.speed = 1.0
        self.preview.refresh_after_edit()
        self.assertEqual(self.signals[-1], [(0.0, 3.0, True)])
        self.preview.set_project(Project())
        self.assertEqual(self.signals[-1], [])


@unittest.skipUnless(_PYSIDE6_AVAILABLE and FFMPEG, "PySide6 or FFmpeg is not available")
class MainWindowRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_render_preview_runs_in_the_background_and_marks_the_ruler(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        source = os.path.join(home, "src.mp4")
        run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", source], timeout=120)
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), \
                patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
            window._autosave_timer.stop()
            self.addCleanup(window.deleteLater)
            clip = Clip(media_type=MediaType.VIDEO, source_path=source, source_in=0.0, source_out=2.0,
                        timeline_start=0.0, source_duration=2.0)
            window.project.tracks[0].clips.append(clip)
            window.preview.set_project(window.project)
            self.assertTrue(window.render_preview_range())
            for _ in range(600):
                QApplication.processEvents()
                if window._preview_render_exporter is None:
                    break
                time.sleep(0.05)
            self.assertIn("ready", window.status_bar.currentMessage())
            self.assertEqual(window.timeline.ruler_item.render_ranges, [(0.0, 2.0, True)])
            self.assertTrue(window.preview._resolve_active_clip(1.0)[0].source_path.endswith(".mp4"))
            self.assertTrue(window.preview._resolve_active_clip(1.0)[0].id.startswith("render-"))
            # A second request for the same state reuses the file at once.
            self.assertTrue(window.render_preview_range())
            self.assertIn("up to date", window.status_bar.currentMessage())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
