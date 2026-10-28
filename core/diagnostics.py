"""Logging and last-resort error reporting for the windowed app.

The packaged editor has no console (build.spec: console=False), so anything printed to
stderr -- an uncaught exception in a Qt slot, a worker thread's traceback -- used to vanish.
`configure_logging` writes a rotating log under ~/.video_editor/logs (next to the autosave
folder) plus a faulthandler dump for hard crashes, and `install_excepthooks` logs every
uncaught exception, on the main thread or any other, and hands a short message to a callback
(main.py shows it in a dialog). Pure stdlib, so it is testable without Qt.
"""
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import sys
import threading
from typing import Callable, Optional

LOG_FILE_NAME = "video_editor.log"
CRASH_FILE_NAME = "crash.log"
_MAX_LOG_BYTES = 1_000_000
_BACKUP_COUNT = 3

_log_file: Optional[str] = None
_crash_file = None  # kept open for faulthandler
log = logging.getLogger("video_editor")


def default_log_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".video_editor", "logs")


def configure_logging(log_dir: Optional[str] = None, level: int = logging.INFO,
                      crash_dump: bool = True) -> Optional[str]:
    """Send every logger to a rotating file in ``log_dir`` (default: default_log_dir()) as well
    as stderr, and (``crash_dump``) faulthandler's dump of a native crash to crash.log there.
    Returns the log file, or None if the folder can't be created: logging must never stop the
    app from starting."""
    global _log_file, _crash_file
    directory = default_log_dir() if log_dir is None else log_dir
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if not any(type(handler) is logging.StreamHandler for handler in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, LOG_FILE_NAME)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=_MAX_LOG_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
    except OSError:
        return None
    handler.setFormatter(formatter)
    for existing in list(root.handlers):
        if isinstance(existing, logging.handlers.RotatingFileHandler):
            root.removeHandler(existing)
            existing.close()
    root.addHandler(handler)
    _log_file = path
    try:
        # A native crash (in Qt, a decoder...) skips Python's excepthook; faulthandler still
        # writes the Python stack of every thread.
        if crash_dump and _crash_file is None:
            _crash_file = open(os.path.join(directory, CRASH_FILE_NAME), "a", encoding="utf-8")  # noqa: SIM115
            faulthandler.enable(file=_crash_file)
    except (OSError, RuntimeError, ValueError):
        pass
    return path


def log_file_hint() -> str:
    """Where the log is, for error dialogs."""
    return _log_file if _log_file is not None else "(logging to a file is not enabled)"


def install_excepthooks(on_error: Optional[Callable[[str], None]] = None) -> None:
    """Log uncaught exceptions from the main thread (sys.excepthook) and from other threads
    (threading.excepthook), then call on_error(message). A failing on_error is logged, never
    raised."""

    def _report(exc_type, exc_value, exc_traceback, where: str) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        log.critical("uncaught exception %s", where, exc_info=(exc_type, exc_value, exc_traceback))
        if on_error is not None:
            try:
                on_error(
                    f"Open Video Editor hit an unexpected error: {exc_type.__name__}: {exc_value}\n\n"
                    f"Save your project under a new name before going on. Details were saved to:\n"
                    f"{log_file_hint()}"
                )
            except Exception:  # noqa: BLE001 -- the error reporter must never raise
                log.exception("error callback failed")

    sys.excepthook = lambda t, v, tb: _report(t, v, tb, "on the main thread")
    threading.excepthook = lambda args: _report(
        args.exc_type, args.exc_value, args.exc_traceback, f"in thread {getattr(args.thread, 'name', '?')}"
    )
