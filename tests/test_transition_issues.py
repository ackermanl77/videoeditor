"""F14: why a requested transition won't be rendered (core.filter_graph.transition_issue)."""
import unittest

from core.clip import Clip, MediaType, TransitionType
from core.filter_graph import _find_audio_crossfade_partner, _find_crossfade_partner, track_transition_issues, transition_issue
from core.track import Track, TrackType


def _clip(start, length=5.0, kind=MediaType.VIDEO, transition=True, **extra):
    return Clip(media_type=kind, source_in=0.0, source_out=length, timeline_start=start,
                transition_out=TransitionType.CROSSFADE if transition else TransitionType.NONE,
                transition_duration=1.0, **extra)


class TransitionIssueTests(unittest.TestCase):
    def test_a_joined_pair_and_no_transition_have_no_issue(self):
        clips = [_clip(0), _clip(5, transition=False)]
        self.assertIsNone(transition_issue(clips, 0))
        self.assertIsNone(transition_issue(clips, 1))

    def test_each_reason(self):
        cases = {
            "no clip follows it on this track": [_clip(0)],
            "there's a gap before the next clip": [_clip(0), _clip(6)],
            "the next clip isn't a video or an image": [_clip(0), _clip(5, kind=MediaType.TEXT)],
            "this clip has a speed ramp": [_clip(0, speed_keyframes=[(0.0, 1.0), (2.0, 2.0)]), _clip(5)],
            "the next clip has a speed ramp": [_clip(0), _clip(5, speed_keyframes=[(0.0, 1.0), (2.0, 2.0)])],
            "the clips are too short for it": [_clip(0, length=0.01), _clip(0.01)],
            "the next clip isn't audio": [_clip(0, kind=MediaType.AUDIO), _clip(5)],
        }
        for reason, clips in cases.items():
            with self.subTest(reason):
                self.assertEqual(transition_issue(clips, 0), reason)

    def test_chained_transitions_follow_the_renderers_pairing(self):
        # a->b pairs, so b's own transition can't start another pair; c->d pairs again.
        clips = [_clip(0), _clip(5), _clip(10), _clip(15, transition=False)]
        self.assertIsNone(transition_issue(clips, 0))
        self.assertIn("can't be chained", transition_issue(clips, 1))
        self.assertIsNone(transition_issue(clips, 2))

    def test_the_reasons_agree_with_the_renderer(self):
        layouts = [
            [_clip(0), _clip(5), _clip(10), _clip(15)],
            [_clip(0), _clip(6), _clip(11, kind=MediaType.IMAGE), _clip(16)],
            [_clip(0, kind=MediaType.AUDIO), _clip(5, kind=MediaType.AUDIO), _clip(11, kind=MediaType.AUDIO)],
        ]
        for clips in layouts:
            audio = clips[0].media_type == MediaType.AUDIO
            finder = _find_audio_crossfade_partner if audio else _find_crossfade_partner
            taken = set()
            for i, clip in enumerate(clips):
                rendered = i not in taken and finder(clips, i) is not None
                if rendered:
                    taken.add(i + 1)
                with self.subTest(layout=[c.timeline_start for c in clips], i=i):
                    self.assertEqual(transition_issue(clips, i) is None, rendered or not clip.has_transition)

    def test_track_issues_use_the_renderers_clip_list(self):
        track = Track(name="A1", track_type=TrackType.AUDIO)
        a, b = _clip(0, kind=MediaType.AUDIO), _clip(5, kind=MediaType.AUDIO, transition=False)
        track.clips = [b, a]  # unsorted on purpose
        self.assertEqual(track_transition_issues(track), {})
        b.timeline_start = 7
        self.assertEqual(track_transition_issues(track), {a.id: "there's a gap before the next clip"})


if __name__ == "__main__":
    unittest.main()
