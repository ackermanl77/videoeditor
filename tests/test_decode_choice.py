"""Per-source GPU decoding (PG2): each file's decoders are timed once and only
inputs measured faster on the GPU get ``-hwaccel``."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from core import decode_choice
from core.ffmpeg_utils import (
    DECODE_HWACCEL_CHOICES,
    DECODE_HWACCEL_ENV,
    PER_SOURCE,
    apply_decode_hwaccel,
    decode_attempts,
    decode_hwaccel_modes,
)

try:
    from core.ffmpeg_utils import find_binary, run

    FFMPEG = find_binary("ffmpeg")
except FileNotFoundError:  # pragma: no cover
    FFMPEG = None


class _IsolatedChoices(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ve_decode_")
        patcher = patch.dict(os.environ, {"USERPROFILE": self.home, "HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        decode_choice.forget_cached_choices()
        self.addCleanup(decode_choice.forget_cached_choices)

    def _file(self, name="clip.mp4"):
        path = os.path.join(self.home, name)
        with open(path, "wb") as f:
            f.write(b"not really a video")
        return path


class ChooseDecoderTests(unittest.TestCase):
    def test_the_gpu_must_clearly_beat_software(self):
        self.assertEqual(decode_choice.choose_decoder({"software": 1.0, "d3d11va": 0.5, "cuda": 0.7}), "d3d11va")
        self.assertIsNone(decode_choice.choose_decoder({"software": 1.0, "d3d11va": 0.95, "cuda": None}))
        self.assertIsNone(decode_choice.choose_decoder({"software": 0.1, "d3d11va": 0.2, "cuda": 0.17}))
        self.assertIsNone(decode_choice.choose_decoder({"software": 1.0, "d3d11va": None, "cuda": None}))
        self.assertEqual(decode_choice.choose_decoder({"software": None, "cuda": 2.0}), "cuda")

    def test_per_source_is_a_menu_choice_and_a_decode_mode(self):
        self.assertIn(PER_SOURCE, DECODE_HWACCEL_CHOICES)
        with patch.dict(os.environ, {DECODE_HWACCEL_ENV: PER_SOURCE}):
            self.assertEqual(decode_hwaccel_modes(), [PER_SOURCE, None])


class PerSourceArgsTests(_IsolatedChoices):
    def test_only_inputs_measured_faster_on_the_gpu_get_hwaccel(self):
        heavy, light = self._file("heavy.mp4"), self._file("light.mp4")
        winners = {heavy: "cuda", light: None}
        with patch.object(decode_choice, "chosen_decoder", side_effect=lambda p: winners.get(p)):
            args = ["ffmpeg", "-i", heavy, "-i", light, "-f", "lavfi", "-i", "color=black", "out.mp4"]
            self.assertEqual(apply_decode_hwaccel(args, PER_SOURCE), [
                "ffmpeg", "-hwaccel", "cuda", "-i", heavy, "-i", light, "-f", "lavfi", "-i", "color=black", "out.mp4",
            ])

    def test_an_attempt_identical_to_software_is_not_run_twice(self):
        light = self._file("light.mp4")
        args = ["ffmpeg", "-i", light, "out.mp4"]
        with patch.dict(os.environ, {DECODE_HWACCEL_ENV: PER_SOURCE}):
            self.assertEqual(decode_attempts(args), [(PER_SOURCE, args)])  # nothing measured: already software
        with patch.dict(os.environ, {DECODE_HWACCEL_ENV: "cuda"}):
            self.assertEqual([m for m, _a in decode_attempts(args)], ["cuda", None])


class RememberTests(_IsolatedChoices):
    def test_a_file_is_measured_once_and_again_after_it_changes(self):
        path = self._file()
        timings = {"software": 1.0, "cuda": 0.4}  # CUDA is a candidate on every platform
        with patch.object(decode_choice, "measure_decoders", return_value=timings) as measure:
            self.assertFalse(decode_choice.is_measured(path))
            self.assertEqual(decode_choice.measure_and_remember(path), "cuda")
            self.assertEqual(decode_choice.measure_and_remember(path), "cuda")
            self.assertEqual(measure.call_count, 1)
        self.assertEqual(decode_choice.chosen_decoder(path), "cuda")
        with open(decode_choice.choices_path(), encoding="utf-8") as f:
            stored = json.load(f)
        self.assertEqual(list(stored.values())[0]["decoder"], "cuda")
        # Replaced file (new size): measured again; a software winner is remembered as such.
        with open(path, "ab") as f:
            f.write(b"more")
        with patch.object(decode_choice, "measure_decoders", return_value={"software": 1.0, "cuda": 1.5}):
            self.assertIsNone(decode_choice.measure_and_remember(path))
        self.assertTrue(decode_choice.is_measured(path))
        self.assertIsNone(decode_choice.chosen_decoder(path))
        self.assertIn("2 files measured, 1 decodes on the GPU", decode_choice.measured_summary())

    def test_an_undecodable_file_is_not_remembered(self):
        path = self._file()
        with patch.object(decode_choice, "measure_decoders", return_value={"software": None, "cuda": None}):
            self.assertIsNone(decode_choice.measure_and_remember(path))
        self.assertFalse(decode_choice.is_measured(path))
        self.assertIsNone(decode_choice.measure_and_remember(os.path.join(self.home, "missing.mp4")))


@unittest.skipUnless(FFMPEG, "FFmpeg is not available")
class RealMeasurementTests(_IsolatedChoices):
    def test_real_timings_for_software_and_a_failing_decoder(self):
        clip = os.path.join(self.home, "small.mp4")
        run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30", "-t", "6",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", clip], timeout=60)
        timings = decode_choice.measure_decoders(clip, candidates=["no_such_hwaccel"], frames=60)
        self.assertGreater(timings["software"], 0.0)
        self.assertIsNone(timings["no_such_hwaccel"])  # FFmpeg rejects it: never chosen
        self.assertIsNone(decode_choice.choose_decoder(timings))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
