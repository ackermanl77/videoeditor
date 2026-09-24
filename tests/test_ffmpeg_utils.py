"""Tests for core/ffmpeg_utils.py: the NVENC availability probe, and the
subprocess helpers' output decoding (plus core/media_probe.py's handling
of unreadable files).

nvenc_available() shells out to `ffmpeg -encoders` plus a tiny test encode,
so most of these tests mock find_binary()/run() rather than depending on
this machine's actual GPU or ffmpeg build -- the same approach
test_exporter.py uses for the exporter's own subprocess calls. The
non-ASCII tests run the real bundled (bin/) or PATH ffmpeg and are skipped
when none is available.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core import ffmpeg_utils


def _real_ffmpeg_tool(tool):
    """Path to a real ffmpeg/ffprobe (the repo's bin/ first, then PATH), or None."""
    here = os.path.dirname(os.path.abspath(__file__))
    bundled = os.path.join(os.path.dirname(here), "bin", tool + (".exe" if os.name == "nt" else ""))
    if os.path.isfile(bundled):
        return bundled
    return shutil.which(tool)


class _FakeCompletedProcess:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class NvencAvailableTests(unittest.TestCase):
    def setUp(self):
        # nvenc_available() is @lru_cache'd for the life of the process --
        # clear it before/after each test so one test's mock result doesn't
        # leak into the next.
        ffmpeg_utils.nvenc_available.cache_clear()

    def tearDown(self):
        ffmpeg_utils.nvenc_available.cache_clear()

    def test_true_when_h264_nvenc_listed(self):
        encoders_output = (
            " V..... libx264              libx264 H.264\n"
            " V..... h264_nvenc           NVIDIA NVENC H.264 encoder\n"
            " V..... hevc_nvenc           NVIDIA NVENC hevc encoder\n"
        )
        with patch.object(ffmpeg_utils, "find_binary", return_value="ffmpeg"), patch.object(
            ffmpeg_utils, "run", return_value=_FakeCompletedProcess(stdout=encoders_output)
        ):
            self.assertTrue(ffmpeg_utils.nvenc_available())

    def test_false_when_h264_nvenc_not_listed(self):
        encoders_output = " V..... libx264              libx264 H.264\n"
        with patch.object(ffmpeg_utils, "find_binary", return_value="ffmpeg"), patch.object(
            ffmpeg_utils, "run", return_value=_FakeCompletedProcess(stdout=encoders_output)
        ):
            self.assertFalse(ffmpeg_utils.nvenc_available())

    def test_false_when_ffmpeg_not_found(self):
        with patch.object(ffmpeg_utils, "find_binary", side_effect=FileNotFoundError("no ffmpeg")):
            self.assertFalse(ffmpeg_utils.nvenc_available())

    def test_false_when_probe_raises(self):
        with patch.object(ffmpeg_utils, "find_binary", return_value="ffmpeg"), patch.object(
            ffmpeg_utils, "run", side_effect=OSError("boom")
        ):
            self.assertFalse(ffmpeg_utils.nvenc_available())

    def test_result_is_cached_across_calls(self):
        encoders_output = " V..... h264_nvenc           NVIDIA NVENC H.264 encoder\n"
        with patch.object(ffmpeg_utils, "find_binary", return_value="ffmpeg") as fake_find, patch.object(
            ffmpeg_utils, "run", return_value=_FakeCompletedProcess(stdout=encoders_output)
        ) as fake_run:
            self.assertTrue(ffmpeg_utils.nvenc_available())
            self.assertTrue(ffmpeg_utils.nvenc_available())
            self.assertTrue(ffmpeg_utils.nvenc_available())
        # The actual probe (encoder listing + one test encode) only ran
        # once -- later calls hit the lru_cache.
        self.assertEqual(fake_find.call_count, 1)
        self.assertEqual(fake_run.call_count, 2)

    def test_false_when_listed_but_test_encode_fails(self):
        # h264_nvenc is compiled into common Windows ffmpeg builds even on
        # machines with no NVIDIA GPU (or too old a driver) -- only a real
        # encode proves it works.
        listing = _FakeCompletedProcess(stdout=" V..... h264_nvenc           NVIDIA NVENC H.264 encoder\n")
        failed_encode = _FakeCompletedProcess(stderr="Cannot load nvcuda.dll", returncode=1)
        with patch.object(ffmpeg_utils, "find_binary", return_value="ffmpeg"), patch.object(
            ffmpeg_utils, "run", side_effect=[listing, failed_encode]
        ) as fake_run:
            self.assertFalse(ffmpeg_utils.nvenc_available())
        test_encode_args = fake_run.call_args_list[1][0][0]
        self.assertIn("h264_nvenc", test_encode_args)
        self.assertIn("nullsrc=s=256x256:d=0.1", test_encode_args)
        self.assertEqual(test_encode_args[-3:], ["-f", "null", "-"])

    def test_no_test_encode_when_encoder_not_listed(self):
        with patch.object(ffmpeg_utils, "find_binary", return_value="ffmpeg"), patch.object(
            ffmpeg_utils, "run", return_value=_FakeCompletedProcess(stdout=" V..... libx264\n")
        ) as fake_run:
            self.assertFalse(ffmpeg_utils.nvenc_available())
        self.assertEqual(fake_run.call_count, 1)

    def test_false_when_test_encode_times_out(self):
        listing = _FakeCompletedProcess(stdout=" V..... h264_nvenc\n")
        with patch.object(ffmpeg_utils, "find_binary", return_value="ffmpeg"), patch.object(
            ffmpeg_utils, "run", side_effect=[listing, subprocess.TimeoutExpired("ffmpeg", 30)]
        ):
            self.assertFalse(ffmpeg_utils.nvenc_available())


class NonAsciiOutputTests(unittest.TestCase):
    """Regression tests: run()/popen() used plain text=True, i.e. the locale
    code page (cp1252 on most Windows installs). FFmpeg/FFprobe emit UTF-8,
    so a file like Árbol.mp4 made subprocess's reader thread raise
    UnicodeDecodeError -- probe_file() then saw empty stdout and imported
    it as a broken duration=0/no-streams asset.
    """

    def setUp(self):
        self.ffmpeg = _real_ffmpeg_tool("ffmpeg")
        self.ffprobe = _real_ffmpeg_tool("ffprobe")
        if not self.ffmpeg or not self.ffprobe:
            self.skipTest("real ffmpeg/ffprobe not available")
        self.tmpdir = tempfile.mkdtemp(prefix="ve_nonascii_")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.path = os.path.join(self.tmpdir, "Árbol ñandú.mp4")
        result = subprocess.run(
            [
                self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=s=64x48:d=1:r=10",
                "-f", "lavfi", "-i", "sine=d=1",
                "-c:v", "libx264", "-c:a", "aac", "-shortest", self.path,
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            self.skipTest("could not generate test media with this ffmpeg build")

    def test_probe_file_reads_a_non_ascii_path(self):
        from core.media_probe import probe_file

        with patch("core.media_probe.find_binary", return_value=self.ffprobe):
            asset = probe_file(self.path)
        self.assertTrue(asset.has_video)
        self.assertTrue(asset.has_audio)
        self.assertGreater(asset.duration, 0.5)

    def test_popen_decodes_utf8_output(self):
        process = ffmpeg_utils.popen([self.ffprobe, "-hide_banner", self.path])
        output = process.stdout.read()
        process.wait()
        process.stdout.close()
        self.assertIn("Árbol ñandú.mp4", output)


class ProbeFileUnreadableTests(unittest.TestCase):
    """probe_file() must report a file ffprobe can't read as an error, not
    return an empty asset that imports as a broken zero-length clip."""

    def test_no_streams_raises(self):
        from core.media_probe import MediaProbeError, probe_file

        output = '{"format": {"duration": "1.0"}, "streams": []}'
        with patch("core.media_probe.find_binary", return_value="ffprobe"), patch(
            "core.media_probe.run", return_value=_FakeCompletedProcess(stdout=output)
        ):
            with self.assertRaises(MediaProbeError):
                probe_file("/media/weird.bin")

    def test_ffprobe_failure_raises_with_its_message(self):
        from core.media_probe import MediaProbeError, probe_file

        with patch("core.media_probe.find_binary", return_value="ffprobe"), patch(
            "core.media_probe.run",
            return_value=_FakeCompletedProcess(stderr="x.mp4: Invalid data found when processing input\n", returncode=1),
        ):
            with self.assertRaises(MediaProbeError) as ctx:
                probe_file("/media/x.mp4")
        self.assertIn("Invalid data found", str(ctx.exception))

    def test_still_image_is_not_an_error(self):
        from core.media_probe import probe_file

        output = '{"format": {}, "streams": [{"codec_type": "video", "width": 64, "height": 48, "codec_name": "png"}]}'
        with patch("core.media_probe.find_binary", return_value="ffprobe"), patch(
            "core.media_probe.run", return_value=_FakeCompletedProcess(stdout=output)
        ):
            asset = probe_file("/media/pic.png")
        self.assertTrue(asset.is_image)

    def test_missing_ffprobe_still_returns_a_default_asset(self):
        from core.media_probe import probe_file

        with patch("core.media_probe.find_binary", side_effect=FileNotFoundError("no ffprobe")):
            asset = probe_file("/media/a.mp4")
        self.assertEqual(asset.duration, 0.0)


if __name__ == "__main__":
    unittest.main()
