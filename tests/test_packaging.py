"""K1: onedir build + per-user installer, and the .vproj command-line argument.

These read the build files; nothing is built or installed here.
"""

from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8-sig") as f:
        return f.read()


class BuildLayoutTests(unittest.TestCase):
    def test_spec_builds_a_folder_not_a_single_file(self):
        spec = _read("build.spec")
        self.assertIn("exclude_binaries=True", spec)
        self.assertRegex(spec, r"coll = COLLECT\(\s*exe,\s*a\.binaries,\s*a\.zipfiles,\s*a\.datas")
        self.assertRegex(spec, r'COLLECT\([^)]*name="VideoEditor"')
        self.assertNotIn("runtime_tmpdir", spec)

    def test_installer_packages_that_folder_per_user_and_registers_vproj(self):
        iss = _read("installer", "VideoEditor.iss")
        self.assertIn(r'Source: "..\dist\VideoEditor\*"', iss)
        self.assertIn("PrivilegesRequired=lowest", iss)
        self.assertIn(r"DefaultDirName={localappdata}\Programs", iss)
        self.assertIn(r'Subkey: "Software\Classes\.vproj"', iss)
        self.assertIn('""{app}\\{#AppExe}"" ""%1""', iss)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "assets", "icons", "app_icon.ico")))
        # Every registry entry is per user.
        for line in iss.splitlines():
            if line.startswith("Root:"):
                self.assertTrue(line.startswith("Root: HKCU;"), line)

    def test_exe_resources_publisher_manifest_and_document_icon(self):
        """K8: one publisher (MELEJ), a UTF-8 + PerMonitorV2 manifest, 24 px and .vproj icons."""
        import struct
        import xml.dom.minidom

        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import version_info

        self.assertIn('u"CompanyName", u"MELEJ"', _read("version_info.txt"))
        self.assertIn("AppPublisher=MELEJ", _read("installer", "VideoEditor.iss"))
        self.assertIn(r'\{#AppExe},1"', _read("installer", "VideoEditor.iss"))  # DefaultIcon: the 2nd icon group
        spec = _read("build.spec")
        self.assertIn("manifest=manifest_path", spec)
        self.assertIn("icon=[icon_path, document_icon_path]", spec)
        dom = xml.dom.minidom.parseString(version_info.MANIFEST_XML)
        self.assertEqual(dom.getElementsByTagName("activeCodePage")[0].firstChild.data, "UTF-8")
        self.assertEqual(dom.getElementsByTagName("dpiAwareness")[0].firstChild.data, "PerMonitorV2")

        def ico_sizes(name):
            with open(os.path.join(ROOT, "assets", "icons", name), "rb") as f:
                data = f.read()
            count = struct.unpack_from("<H", data, 4)[0]
            return {data[6 + 16 * i] or 256 for i in range(count)}

        self.assertTrue({16, 24, 32, 48, 256} <= ico_sizes("app_icon.ico"))
        self.assertTrue({16, 24, 32, 48, 256} <= ico_sizes("vproj.ico"))

    def test_build_script_and_release_workflow_use_the_folder_and_installer(self):
        build = _read("build.py")
        self.assertIn("--installer", build)
        self.assertIn("/DAppVersion=", build)
        release = _read(".github", "workflows", "release.yml")
        self.assertIn("python build.py --installer", release)
        self.assertIn(r"dist\VideoEditor\VideoEditor.exe", release)
        self.assertIn("VideoEditor-*-setup.exe", release)
        self.assertNotRegex(release, r"dist[\\/]VideoEditor\.exe")


try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from main import project_argument

    _MAIN_IMPORTABLE = True
except ImportError:
    _MAIN_IMPORTABLE = False


@unittest.skipUnless(_MAIN_IMPORTABLE, "PySide6 is not installed in this interpreter")
class ProjectArgumentTests(unittest.TestCase):
    def test_first_vproj_argument_is_opened(self):
        path = project_argument(["VideoEditor.exe", r"C:\work\My Film.VPROJ", "other.vproj"])
        self.assertEqual(path, os.path.abspath(r"C:\work\My Film.VPROJ"))

    def test_options_and_other_files_are_ignored(self):
        self.assertIsNone(project_argument(["VideoEditor.exe"]))
        self.assertIsNone(project_argument(["VideoEditor.exe", "--self-test"]))
        self.assertIsNone(project_argument(["VideoEditor.exe", "clip.mp4"]))
        self.assertIsNone(project_argument(["VideoEditor.exe", "-x.vproj"]))


if __name__ == "__main__":
    unittest.main()
