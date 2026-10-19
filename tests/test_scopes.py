"""Video scopes (F24): FFmpeg's waveform / parade / vectorscope of the timeline frame, and the dock."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from core.scopes import SCOPES, ScopeError, render_scope, scope_filter, timeline_scope
from tests.integration import test_p2_real_ffmpeg as p2

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover - PySide6 missing
    _PYSIDE6_AVAILABLE = False


def _png(source: str, size: str = "320x180") -> bytes:
    command = [p2.FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"{source}{':' if '=' in source else '='}s={size}:d=1", "-frames:v", "1",
               "-f", "image2pipe", "-c:v", "png", "pipe:1"]
    return subprocess.run(command, check=True, capture_output=True).stdout


class FilterTests(unittest.TestCase):
    def test_every_scope_has_a_filter(self):
        self.assertEqual(set(SCOPES), {"waveform", "parade", "vectorscope"})
        self.assertIn("display=parade", scope_filter("parade"))
        self.assertIn("vectorscope", scope_filter("vectorscope"))
        with self.assertRaises(ValueError):
            scope_filter("histogram")


@unittest.skipUnless(p2.FFMPEG and _PYSIDE6_AVAILABLE, "FFmpeg or PySide6 not available")
class RealScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _image(self, png: bytes) -> QImage:
        image = QImage()
        self.assertTrue(image.loadFromData(png, "PNG"))
        return image

    def _lit_rows(self, image: QImage, column: int):
        return [y for y in range(image.height()) if max(image.pixelColor(column, y).getRgb()[:3]) > 80]

    def test_waveform_puts_a_bright_frame_high_and_a_dark_frame_low(self):
        bright = self._image(render_scope(_png("color=c=0xE0E0E0"), "waveform"))
        dark = self._image(render_scope(_png("color=c=0x202020"), "waveform"))
        self.assertEqual((bright.width(), bright.height()), (320, 256))
        column = bright.width() // 2
        bright_rows = [y for y in self._lit_rows(bright, column) if y not in self._lit_rows(dark, column)]
        dark_rows = [y for y in self._lit_rows(dark, column) if y not in self._lit_rows(bright, column)]
        self.assertTrue(bright_rows and dark_rows)
        self.assertLess(max(bright_rows), min(dark_rows))  # 0-255 bottom to top

    def test_parade_has_three_panels_and_the_vectorscope_a_square(self):
        parade = self._image(render_scope(_png("testsrc2"), "parade"))
        self.assertEqual((parade.width(), parade.height()), (960, 256))
        red = self._image(render_scope(_png("color=c=red"), "parade"))
        # A pure red frame: the red panel's trace sits high, the green and blue ones at the bottom.
        lit = [self._lit_rows(red, x) for x in (160, 480, 800)]
        self.assertLess(min(lit[0]), 40)
        self.assertGreater(min(lit[1]), 200)
        self.assertGreater(min(lit[2]), 200)
        vector = self._image(render_scope(_png("testsrc2"), "vectorscope"))
        self.assertEqual((vector.width(), vector.height()), (256, 256))

    def test_bad_input_raises_a_scope_error(self):
        with self.assertRaises(ScopeError):
            render_scope(b"not a png", "waveform")

    def test_timeline_scope_measures_the_composited_frame(self):
        from core.clip import Clip, MediaType
        from core.media_asset import MediaAsset
        from core.project import Project

        folder = tempfile.mkdtemp(prefix="ve_scopes_")
        path = os.path.join(folder, "white.mp4")
        subprocess.run([p2.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=320x180:r=30:d=2",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", path], check=True)
        project = Project()
        project.add_media_asset(MediaAsset(path=path, duration=2.0, has_video=True, has_audio=False, width=320, height=180))
        project.tracks[0].clips.append(Clip(media_type=MediaType.VIDEO, source_path=path, source_in=0, source_out=2, timeline_start=0))
        image = self._image(timeline_scope(project, 1.0, "waveform"))
        rows = self._lit_rows(image, image.width() // 2)
        self.assertLess(min(rows), 40)  # white sits at the top of the waveform


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_has_the_dock_and_it_shows_the_scope(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        self.addCleanup(window.deleteLater)
        self.assertEqual(window.scopes_action.shortcut().toString(), "Ctrl+Alt+S")
        panel = window.scopes_panel
        fake_png = _png("color=c=gray") if p2.FFMPEG else None
        if fake_png is None:
            self.skipTest("FFmpeg not available")
        calls = []
        with patch("ui.scopes_panel.timeline_scope", lambda project, t, kind: calls.append((t, kind)) or fake_png), \
                patch.object(type(window.project), "total_duration", property(lambda _self: 5.0)):
            window.scopes_dock.show()
            panel.kind_combo.setCurrentIndex(panel.kind_combo.findData("vectorscope"))
            panel._timer.stop()
            panel._start_render()
            deadline = time.time() + 5
            while not panel.has_image and time.time() < deadline:
                QApplication.processEvents()
                time.sleep(0.01)
        self.assertTrue(panel.has_image)
        self.assertEqual(calls[-1][1], "vectorscope")
        stale = panel._generation
        panel.schedule_refresh()
        panel._on_scope_ready(stale, "old error")  # superseded: ignored
        self.assertTrue(panel.has_image)


if __name__ == "__main__":
    unittest.main()
