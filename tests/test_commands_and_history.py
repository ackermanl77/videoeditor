"""Tests for the undo/redo command stack against real editing operations."""

import unittest

from core.clip import Clip, MediaType
from core.commands import (
    AddClipCommand,
    MoveClipCommand,
    RemoveClipCommand,
    SplitClipCommand,
    TrimClipCommand,
    UpdateClipPropertyCommand,
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
