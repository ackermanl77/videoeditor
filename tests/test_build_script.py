"""build.py's release hygiene (K7): the pinned Python version and code signing."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build  # noqa: E402


class PythonVersionTests(unittest.TestCase):
    def test_the_python_version_file_is_read(self):
        self.assertEqual(build.expected_python(), (3, 14))

    def test_a_build_with_another_python_stops_before_pyinstaller(self):
        with mock.patch.object(build, "expected_python", return_value=(2, 7)), \
                mock.patch.object(sys, "argv", ["build.py"]), mock.patch.object(build.subprocess, "run") as run:
            self.assertEqual(build.main(), 1)
            run.assert_not_called()

    def test_any_python_skips_the_check(self):
        with mock.patch.object(build, "expected_python", return_value=(2, 7)), \
                mock.patch.object(sys, "argv", ["build.py", "--any-python"]), \
                mock.patch.object(build.shutil, "which", return_value=None), \
                mock.patch.dict(sys.modules, {"PyInstaller": None}):
            self.assertEqual(build.main(), 1)  # got past the check, stopped at "PyInstaller is not installed"


class SigningTests(unittest.TestCase):
    def test_the_signtool_command_signs_sha256_with_an_rfc3161_timestamp(self):
        cmd = build.sign_command("signtool.exe", "ABC123", "app.exe", "http://ts.example")
        self.assertEqual(cmd, ["signtool.exe", "sign", "/sha1", "ABC123", "/fd", "SHA256", "/tr", "http://ts.example", "/td", "SHA256", "app.exe"])

    def test_signing_needs_a_thumbprint(self):
        with mock.patch.dict(os.environ, {"VIDEO_EDITOR_SIGN_THUMBPRINT": ""}):
            self.assertEqual(build.sign("app.exe"), 1)

    def test_signing_reports_signtools_exit_code(self):
        result = mock.Mock(returncode=0)
        with mock.patch.dict(os.environ, {"VIDEO_EDITOR_SIGN_THUMBPRINT": "AB CD", "VIDEO_EDITOR_TIMESTAMP_URL": ""}), \
                mock.patch.object(build, "find_signtool", return_value="signtool.exe"), \
                mock.patch.object(build.subprocess, "run", return_value=result) as run:
            self.assertEqual(build.sign("app.exe"), 0)
            self.assertEqual(run.call_args[0][0][3], "ABCD")  # spaces in a pasted thumbprint removed
            self.assertIn(build.DEFAULT_TIMESTAMP_URL, run.call_args[0][0])
            result.returncode = 2
            self.assertEqual(build.sign("app.exe"), 2)

    def test_without_signtool_signing_fails_clearly(self):
        with mock.patch.dict(os.environ, {"VIDEO_EDITOR_SIGN_THUMBPRINT": "AB"}), \
                mock.patch.object(build, "find_signtool", return_value=None):
            self.assertEqual(build.sign("app.exe"), 1)


if __name__ == "__main__":
    unittest.main()
