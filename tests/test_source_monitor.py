"""Source monitor (UI6): preview a bin file, mark In/Out, Insert / Overwrite / Append that part."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from core.media_asset import MediaAsset
    from ui.source_monitor import SourceMonitor, format_seconds, marked_range

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PYSIDE6_AVAILABLE = False


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class SourceMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_marked_range(self):
        self.assertIsNone(marked_range(10.0, None, None))
        self.assertEqual(marked_range(10.0, 2.0, None), (2.0, 10.0))
        self.assertEqual(marked_range(10.0, None, 4.0), (0.0, 4.0))
        self.assertEqual(marked_range(10.0, -1.0, 99.0), (0.0, 10.0))
        self.assertIsNone(marked_range(10.0, 5.0, 5.05))
        self.assertEqual(format_seconds(75.5), "01:15.50")

    def test_marks_follow_the_position_and_an_in_after_the_out_starts_over(self):
        monitor = SourceMonitor()
        self.addCleanup(monitor.deleteLater)
        changes = []
        monitor.range_changed.connect(lambda asset_id, marked: changes.append((asset_id, marked)))
        monitor.open_asset("a1", "/nonexistent/clip.mp4", 12.0, "clip.mp4")
        monitor.seek(3.0)
        monitor.set_in()
        monitor.seek(8.0)
        monitor.set_out()
        self.assertEqual(monitor.marked(), (3.0, 8.0))
        self.assertEqual(changes[-1], ("a1", (3.0, 8.0)))
        self.assertIn("In 00:03.00", monitor.range_label.text())
        monitor.set_in(9.0)  # after the Out: the Out goes
        self.assertEqual(monitor.marked(), (9.0, 12.0))
        monitor.clear_marks()
        self.assertEqual(changes[-1], ("a1", None))
        monitor.open_asset("a1", "/nonexistent/clip.mp4", 12.0, "clip.mp4", (1.0, 2.5))
        self.assertEqual((monitor.mark_in, monitor.marked(), monitor.position()), (1.0, (1.0, 2.5), 1.0))

    def test_the_window_places_the_marked_part_and_remembers_it_for_insert(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), \
                patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        self.addCleanup(window.deleteLater)
        asset = MediaAsset(path=os.path.join(home, "clip.mp4"), duration=20.0, has_video=True, has_audio=False,
                           width=640, height=360)
        window.project.media_pool.append(asset)
        window._refresh_media_bin()

        # Double-click: a video opens in the monitor instead of being appended.
        window.media_bin.list_widget.setCurrentRow(0)
        window.media_bin._on_item_double_clicked(window.media_bin.list_widget.item(0))
        monitor = window.source_monitor
        self.assertEqual(monitor.asset_id, asset.id)
        self.assertEqual(sum(len(t.clips) for t in window.project.tracks), 0)

        monitor.set_in(4.0)
        monitor.set_out(6.5)
        self.assertEqual(window.source_ranges[asset.id], (4.0, 6.5))
        monitor._place("append")
        clip = next(c for t in window.project.tracks for c in t.clips)
        self.assertEqual((clip.source_in, clip.source_out), (4.0, 6.5))

        # Edit > Insert with the file selected in the bin uses the same marks.
        window.timeline.set_playhead(0.0)
        window._place_selected_asset(False)
        clips = sorted((c for t in window.project.tracks for c in t.clips), key=lambda c: c.timeline_start)
        self.assertEqual(len(clips), 2)
        self.assertEqual((clips[0].source_in, clips[0].source_out), (4.0, 6.5))


if __name__ == "__main__":
    unittest.main()
