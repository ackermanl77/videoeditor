#!/usr/bin/env python3
"""Application entry point.

Run with ``python main.py`` during development, or launch the packaged
executable built by PyInstaller (see build.spec / README.md).
"""

from __future__ import annotations

import logging
import os
import sys

# Ensure the project root is importable whether launched as `python main.py`
# from another directory or bundled by PyInstaller.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QObject, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QIcon, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QSplashScreen  # noqa: E402

from core import __version__, diagnostics, update_check  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.theme import DARK_STYLESHEET  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("video_editor")

APP_ID = "OpenVideoEditor.VideoEditor.1"  # Windows AppUserModelID
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icons")


class _GuiThreadRelay(QObject):
    """Hands messages from any thread to the GUI thread (a queued signal hops threads safely;
    a QMessageBox can't): uncaught-exception reports and a found update."""

    error = Signal(str)
    update = Signal(object)

    def __init__(self, window: MainWindow):
        super().__init__()
        self.error.connect(lambda text: QMessageBox.critical(window, "Open Video Editor Error", text),
                           Qt.QueuedConnection)
        self.update.connect(window.show_update, Qt.QueuedConnection)


def _set_windows_app_id() -> None:
    """Give the app its own taskbar identity on Windows.

    Without this, a PyInstaller-packaged Python GUI app is grouped under
    (and shows the icon of) the generic Python/pythonw process in the
    Windows taskbar instead of using our own icon. This is a no-op on any
    other OS.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:  # noqa: BLE001 - purely cosmetic, never fatal
        pass


def _show_splash(app: QApplication) -> QSplashScreen | None:
    splash_path = os.path.join(ASSETS_DIR, "splash.png")
    if not os.path.exists(splash_path):
        return None
    pixmap = QPixmap(splash_path)
    if pixmap.isNull():
        return None
    splash = QSplashScreen(pixmap, Qt.WindowStaysOnTopHint)
    splash.setWindowFlag(Qt.FramelessWindowHint)
    splash.show()
    app.processEvents()
    return splash


def project_argument(argv: list[str]) -> str | None:
    """The .vproj path given on the command line, if any (the first one wins).

    Explorer passes it as the only argument when a project is double-clicked
    (installer/VideoEditor.iss registers .vproj); options start with "-".
    """
    for arg in argv[1:]:
        if not arg.startswith("-") and arg.lower().endswith(".vproj"):
            return os.path.abspath(arg)
    return None


#: Media types Explorer's "Edit with Open Video Editor" passes (F38, installer/VideoEditor.iss).
MEDIA_ARGUMENT_EXTENSIONS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".mpg", ".mpeg", ".mts",
                             ".m2ts", ".mp3", ".wav", ".m4a", ".flac", ".ogg")


def media_arguments(argv: list[str]) -> list[str]:
    """Media files given on the command line ("Edit with Open Video Editor" in Explorer), existing ones only."""
    return [os.path.abspath(arg) for arg in argv[1:]
            if not arg.startswith("-") and arg.lower().endswith(MEDIA_ARGUMENT_EXTENSIONS) and os.path.isfile(arg)]


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        # Checks the bundled FFmpeg, required filters and a real one-second
        # export, then exits -- no QApplication, no window (core/selftest.py).
        from core.selftest import run_self_test

        return run_self_test()

    _set_windows_app_id()
    # The windowed build has no console: log to a file (core/diagnostics.py).
    diagnostics.configure_logging()

    QApplication.setApplicationName("Open Video Editor")
    QApplication.setApplicationDisplayName("Open Video Editor")
    QApplication.setOrganizationName("OpenVideoEditor")

    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_STYLESHEET)

    icon_path = os.path.join(ASSETS_DIR, "app_icon.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    splash = _show_splash(app)

    ffmpeg_missing = False
    try:
        from core.ffmpeg_utils import find_binary

        find_binary("ffmpeg")
    except FileNotFoundError:
        ffmpeg_missing = True
        log.warning(
            "FFmpeg was not found. Media import, thumbnails, preview and export "
            "will not work until FFmpeg is installed and available on PATH."
        )

    window = MainWindow()
    window.show()
    relay = _GuiThreadRelay(window)
    # Any uncaught exception (a Qt slot, a worker thread) is logged and shown in a dialog.
    diagnostics.install_excepthooks(relay.error.emit)
    # Does nothing until core/update_check.py's UPDATE_FEED_URL is set.
    update_check.check_in_background(__version__, relay.update.emit)

    project_path = project_argument(sys.argv)
    if project_path:
        # Queued after the window's own autosave-recovery offer (also a
        # zero-delay timer), so a recovered session still gets the
        # "discard unsaved changes?" question before the file replaces it.
        QTimer.singleShot(0, lambda: window.open_project_file(project_path))
    media_paths = media_arguments(sys.argv)
    if media_paths and not project_path:
        # F38: imported into the media bin of the new project, after any autosave-recovery offer.
        QTimer.singleShot(0, lambda: window.import_media_paths(media_paths))

    if splash is not None:
        splash.finish(window)

    if ffmpeg_missing:
        QMessageBox.warning(
            window,
            "FFmpeg Not Found",
            "FFmpeg could not be found on this system.\n\n"
            "Media import, thumbnails, preview playback and video export all "
            "depend on FFmpeg. Please install it and make sure it is on your "
            "PATH (or place ffmpeg/ffprobe next to this application), then "
            "restart the app.\n\n"
            "Download: https://ffmpeg.org/download.html",
        )

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
