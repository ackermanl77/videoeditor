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
        with mock.patch.dict(os.environ, {"VIDEO_EDITOR_SIGN_THUMBPRINT": "", "CODESIGN_THUMBPRINT": ""}):
            self.assertEqual(build.sign("app.exe"), 1)

    def test_signing_reports_signtools_exit_code(self):
        result = mock.Mock(returncode=0)
        with mock.patch.dict(os.environ, {"VIDEO_EDITOR_SIGN_THUMBPRINT": "AB CD", "VIDEO_EDITOR_TIMESTAMP_URL": "", "CODESIGN_TIMESTAMP_URL": ""}), \
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


    def test_the_shared_codesign_variables_are_the_fallback(self):
        env = {"VIDEO_EDITOR_SIGN_THUMBPRINT": "", "CODESIGN_THUMBPRINT": "EF01",
               "VIDEO_EDITOR_TIMESTAMP_URL": "", "CODESIGN_TIMESTAMP_URL": "http://ts.shared"}
        with mock.patch.dict(os.environ, env), mock.patch.object(build, "find_signtool", return_value="signtool.exe"):
            self.assertEqual(build.signing_settings(), ("signtool.exe", "EF01", "http://ts.shared"))

    def test_inno_setup_gets_a_codesign_tool_that_signs_installer_and_uninstaller(self):
        args = build.inno_sign_args(r"C:\Kits\signtool.exe", "ABC", "http://ts.example")
        self.assertEqual(args[0], "/DSign")
        self.assertEqual(args[1], r"/Scodesign=$qC:\Kits\signtool.exe$q sign /sha1 ABC /fd SHA256 /tr http://ts.example /td SHA256 $f")
        with open(os.path.join(build.ROOT, "installer", "VideoEditor.iss"), encoding="utf-8-sig") as handle:
            iss = handle.read()
        self.assertIn("#ifdef Sign\nSignTool=codesign\nSignedUninstaller=yes\n#endif", iss.replace("\r\n", "\n"))

    def test_a_signed_installer_build_passes_the_tool_to_iscc(self):
        with mock.patch.object(build, "find_iscc", return_value="ISCC.exe"),                 mock.patch.object(build, "signing_settings", return_value=("signtool.exe", "ABC", "http://ts")),                 mock.patch.object(sys, "argv", ["build.py", "--installer", "--sign"]),                 mock.patch.object(build.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(build.build_installer(), 0)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "ISCC.exe")
        self.assertIn("/DSign", cmd)
        self.assertTrue(cmd[-1].endswith("VideoEditor.iss"))


if __name__ == "__main__":
    unittest.main()
