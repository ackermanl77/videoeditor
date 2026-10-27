"""UI2 of the 2026-10-22 UI audit: the track header toggles are icons, and -- since graphics items
are invisible to screen readers -- the same toggles are checkable actions (header context menu,
Timeline > Selected Track). Offscreen Qt; skipped without PySide6."""

from __future__ import annotations

import os
import unittest

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from core.history import History
    from core.project import Project
    from ui.widgets.timeline_items import TrackHeaderItem, draw_track_icon
    from ui.widgets.timeline_widget import TimelineWidget

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PYSIDE6_AVAILABLE = False


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 not installed")
class TrackHeaderIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.project = Project()
        self.history = History()
        self.timeline = TimelineWidget()
        self.timeline.set_project(self.project, self.history)

    def test_header_toggles_are_icons_with_tooltips(self):
        headers = [item for item in self.timeline.scene.items() if isinstance(item, TrackHeaderItem)]
        self.assertTrue(headers)
        header = headers[0]
        icons = [header.mute_btn.icon, header.lock_btn.icon, header.hide_btn.icon, header.duck_btn.icon]
        self.assertEqual(icons, ["mute", "lock", "hide", "duck"])
        self.assertIn("Mute track audio", header.mute_btn.toolTip())

    def test_every_icon_draws_something(self):
        for kind in ("mute", "lock", "hide", "duck"):
            for active in (False, True):
                image = QImage(18, 18, QImage.Format_ARGB32)
                image.fill(0)
                painter = QPainter(image)
                draw_track_icon(painter, kind, QRectF(0, 0, 18, 18), QColor("white"), active)
                painter.end()
                lit = sum(1 for x in range(18) for y in range(18) if image.pixelColor(x, y).alpha() > 0)
                self.assertGreater(lit, 10, (kind, active))

    def test_selected_track_actions_toggle_the_flags_undoably(self):
        track = self.project.tracks[0]
        self.timeline.selected_track_id = track.id
        for flag in ("muted", "locked", "hidden"):
            with self.subTest(flag=flag):
                self.timeline.toggle_selected_track(flag)
                self.assertTrue(getattr(self.project.find_track(track.id), flag))
                self.history.undo()
                self.assertFalse(getattr(self.project.find_track(track.id), flag))

    def test_no_selected_track_is_a_no_op(self):
        self.timeline.selected_track_id = None
        self.timeline.toggle_selected_track("muted")
        self.assertFalse(any(track.muted for track in self.project.tracks))


if __name__ == "__main__":
    unittest.main()
