"""core.diagnostics: the log file the windowed app writes, and uncaught exceptions from any
thread ending up in it and in the error callback."""

import logging
import logging.handlers
import os
import sys
import tempfile
import threading
import unittest

from core import diagnostics


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self.saved_handlers = list(self.root.handlers)
        self.saved_level = self.root.level
        self.saved_hooks = (sys.excepthook, threading.excepthook)
        self.folder = tempfile.TemporaryDirectory()

    def tearDown(self):
        for handler in list(self.root.handlers):
            if handler not in self.saved_handlers:
                self.root.removeHandler(handler)
                handler.close()
        for handler in self.saved_handlers:
            if handler not in self.root.handlers:
                self.root.addHandler(handler)
        self.root.setLevel(self.saved_level)
        sys.excepthook, threading.excepthook = self.saved_hooks
        self.folder.cleanup()

    def test_configure_logging_writes_every_logger_to_a_file(self):
        path = diagnostics.configure_logging(self.folder.name, crash_dump=False)
        self.assertEqual(path, os.path.join(self.folder.name, diagnostics.LOG_FILE_NAME))
        logging.getLogger("core.exporter").warning("export went wrong")
        for handler in self.root.handlers:
            handler.flush()
        with open(path, encoding="utf-8") as log:
            self.assertIn("export went wrong", log.read())
        self.assertEqual(diagnostics.log_file_hint(), path)

    def test_configure_logging_never_raises_when_the_folder_cannot_be_created(self):
        blocker = os.path.join(self.folder.name, "a-file")
        with open(blocker, "w", encoding="utf-8"):
            pass
        self.assertIsNone(diagnostics.configure_logging(os.path.join(blocker, "logs"), crash_dump=False))

    def test_uncaught_exceptions_are_logged_and_reported_from_any_thread(self):
        path = diagnostics.configure_logging(self.folder.name, crash_dump=False)
        messages = []
        diagnostics.install_excepthooks(messages.append)
        try:
            raise ValueError("main thread boom")
        except ValueError:
            sys.excepthook(*sys.exc_info())

        def worker():
            raise RuntimeError("worker boom")

        thread = threading.Thread(target=worker, name="render")
        thread.start()
        thread.join()
        for handler in self.root.handlers:
            handler.flush()
        with open(path, encoding="utf-8") as log:
            text = log.read()
        self.assertIn("main thread boom", text)
        self.assertIn("worker boom", text)
        self.assertIn("in thread render", text)
        self.assertEqual(len(messages), 2)
        self.assertIn("ValueError: main thread boom", messages[0])
        self.assertIn(path, messages[0])

    def test_a_failing_error_callback_is_logged_not_raised(self):
        diagnostics.configure_logging(self.folder.name, crash_dump=False)

        def broken(message):
            raise OSError("no dialog")

        diagnostics.install_excepthooks(broken)
        try:
            raise ValueError("boom")
        except ValueError:
            sys.excepthook(*sys.exc_info())  # must not raise


if __name__ == "__main__":
    unittest.main()
