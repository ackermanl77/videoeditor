"""Tests for the undo/redo command stack against real editing operations."""

import unittest

from core.clip import Clip, MediaType
from core.commands import (
    AddClipCommand,
    AddMarkerCommand,
    MoveClipCommand,
    RemoveClipCommand,
    RemoveMarkerCommand,
    RenameMarkerCommand,
    SplitClipCommand,
    TrimClipCommand,
    UpdateClipPropertyCommand,
    build_ripple_delete_command,
    build_ripple_trim_command,
)
from core.history import History
from core.project import Project
from core.track import TrackType


def make_project_with_clip():
    project = Project()
    track = project.tracks[0]
    clip = Clip(media_type=MediaType.VIDEO, source_in=0, source_out=10, timeline_start=0)
    track.clips.append(clip)
    return project, track, clip


class AddRemoveCommandTests(unittest.TestCase):
    def test_add_and_undo(self):
        project = Project()
        track = project.tracks[0]
        clip = Clip(media_type=MediaType.VIDEO, source_in=0, source_out=5, timeline_start=0)
        history = History()

        history.execute(AddClipCommand(project, track.id, clip))
        self.assertEqual(len(track.clips), 1)

        history.undo()
        self.assertEqual(len(track.clips), 0)

        history.redo()
        self.assertEqual(len(track.clips), 1)

    def test_remove_and_undo_restores_clip(self):
        project, track, clip = make_project_with_clip()
        history = History()

        history.execute(RemoveClipCommand(project, clip.id))
        self.assertEqual(len(track.clips), 0)

        history.undo()
        self.assertEqual(len(track.clips), 1)
        self.assertEqual(track.clips[0].id, clip.id)


class TrimSplitCommandTests(unittest.TestCase):
    def test_trim_and_undo(self):
        project, track, clip = make_project_with_clip()
        history = History()

        history.execute(TrimClipCommand(project, clip.id, source_in=2.0, source_out=8.0, timeline_start=1.0))
        self.assertAlmostEqual(clip.source_in, 2.0)
        self.assertAlmostEqual(clip.source_out, 8.0)
        self.assertAlmostEqual(clip.timeline_start, 1.0)

        history.undo()
        self.assertAlmostEqual(clip.source_in, 0.0)
        self.assertAlmostEqual(clip.source_out, 10.0)
        self.assertAlmostEqual(clip.timeline_start, 0.0)

    def test_split_creates_two_clips_and_undo_restores_one(self):
        project, track, clip = make_project_with_clip()
        history = History()

        history.execute(SplitClipCommand(project, clip.id, split_time=4.0))
        self.assertEqual(len(track.clips), 2)
        first, second = sorted(track.clips, key=lambda c: c.timeline_start)
        self.assertAlmostEqual(first.source_out, 4.0)
        self.assertAlmostEqual(second.source_in, 4.0)
        self.assertAlmostEqual(second.timeline_start, 4.0)

        history.undo()
        self.assertEqual(len(track.clips), 1)
        self.assertAlmostEqual(track.clips[0].source_out, 10.0)

    def test_split_outside_clip_is_noop(self):
        project, track, clip = make_project_with_clip()
        history = History()
        history.execute(SplitClipCommand(project, clip.id, split_time=50.0))
        self.assertEqual(len(track.clips), 1)


class MoveAndPropertyCommandTests(unittest.TestCase):
    def test_move_between_tracks(self):
        project, track, clip = make_project_with_clip()
        new_track = project.add_track(TrackType.VIDEO)
        history = History()

        history.execute(MoveClipCommand(project, clip.id, new_start=3.0, new_track_id=new_track.id))
        self.assertEqual(len(track.clips), 0)
        self.assertEqual(len(new_track.clips), 1)
        self.assertAlmostEqual(clip.timeline_start, 3.0)

        history.undo()
        self.assertEqual(len(track.clips), 1)
        self.assertEqual(len(new_track.clips), 0)

    def test_update_property_undo(self):
        project, track, clip = make_project_with_clip()
        history = History()
        history.execute(UpdateClipPropertyCommand(project, clip.id, "volume", 1.5))
        self.assertAlmostEqual(clip.volume, 1.5)
        history.undo()
        self.assertAlmostEqual(clip.volume, 1.0)


class RippleEditTests(unittest.TestCase):
    def _three_clip_track(self):
        project = Project()
        track = project.tracks[0]
        a = Clip(media_type=MediaType.VIDEO, source_in=0, source_out=5, timeline_start=0)  # 0-5
        b = Clip(media_type=MediaType.VIDEO, source_in=0, source_out=3, timeline_start=5)  # 5-8
        c = Clip(media_type=MediaType.VIDEO, source_in=0, source_out=2, timeline_start=8)  # 8-10
        track.clips.extend([a, b, c])
        return project, track, a, b, c

    def test_ripple_delete_shifts_only_later_same_track_clips(self):
        project, track, a, b, c = self._three_clip_track()
        other_track = project.add_track(TrackType.VIDEO)
        other_clip = Clip(media_type=MediaType.VIDEO, source_in=0, source_out=5, timeline_start=8)
        other_track.clips.append(other_clip)
        history = History()

        history.execute(build_ripple_delete_command(project, a.id))
        self.assertEqual(len(track.clips), 2)
        self.assertAlmostEqual(b.timeline_start, 0.0)  # was 5, shifted left by a's duration (5)
        self.assertAlmostEqual(c.timeline_start, 3.0)  # was 8, shifted left by 5
        self.assertAlmostEqual(other_clip.timeline_start, 8.0)  # different track: untouched

    def test_ripple_delete_of_the_last_clip_shifts_nothing(self):
        project, track, a, b, c = self._three_clip_track()
        history = History()
        history.execute(build_ripple_delete_command(project, c.id))
        self.assertAlmostEqual(a.timeline_start, 0.0)
        self.assertAlmostEqual(b.timeline_start, 5.0)

    def test_ripple_delete_undo_restores_everything_in_one_step(self):
        project, track, a, b, c = self._three_clip_track()
        history = History()
        history.execute(build_ripple_delete_command(project, a.id))
        history.undo()
        self.assertEqual(len(track.clips), 3)
        restored = {clip.id: clip.timeline_start for clip in track.clips}
        self.assertAlmostEqual(restored[b.id], 5.0)
        self.assertAlmostEqual(restored[c.id], 8.0)

    def test_ripple_trim_shortening_shifts_later_clips_left(self):
        project, track, a, b, c = self._three_clip_track()
        history = History()
        # Shorten a's right edge from 5s to 3s duration (source_out 5 -> 3).
        history.execute(build_ripple_trim_command(project, a.id, source_in=0.0, source_out=3.0, timeline_start=0.0))
        self.assertAlmostEqual(a.timeline_end, 3.0)
        self.assertAlmostEqual(b.timeline_start, 3.0)  # was 5, shifted left by 2 (the shortened amount)
        self.assertAlmostEqual(c.timeline_start, 6.0)  # was 8, shifted left by 2

    def test_ripple_trim_lengthening_shifts_later_clips_right(self):
        project, track, a, b, c = self._three_clip_track()
        history = History()
        # Lengthen a's right edge from 5s to 7s duration (source_out 5 -> 7).
        history.execute(build_ripple_trim_command(project, a.id, source_in=0.0, source_out=7.0, timeline_start=0.0))
        self.assertAlmostEqual(b.timeline_start, 7.0)  # was 5, shifted right by 2
        self.assertAlmostEqual(c.timeline_start, 10.0)  # was 8, shifted right by 2

    def test_ripple_trim_undo_restores_everything_in_one_step(self):
        project, track, a, b, c = self._three_clip_track()
        history = History()
        history.execute(build_ripple_trim_command(project, a.id, source_in=0.0, source_out=3.0, timeline_start=0.0))
        history.undo()
        self.assertAlmostEqual(a.timeline_end, 5.0)
        self.assertAlmostEqual(b.timeline_start, 5.0)
        self.assertAlmostEqual(c.timeline_start, 8.0)

    def test_ripple_trim_with_no_net_duration_change_is_a_plain_trim(self):
        project, track, a, b, c = self._three_clip_track()
        history = History()
        # Same net duration (5s), just a different in/out pair (e.g. sliding
        # the trim window) — nothing downstream should move.
        history.execute(build_ripple_trim_command(project, a.id, source_in=1.0, source_out=6.0, timeline_start=0.0))
        self.assertAlmostEqual(b.timeline_start, 5.0)
        self.assertAlmostEqual(c.timeline_start, 8.0)


class MarkerCommandTests(unittest.TestCase):
    def test_add_marker_and_undo(self):
        project = Project()
        history = History()
        history.execute(AddMarkerCommand(project, 5.0, "Chorus"))
        self.assertEqual(len(project.markers), 1)
        self.assertEqual(project.markers[0].name, "Chorus")
        history.undo()
        self.assertEqual(len(project.markers), 0)
        history.redo()
        self.assertEqual(len(project.markers), 1)

    def test_remove_marker_and_undo_restores_it(self):
        project = Project()
        marker = project.add_marker(3.0, "Verse")
        history = History()
        history.execute(RemoveMarkerCommand(project, marker.id))
        self.assertEqual(len(project.markers), 0)
        history.undo()
        self.assertEqual(len(project.markers), 1)
        self.assertEqual(project.markers[0].name, "Verse")

    def test_rename_marker_and_undo(self):
        project = Project()
        marker = project.add_marker(1.0, "Old Name")
        history = History()
        history.execute(RenameMarkerCommand(project, marker.id, "New Name"))
        self.assertEqual(project.find_marker(marker.id).name, "New Name")
        history.undo()
        self.assertEqual(project.find_marker(marker.id).name, "Old Name")


class HistoryStackTests(unittest.TestCase):
    def test_redo_cleared_after_new_command(self):
        project, track, clip = make_project_with_clip()
        history = History()
        history.execute(UpdateClipPropertyCommand(project, clip.id, "volume", 0.5))
        history.undo()
        self.assertTrue(history.can_redo)
        history.execute(UpdateClipPropertyCommand(project, clip.id, "volume", 0.8))
        self.assertFalse(history.can_redo)


class SplitIdAndKeyframeTests(unittest.TestCase):
    """Split used `id + "_b"` for the new half (colliding on a repeated
    split) and copied every keyframe verbatim into both halves, so the
    second half replayed the animation from the clip's original start."""

    def test_repeated_splits_produce_unique_ids(self):
        project, track, clip = make_project_with_clip()
        history = History()
        history.execute(SplitClipCommand(project, clip.id, split_time=6.0))
        history.execute(SplitClipCommand(project, clip.id, split_time=3.0))  # split the first half again
        history.execute(SplitClipCommand(project, clip.id, split_time=1.0))
        ids = [c.id for c in track.clips]
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4)
        self.assertFalse(any(i.endswith("_b") for i in ids))

    def test_redo_recreates_the_same_second_half_id(self):
        project, track, clip = make_project_with_clip()
        history = History()
        history.execute(SplitClipCommand(project, clip.id, split_time=4.0))
        second_id = next(c.id for c in track.clips if c.id != clip.id)
        history.undo()
        history.redo()
        self.assertIsNotNone(track.find_clip(second_id))

    def test_opacity_and_position_keyframes_are_rebased(self):
        project, track, clip = make_project_with_clip()
        clip.opacity_keyframes = [(0.0, 0.0), (2.0, 1.0), (8.0, 0.5)]
        clip.position_keyframes = [(0.0, 0.0, 0.0), (10.0, 1.0, 0.5)]
        History().execute(SplitClipCommand(project, clip.id, split_time=4.0))
        first, second = sorted(track.clips, key=lambda c: c.timeline_start)

        # First half: keyframes before the cut, plus the value at the cut.
        self.assertEqual(first.opacity_keyframes[:2], [(0.0, 0.0), (2.0, 1.0)])
        t, v = first.opacity_keyframes[-1]
        self.assertAlmostEqual(t, 4.0)
        self.assertAlmostEqual(v, 1.0 - 0.5 * (2.0 / 6.0))
        # Second half: re-based so the cut is t=0, continuing from the same value.
        self.assertAlmostEqual(second.opacity_keyframes[0][0], 0.0)
        self.assertAlmostEqual(second.opacity_keyframes[0][1], v)
        self.assertEqual(second.opacity_keyframes[1], (4.0, 0.5))
        # Position: linear 0..1 over 10s -> (0.4, 0.2) at the cut.
        self.assertEqual(second.position_keyframes[0][0], 0.0)
        self.assertAlmostEqual(second.position_keyframes[0][1], 0.4)
        self.assertAlmostEqual(second.position_keyframes[0][2], 0.2)
        self.assertEqual(second.position_keyframes[-1], (6.0, 1.0, 0.5))
        self.assertAlmostEqual(first.position_keyframes[-1][1], 0.4)

    def test_half_with_no_keyframes_of_its_own_holds_the_animated_value(self):
        project, track, clip = make_project_with_clip()
        clip.opacity = 1.0
        clip.opacity_keyframes = [(0.0, 0.2), (2.0, 0.6)]
        History().execute(SplitClipCommand(project, clip.id, split_time=5.0))
        second = max(track.clips, key=lambda c: c.timeline_start)
        # Still 2+ keyframes (so the scalar opacity=1.0 doesn't take over), holding 0.6.
        self.assertGreaterEqual(len(second.opacity_keyframes), 2)
        self.assertTrue(all(abs(v - 0.6) < 1e-9 for _t, v in second.opacity_keyframes))

    def test_unanimated_clip_keeps_empty_keyframes(self):
        project, track, clip = make_project_with_clip()
        History().execute(SplitClipCommand(project, clip.id, split_time=4.0))
        for c in track.clips:
            self.assertEqual(c.opacity_keyframes, [])
            self.assertEqual(c.speed_keyframes, [])

    def test_speed_ramped_split_uses_the_ramped_source_offset(self):
        project = Project()
        track = project.tracks[0]
        # Source 0-4 at 1x (4s on the timeline), then 4-10 at 2x (3s).
        clip = Clip(
            media_type=MediaType.VIDEO, source_in=0, source_out=10, timeline_start=0,
            speed_keyframes=[(0.0, 1.0), (4.0, 2.0)],
        )
        track.clips.append(clip)
        self.assertAlmostEqual(clip.duration, 7.0)

        History().execute(SplitClipCommand(project, clip.id, split_time=5.0))
        first, second = sorted(track.clips, key=lambda c: c.timeline_start)
        # 5s in = 4s of 1x + 1s of 2x -> source 6.0 (not 5.0 * speed).
        self.assertAlmostEqual(first.source_out, 6.0)
        self.assertAlmostEqual(second.source_in, 6.0)
        # Timeline length is preserved across the two halves.
        self.assertAlmostEqual(first.duration, 5.0)
        self.assertAlmostEqual(second.duration, 2.0)
        self.assertAlmostEqual(second.timeline_end, 7.0)
        # The second half is entirely 2x: collapses to a constant speed.
        self.assertEqual(second.speed_keyframes, [])
        self.assertAlmostEqual(second.speed, 2.0)

    def test_speed_ramp_split_before_the_boundary_keeps_both_segments_rebased(self):
        project = Project()
        track = project.tracks[0]
        clip = Clip(
            media_type=MediaType.VIDEO, source_in=0, source_out=10, timeline_start=0,
            speed_keyframes=[(0.0, 1.0), (4.0, 2.0)],
        )
        track.clips.append(clip)
        History().execute(SplitClipCommand(project, clip.id, split_time=2.0))
        first, second = sorted(track.clips, key=lambda c: c.timeline_start)
        self.assertAlmostEqual(first.source_out, 2.0)
        self.assertEqual(first.speed_keyframes, [])
        self.assertAlmostEqual(first.speed, 1.0)
        self.assertEqual(second.speed_keyframes, [(0.0, 1.0), (2.0, 2.0)])
        self.assertAlmostEqual(first.duration + second.duration, 7.0)


class TrackPropertyCommandTests(unittest.TestCase):
    """Mute/lock/hide used to be flipped directly on the model by the
    timeline header -- not undoable."""

    def test_toggle_is_undoable(self):
        from core.commands import UpdateTrackPropertyCommand

        project = Project()
        track = project.tracks[0]
        history = History()
        for attribute in ("muted", "locked", "hidden"):
            with self.subTest(attribute=attribute):
                history.execute(UpdateTrackPropertyCommand(project, track.id, attribute, True))
                self.assertTrue(getattr(track, attribute))
                history.undo()
                self.assertFalse(getattr(track, attribute))
                history.redo()
                self.assertTrue(getattr(track, attribute))


if __name__ == "__main__":
    unittest.main()
