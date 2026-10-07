"""F11: timeline waveforms are drawn at the clip's volume (core.waveform.display_gains)."""
import unittest

from core.clip import Clip, MediaType
from core.waveform import display_gains


def _audio(**extra):
    return Clip(media_type=MediaType.AUDIO, source_in=0.0, source_out=4.0, **extra)


class DisplayGainTests(unittest.TestCase):
    def test_scalar_volume_and_mute(self):
        self.assertEqual(display_gains(_audio(), 3), [1.0, 1.0, 1.0])
        self.assertEqual(display_gains(_audio(volume=0.5), 2), [0.5, 0.5])
        self.assertEqual(display_gains(_audio(volume=1.5, muted=True), 2), [0.0, 0.0])
        self.assertEqual(display_gains(_audio(), 0), [])

    def test_the_automation_envelope_is_followed(self):
        clip = _audio(volume_keyframes=[(0.0, 0.0), (4.0, 2.0)])  # a 0 -> 2 ramp over the clip
        gains = display_gains(clip, 4)  # sampled at 0.5, 1.5, 2.5, 3.5 s
        self.assertEqual([round(g, 3) for g in gains], [0.25, 0.75, 1.25, 1.75])

    def test_a_single_keyframe_is_not_automation(self):
        self.assertEqual(display_gains(_audio(volume=0.8, volume_keyframes=[(1.0, 0.1)]), 1), [0.8])


if __name__ == "__main__":
    unittest.main()
