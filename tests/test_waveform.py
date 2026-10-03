"""Tests for waveform peak extraction/caching (no FFmpeg required).

``_pcm_bytes_to_peaks`` and ``load_waveform`` are pure functions with no
subprocess or Qt dependency, so they're tested directly here. The FFmpeg
decode step itself (``generate_waveform``) was verified manually against
real media files; it isn't exercised by this suite since it needs a real
FFmpeg binary.
"""

import array
import json
import os
import struct
import tempfile
import unittest

from core.waveform import (
    PEAKS_PER_SECOND,
    _pcm_bytes_to_peaks,
    load_waveform,
)


def _pcm(samples):
    """Pack a list of 16-bit signed ints as little-endian PCM bytes."""
    return struct.pack(f"<{len(samples)}h", *samples)


class PcmToPeaksTests(unittest.TestCase):
    def test_empty_input_yields_no_peaks(self):
        self.assertEqual(_pcm_bytes_to_peaks(b""), [])

    def test_silence_yields_zero_amplitude_peaks(self):
        samples = [0] * 400  # one full bucket at the module's sample rate
        peaks = _pcm_bytes_to_peaks(_pcm(samples))
        self.assertTrue(all(p == 0.0 for p in peaks))
        self.assertGreater(len(peaks), 0)

    def test_full_scale_sample_yields_peak_near_one(self):
        samples = [32767] * 400
        peaks = _pcm_bytes_to_peaks(_pcm(samples))
        self.assertAlmostEqual(peaks[0], 1.0, places=2)

    def test_negative_full_scale_sample_is_measured_by_magnitude(self):
        samples = [-32768] * 400
        peaks = _pcm_bytes_to_peaks(_pcm(samples))
        self.assertAlmostEqual(peaks[0], 1.0, places=2)

    def test_odd_trailing_byte_is_ignored_not_crashed_on(self):
        # One extra stray byte that doesn't complete a 16-bit sample.
        data = _pcm([1000, 2000]) + b"\x01"
        peaks = _pcm_bytes_to_peaks(data)
        self.assertTrue(len(peaks) >= 1)

    def test_peak_values_are_bounded_to_one(self):
        samples = array.array("h", [32767, -32768] * 200)
        peaks = _pcm_bytes_to_peaks(samples.tobytes())
        self.assertTrue(all(0.0 <= p <= 1.0 for p in peaks))


class LoadWaveformTests(unittest.TestCase):
    def test_load_missing_file_returns_none(self):
        self.assertIsNone(load_waveform("/nonexistent/path/that/is/not/real.json"))

    def test_load_malformed_json_returns_none(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{not valid json")
            path = f.name
        try:
            self.assertIsNone(load_waveform(path))
        finally:
            os.unlink(path)

    def test_load_valid_cache_round_trips(self):
        peaks = [0.1, 0.5, 0.9, 0.2]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"peaks_per_second": PEAKS_PER_SECOND, "peaks": peaks}, f)
            path = f.name
        try:
            result = load_waveform(path)
            self.assertIsNotNone(result)
            loaded_peaks, rms, density = result
            self.assertEqual(loaded_peaks, peaks)
            # A cache written before RMS levels existed falls back to the peaks.
            self.assertEqual(rms, peaks)
            self.assertEqual(density, float(PEAKS_PER_SECOND))
        finally:
            os.unlink(path)

    def test_load_cache_missing_keys_returns_none(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"peaks": [0.1]}, f)  # missing peaks_per_second
            path = f.name
        try:
            self.assertIsNone(load_waveform(path))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
