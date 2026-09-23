"""Unit tests for the Clip / Track data models."""

import unittest

from core.clip import Clip, MediaType, TextStyle, TransitionType
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

    def test_speed_keyframes_are_ignored_below_two_entries(self):
        clip = Clip(media_type=MediaType.VIDEO, source_in=0.0, source_out=10.0, speed_keyframes=[(2.0, 4.0)])
        self.assertAlmostEqual(clip.duration, 10.0)  # falls back to constant speed=1.0

    def test_speed_segments_extend_to_cover_the_full_trimmed_region(self):
        # Keyframes at t=2 and t=6 (source-in-relative); region is [0, 10].
        clip = Clip(
            media_type=MediaType.VIDEO, source_in=0.0, source_out=10.0,
            speed_keyframes=[(2.0, 2.0), (6.0, 0.5)],
        )
        segments = clip.speed_segments
        self.assertEqual(len(segments), 3)
        self.assertAlmostEqual(segments[0][0], 0.0)
        self.assertAlmostEqual(segments[0][1], 2.0)
        self.assertAlmostEqual(segments[0][2], 2.0)  # extended backward at the first keyframe's speed
        self.assertAlmostEqual(segments[1][0], 2.0)
        self.assertAlmostEqual(segments[1][1], 6.0)
        self.assertAlmostEqual(segments[1][2], 2.0)
        self.assertAlmostEqual(segments[2][0], 6.0)
        self.assertAlmostEqual(segments[2][1], 10.0)
        self.assertAlmostEqual(segments[2][2], 0.5)

    def test_duration_sums_each_segments_own_speed(self):
        # [0,2) at 1x (2s), [2,6) at 2x (2s), [6,10) at 0.5x (8s) -> 12s total.
        clip = Clip(
            media_type=MediaType.VIDEO, source_in=0.0, source_out=10.0,
            speed_keyframes=[(0.0, 1.0), (2.0, 2.0), (6.0, 0.5)],
        )
        self.assertAlmostEqual(clip.duration, 2.0 / 1.0 + 4.0 / 2.0 + 4.0 / 0.5)

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

    def test_denoise_fields_default_off_and_round_trip(self):
        clip = Clip(media_type=MediaType.AUDIO, source_in=0.0, source_out=5.0)
        self.assertFalse(clip.denoise_enabled)
        self.assertAlmostEqual(clip.denoise_strength, 12.0)

        clip.denoise_enabled = True
        clip.denoise_strength = 30.0
        restored = Clip.from_dict(clip.to_dict())
        self.assertTrue(restored.denoise_enabled)
        self.assertAlmostEqual(restored.denoise_strength, 30.0)

    def test_new_transition_types_round_trip(self):
        for transition in (
            TransitionType.WIPE_LEFT, TransitionType.SLIDE_UP,
            TransitionType.CIRCLE_CROP, TransitionType.DISSOLVE,
        ):
            with self.subTest(transition=transition):
                clip = Clip(media_type=MediaType.VIDEO, source_in=0.0, source_out=5.0, transition_out=transition)
                restored = Clip.from_dict(clip.to_dict())
                self.assertEqual(restored.transition_out, transition)


class ClipEffectFlagTests(unittest.TestCase):
    """``Clip.has_*`` mirror filter_graph.py's own "should I emit this
    filter" checks exactly -- both it and the timeline's per-clip
    effect-indicator badges (``ui/widgets/timeline_items.py::ClipItem``)
    read these instead of duplicating the field comparisons.
    """

    def test_default_clip_has_no_active_effects(self):
        clip = Clip(media_type=MediaType.VIDEO, source_in=0.0, source_out=5.0)
        self.assertFalse(clip.has_color_correction)
        self.assertFalse(clip.has_chroma_key)
        self.assertFalse(clip.has_denoise)
        self.assertFalse(clip.has_transition)

    def test_has_color_correction_true_if_any_single_field_is_non_neutral(self):
        for field_name, value in (("brightness", 0.1), ("contrast", 1.2), ("saturation", 0.5), ("gamma", 1.3)):
            with self.subTest(field=field_name):
                clip = Clip(media_type=MediaType.VIDEO, source_in=0.0, source_out=5.0)
                setattr(clip, field_name, value)
                self.assertTrue(clip.has_color_correction)

    def test_has_chroma_key_tracks_the_enabled_flag(self):
        clip = Clip(media_type=MediaType.VIDEO, source_in=0.0, source_out=5.0, chroma_key_enabled=True)
        self.assertTrue(clip.has_chroma_key)

    def test_has_denoise_tracks_the_enabled_flag(self):
        clip = Clip(media_type=MediaType.AUDIO, source_in=0.0, source_out=5.0, denoise_enabled=True)
        self.assertTrue(clip.has_denoise)

    def test_has_transition_false_for_none_true_otherwise(self):
        none_clip = Clip(media_type=MediaType.VIDEO, source_in=0.0, source_out=5.0)
        self.assertFalse(none_clip.has_transition)
        fade_clip = Clip(
            media_type=MediaType.VIDEO, source_in=0.0, source_out=5.0, transition_out=TransitionType.DISSOLVE
        )
        self.assertTrue(fade_clip.has_transition)


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


class TrackDuckingFieldsTests(unittest.TestCase):
    def test_ducking_fields_default_to_no_trigger(self):
        track = Track(track_type=TrackType.AUDIO)
        self.assertIsNone(track.duck_trigger_track_id)

    def test_ducking_fields_round_trip(self):
        trigger = Track(track_type=TrackType.AUDIO, name="Narration")
        music = Track(track_type=TrackType.AUDIO, name="Music")
        music.duck_trigger_track_id = trigger.id
        music.duck_threshold = 0.08
        music.duck_ratio = 6.0
        music.duck_attack = 10.0
        music.duck_release = 300.0

        restored = Track.from_dict(music.to_dict())
        self.assertEqual(restored.duck_trigger_track_id, trigger.id)
        self.assertAlmostEqual(restored.duck_threshold, 0.08)
        self.assertAlmostEqual(restored.duck_ratio, 6.0)
        self.assertAlmostEqual(restored.duck_attack, 10.0)
        self.assertAlmostEqual(restored.duck_release, 300.0)


if __name__ == "__main__":
    unittest.main()
