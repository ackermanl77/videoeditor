"""Live EBU R128 loudness in the preview (core/loudness.py, F15's last piece).

The measurement comes from FFmpeg's ebur128 filter; the integrated value is
recomputed here from its momentary blocks and must match FFmpeg's own summary.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
import unittest
from unittest.mock import patch

from core.loudness import (
    IntegratedLoudness,
    format_lufs,
    generate_loudness,
    load_loudness,
    loudness_at,
    loudness_cache_path,
    parse_frame_log,
)

try:
    from core.ffmpeg_utils import find_binary, run

    FFMPEG = find_binary("ffmpeg")
except FileNotFoundError:  # pragma: no cover
    FFMPEG = None

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from core.clip import Clip, MediaType
    from core.media_asset import MediaAsset
    from core.project import Project
    from ui.preview_player import PreviewPlayer

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PYSIDE6_AVAILABLE = False

LOG = """[Parsed_ebur128_0 @ 0000] t: 0.0999792  TARGET:-23 LUFS    M:-120.7 S:-120.7     I: -70.0 LUFS       LRA:   0.0 LU
[Parsed_ebur128_0 @ 0000] t: 0.199979   TARGET:-23 LUFS    M: -inf S:-120.7     I: -70.0 LUFS       LRA:   0.0 LU
[Parsed_ebur128_0 @ 0000] t: 0.299979   TARGET:-23 LUFS    M: -18.4 S: -21.0     I: -18.4 LUFS       LRA:   0.0 LU
"""


class LoudnessMathTests(unittest.TestCase):
    def test_frame_log_lines_become_two_series(self):
        momentary, short_term = parse_frame_log(LOG)
        self.assertEqual(momentary, [-120.7, -120.0, -18.4])
        self.assertEqual(short_term, [-120.7, -120.7, -21.0])

    def test_readings_apply_the_clip_gain_and_hide_silence(self):
        data = ([-120.0, -30.0, -20.0], [-120.0, -32.0, -25.0], 10.0)
        self.assertEqual(loudness_at(data, 0.05), (None, None))  # before the first window ends
        self.assertEqual(loudness_at(data, 0.2), (-30.0, -32.0))
        m, s = loudness_at(data, 0.3, gain=0.5)  # -6.02 dB
        self.assertAlmostEqual(m, -26.02, places=2)
        self.assertAlmostEqual(s, -31.02, places=2)
        self.assertEqual(loudness_at(data, 0.3, gain=0.0), (None, None))
        self.assertEqual(loudness_at(data, 5.0), (None, None))
        self.assertEqual(format_lufs(None), "  -∞ ")
        self.assertEqual(format_lufs(-18.25), "-18.2")

    def test_integrated_gates_like_r128_and_counts_each_block_once(self):
        meter = IntegratedLoudness()
        self.assertIsNone(meter.value())
        for i in range(10):
            meter.add(i / 10, -20.0)
            meter.add(i / 10 + 0.04, -20.0)  # a faster UI tick: same block
        meter.add(1.0, None)  # silence: below the absolute gate
        meter.add(1.1, -40.0)  # more than 10 LU below: relative gate
        self.assertAlmostEqual(meter.value(), -20.0)
        meter.reset()
        self.assertIsNone(meter.value())


@unittest.skipUnless(FFMPEG, "FFmpeg is not available")
class LoudnessMeasurementTests(unittest.TestCase):
    def _tone(self, path, filters):
        run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", filters, "-c:a", "pcm_s16le", path], timeout=60)
        return path

    def test_integrated_from_the_momentary_blocks_matches_ffmpegs_summary(self):
        tmp = tempfile.mkdtemp(prefix="ve_loud_")
        # 3 s loud, 3 s 20 dB quieter (gated out relatively), 3 s medium.
        source = self._tone(os.path.join(tmp, "tone.wav"), (
            "sine=frequency=1000:sample_rate=48000:duration=9,"
            "volume='if(lt(t,3),0.8,if(lt(t,6),0.08,0.4))':eval=frame"
        ))
        cache = generate_loudness(source, os.path.join(tmp, "cache"), 9.0)
        self.assertEqual(cache, loudness_cache_path(source, os.path.join(tmp, "cache")))
        momentary, short_term, density = load_loudness(cache)
        self.assertEqual(density, 10.0)
        self.assertAlmostEqual(len(momentary), 90, delta=2)
        # The loud second (a 1 kHz sine at 0.8 x 1/8 full scale, mono).
        expected = 20 * math.log10(0.8 / 8) - 3.01 - 0.691 + 0.69  # sine RMS, -0.691, K-weighting at 1 kHz
        self.assertAlmostEqual(loudness_at((momentary, short_term, density), 2.0)[0], expected, delta=0.3)
        meter = IntegratedLoudness()
        for i, value in enumerate(momentary):
            meter.add(i / density, value if value > -70 else None)
        summary = run([FFMPEG, "-hide_banner", "-nostats", "-i", source, "-af", "ebur128", "-f", "null", "-"],
                      timeout=60)
        ffmpeg_i = float(re.findall(r"I:\s*(-?[\d.]+) LUFS", summary.stderr)[-1])
        self.assertAlmostEqual(meter.value(), ffmpeg_i, delta=0.2)
        # Cached: a second call doesn't run FFmpeg again.
        with patch("core.loudness.run_binary", side_effect=AssertionError("ran again")):
            self.assertEqual(generate_loudness(source, os.path.join(tmp, "cache"), 9.0), cache)

    def test_a_file_without_audio_gives_no_measurement(self):
        tmp = tempfile.mkdtemp(prefix="ve_loud_")
        video = os.path.join(tmp, "silent.mp4")
        run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", video], timeout=60)
        self.assertIsNone(generate_loudness(video, os.path.join(tmp, "cache"), 1.0))
        self.assertIsNone(load_loudness(os.path.join(tmp, "missing.json")))


@unittest.skipUnless(_PYSIDE6_AVAILABLE and FFMPEG, "PySide6 or FFmpeg is not available")
class PreviewLoudnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ve_loud_ui_")
        source = os.path.join(self.tmp, "tone.wav")
        run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
             "sine=frequency=1000:sample_rate=48000:duration=4,volume=0.8", "-c:a", "pcm_s16le", source], timeout=60)
        self.project = Project()
        self.asset = MediaAsset(path=source, duration=4.0, has_video=False, has_audio=True)
        self.project.add_media_asset(self.asset)
        self.clip = Clip(name="tone", source_path=source, media_type=MediaType.AUDIO, timeline_start=0.0,
                         source_in=0.0, source_out=4.0, asset_id=self.asset.id)
        audio_track = next(t for t in self.project.tracks if t.track_type.name == "AUDIO")
        audio_track.clips.append(self.clip)
        self.preview = PreviewPlayer()
        self.preview.loudness_cache_dir = os.path.join(self.tmp, "loudness")
        self.addCleanup(self.preview.deleteLater)
        self.preview.set_project(self.project)

    def _wait_for_measurement(self):
        # time.sleep (not QTest.qWait, which keeps the GIL) lets the measuring thread run.
        for _ in range(400):
            QApplication.processEvents()
            if self.asset.path in self.preview._loudness_cache:
                return
            time.sleep(0.05)
        self.fail("the loudness measurement never arrived")

    def test_the_meter_measures_in_the_background_then_follows_volume(self):
        self.preview.seek_to(3.5)  # short-term needs 3 s of audio behind it
        self._wait_for_measurement()
        momentary, short_term, _integrated = self.preview.loudness_reading()
        self.assertIsNotNone(momentary)
        self.assertAlmostEqual(momentary, short_term, delta=0.3)  # a steady tone
        self.assertIn("LUFS", self.preview.loudness_label.text())
        self.clip.volume = 0.5
        self.preview.refresh_after_edit()
        self.assertAlmostEqual(self.preview.loudness_reading()[0], momentary - 6.02, delta=0.05)
        self.clip.muted = True
        self.preview.refresh_after_edit()
        self.assertEqual(self.preview.loudness_reading()[:2], (None, None))
        self.assertTrue(os.path.isfile(loudness_cache_path(self.asset.path, self.preview.loudness_cache_dir)))

    def test_integrated_accumulates_only_while_playing_and_restarts_on_play(self):
        self.preview.seek_to(2.0)
        self._wait_for_measurement()
        self.assertIsNone(self.preview.loudness_reading()[2])  # paused: nothing integrated
        self.preview._is_playing = True  # what play() sets; no real audio output offscreen
        for step in range(10):
            self.preview.seek_to(1.0 + step / 10)
        self.assertIsNotNone(self.preview.loudness_reading()[2])
        self.preview._is_playing = False
        self.preview.play()
        self.preview.pause()
        self.preview.seek_to(2.0)
        self.assertIsNone(self.preview.loudness_reading()[2])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
