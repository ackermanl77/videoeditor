"""Tests for core/ffmpeg_utils.py's NVENC availability probe.

nvenc_available() shells out to `ffmpeg -encoders`, so these tests mock
find_binary()/run() rather than depending on this machine's actual GPU or
ffmpeg build -- the same approach test_exporter.py uses for the exporter's
own subprocess calls.
"""

import unittest
from unittest.mock import patch

from core import ffmpeg_utils


class _FakeCompletedProcess:
    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = 0


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
        # The actual probe only ran once -- later calls hit the lru_cache.
        self.assertEqual(fake_find.call_count, 1)
        self.assertEqual(fake_run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
