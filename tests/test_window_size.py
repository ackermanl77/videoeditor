"""The main window fits small laptop screens (UI4): no fixed 1200x760 minimum, and a first-run
size bounded by the screen's work area."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRect, QSettings, QSize
    from PySide6.QtWidgets import QApplication

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PYSIDE6_AVAILABLE = False


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class WindowSizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = tempfile.mkdtemp(prefix="ve_settings_")
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, folder)
        self.addCleanup(QSettings.setDefaultFormat, QSettings.Format.NativeFormat)
        QSettings().clear()

    def test_default_size_stays_inside_a_1366x768_work_area(self):
        from ui.main_window import DEFAULT_WINDOW_SIZE, default_window_size

        laptop = MagicMock()
        laptop.availableGeometry.return_value = QRect(0, 0, 1366, 728)  # 768 minus the taskbar
        size = default_window_size(laptop)
        self.assertLessEqual(size.width(), 1366)
        self.assertLessEqual(size.height(), 728)
        big = MagicMock()
        big.availableGeometry.return_value = QRect(0, 0, 3840, 2100)
        self.assertEqual(default_window_size(big), DEFAULT_WINDOW_SIZE)
        self.assertEqual(default_window_size(None), DEFAULT_WINDOW_SIZE)

    def test_no_fixed_minimum_beyond_a_laptop_screen(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        self.addCleanup(window.deleteLater)
        self.assertEqual(window.minimumSize(), QSize(0, 0))  # only the layout's own minimum applies
        self.assertLessEqual(window.minimumSizeHint().height(), 700)


if __name__ == "__main__":
    unittest.main()
