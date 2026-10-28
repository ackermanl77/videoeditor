"""MainWindow.show_update: a found update is announced and opens the download page only on Yes."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from core.update_check import UpdateInfo
    from ui.main_window import MainWindow

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover - PySide6 missing
    _PYSIDE6_AVAILABLE = False


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class UpdateNoticeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _show(self, answer):
        window = MagicMock()
        info = UpdateInfo("9.0.0", "https://example.com/get", "Faster export")
        with patch("ui.main_window.QMessageBox.question", return_value=answer) as question, \
                patch("ui.main_window.QDesktopServices.openUrl") as open_url:
            MainWindow.show_update(window, info)
        return window, question, open_url

    def test_yes_opens_the_download_page(self):
        window, question, open_url = self._show(QMessageBox.Yes)
        self.assertIn("9.0.0", window.status_bar.showMessage.call_args.args[0])
        self.assertIn("Faster export", question.call_args.args[2])
        self.assertEqual(open_url.call_args.args[0].toString(), "https://example.com/get")

    def test_no_leaves_it(self):
        _window, _question, open_url = self._show(QMessageBox.No)
        open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
