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


if __name__ == "__main__":
    unittest.main()
