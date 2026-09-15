"""Unit tests for the Clip / Track data models."""

import unittest

from core.clip import Clip, MediaType, TextStyle
from core.track import Track, TrackType


class ClipDurationTests(unittest.TestCase):
    def test_duration_at_normal_speed(self):
        clip = Clip(media_type=MediaType.VIDEO, source_in=2.0, source_out=7.0, timeline_start=0.0)
        self.assertAlmostEqual(clip.duration, 5.0)
        self.assertAlmostEqual(clip.timeline_end, 5.0)

    def test_duration_scales_with_speed(self):
        clip = Clip(media_type=MediaType.VIDEO, source_in=0.0, source_out=10.0, speed=2.0)
        self.assertAlmostEqual(clip.duration, 5.0)

        clip.speed = 0.5
        self.assertAlmostEqual(clip.duration, 20.0)

    def test_contains_time(self):
        clip = Clip(media_type=MediaType.VIDEO, source_in=0.0, source_out=4.0, timeline_start=10.0)
        self.assertFalse(clip.contains_time(9.9))
        self.assertTrue(clip.contains_time(10.0))
        self.assertTrue(clip.contains_time(13.9))
        self.assertFalse(clip.contains_time(14.0))

    def test_round_trip_serialization(self):
        clip = Clip(
            media_type=MediaType.TEXT,
            source_in=0.0,
            source_out=3.0,
            timeline_start=1.5,
            text_style=TextStyle(content="Hello", font_size=60),
        )
        data = clip.to_dict()
        restored = Clip.from_dict(data)
        self.assertEqual(restored.media_type, MediaType.TEXT)
        self.assertEqual(restored.text_style.content, "Hello")
        self.assertEqual(restored.text_style.font_size, 60)
        self.assertAlmostEqual(restored.timeline_start, 1.5)


class TrackOverlapTests(unittest.TestCase):
    def test_no_overlap_when_sequential(self):
        track = Track(track_type=TrackType.VIDEO)
        a = Clip(media_type=MediaType.VIDEO, source_in=0, source_out=5, timeline_start=0)
        b = Clip(media_type=MediaType.VIDEO, source_in=0, source_out=5, timeline_start=5)
        track.clips = [a, b]
        self.assertFalse(track.has_overlap(a, ignore_id=a.id))
        self.assertFalse(track.has_overlap(b, ignore_id=b.id))

    def test_overlap_detected(self):
        track = Track(track_type=TrackType.VIDEO)
        a = Clip(media_type=MediaType.VIDEO, source_in=0, source_out=5, timeline_start=0)
        track.clips = [a]
        probe = Clip(media_type=MediaType.VIDEO, source_in=0, source_out=5, timeline_start=3)
        self.assertTrue(track.has_overlap(probe, ignore_id=probe.id))

    def test_clip_at_returns_correct_clip(self):
        track = Track(track_type=TrackType.VIDEO)
        a = Clip(media_type=MediaType.VIDEO, source_in=0, source_out=5, timeline_start=0)
        b = Clip(media_type=MediaType.VIDEO, source_in=0, source_out=5, timeline_start=5)
        track.clips = [a, b]
        self.assertEqual(track.clip_at(2.0).id, a.id)
        self.assertEqual(track.clip_at(7.0).id, b.id)
        self.assertIsNone(track.clip_at(20.0))


if __name__ == "__main__":
    unittest.main()
