"""scripts/fetch_ffmpeg.py: the pinned-FFmpeg installer used by CI and the release (K4).

Runs offline against a tiny fake archive -- no download.
"""

import hashlib
import importlib.util
import os
import shutil
import tempfile
import unittest
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("fetch_ffmpeg", os.path.join(_ROOT, "scripts", "fetch_ffmpeg.py"))
fetch_ffmpeg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_ffmpeg)


class FetchFfmpegTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ve_fetch_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.zip_path = os.path.join(self.tmp, "ff.zip")
        with zipfile.ZipFile(self.zip_path, "w") as z:
            z.writestr("ffmpeg-x/bin/ffmpeg.exe", b"ffmpeg-bytes")
            z.writestr("ffmpeg-x/bin/ffprobe.exe", b"ffprobe-bytes")
        self.lock = {
            "sha256": fetch_ffmpeg.sha256_of(self.zip_path),
            "binaries": {
                "ffmpeg.exe": hashlib.sha256(b"ffmpeg-bytes").hexdigest(),
                "ffprobe.exe": hashlib.sha256(b"ffprobe-bytes").hexdigest(),
            },
        }

    def test_verified_archive_installs_its_binaries(self):
        dest = os.path.join(self.tmp, "bin")
        installed = fetch_ffmpeg.install_from_zip(self.zip_path, self.lock, dest)
        self.assertEqual(sorted(os.path.basename(p) for p in installed), ["ffmpeg.exe", "ffprobe.exe"])

    def test_archive_hash_mismatch_installs_nothing(self):
        self.lock["sha256"] = "0" * 64
        dest = os.path.join(self.tmp, "bin")
        with self.assertRaises(ValueError):
            fetch_ffmpeg.install_from_zip(self.zip_path, self.lock, dest)
        self.assertFalse(os.path.exists(dest))

    def test_binary_hash_mismatch_is_rejected(self):
        self.lock["binaries"]["ffprobe.exe"] = "0" * 64
        with self.assertRaises(ValueError):
            fetch_ffmpeg.install_from_zip(self.zip_path, self.lock, os.path.join(self.tmp, "bin"))

    def test_repo_lock_is_pinned(self):
        lock = fetch_ffmpeg.load_lock()
        self.assertIn(lock["version"], lock["url"])
        self.assertRegex(lock["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(set(lock["binaries"]), {"ffmpeg.exe", "ffprobe.exe"})


if __name__ == "__main__":
    unittest.main()
