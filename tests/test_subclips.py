"""Subclips (F29): the model, ranges from a clip and the timeline In/Out, saving, and the Media Bin."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType
from core.history import History
from core.media_asset import MediaAsset
from core.project import Project
from core.subclips import AddSubclipCommand, RemoveSubclipCommand, RenameSubclipCommand, Subclip, media_item, subclip_range
from core.track import TrackType

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PYSIDE6_AVAILABLE = False


def _asset():
    return MediaAsset(path="/m/interview.mp4", duration=60.0, has_video=True, has_audio=True)


class ModelTests(unittest.TestCase):
    def test_ranges_from_a_clip_and_the_in_out(self):
        clip = Clip(media_type=MediaType.VIDEO, source_path="/m/a.mp4", source_in=10.0, source_out=20.0, timeline_start=5.0)
        self.assertEqual(subclip_range(clip), (10.0, 20.0))
        self.assertEqual(subclip_range(clip, 8.0, 12.0), (13.0, 17.0))  # the part inside In/Out
        self.assertIsNone(subclip_range(clip, 30.0, 40.0))  # In/Out miss the clip
        clip.speed = 2.0  # 10 s of source in 5 s of timeline
        self.assertEqual(subclip_range(clip, 6.0, 8.0), (12.0, 16.0))
        reverse = Clip(media_type=MediaType.VIDEO, source_path="/m/a.mp4", source_in=10.0, source_out=20.0, timeline_start=0.0, reverse=True)
        self.assertEqual(subclip_range(reverse, 0.0, 4.0), (16.0, 20.0))  # the start of a reversed clip shows the end

    def test_media_item_resolution_save_and_undo(self):
        project = Project()
        asset = _asset()
        project.add_media_asset(asset)
        subclip = Subclip(asset.id, 12.5, 30.0, "Best answer")
        history = History()
        history.execute(AddSubclipCommand(project, subclip))
        self.assertEqual(media_item(project, subclip.item_id), (asset, 12.5, 30.0))
        self.assertEqual(media_item(project, asset.id), (asset, 0.0, None))
        self.assertEqual(media_item(project, "sub:nope")[0], None)
        restored = Project.from_dict(project.to_dict())
        self.assertEqual([(s.name, s.source_in, s.source_out) for s in restored.subclips], [("Best answer", 12.5, 30.0)])
        history.execute(RenameSubclipCommand(project, subclip.id, "Quote"))
        self.assertEqual(project.subclips[0].name, "Quote")
        history.execute(RemoveSubclipCommand(project, subclip.id))
        self.assertEqual(project.subclips, [])
        history.undo()
        history.undo()
        self.assertEqual(project.subclips[0].name, "Best answer")
        project.remove_media_asset(asset.id)
        self.assertEqual(project.subclips, [])  # a removed file takes its subclips along
        data = restored.to_dict()
        data["subclips"].append({"asset_id": asset.id, "source_in": 5, "source_out": 5})  # empty range
        self.assertEqual(len(Project.from_dict(data).subclips), 1)


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_make_subclip_then_place_it(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        self.addCleanup(window.deleteLater)
        asset = _asset()
        window.project.add_media_asset(asset)
        video = next(t for t in window.project.tracks if t.track_type == TrackType.VIDEO)
        clip = Clip(media_type=MediaType.VIDEO, source_path=asset.path, source_in=0.0, source_out=20.0, timeline_start=0.0,
                    source_duration=60.0, asset_id=asset.id)
        video.clips.append(clip)
        window.timeline.rebuild()
        window.timeline.select_clips([clip.id])
        window.timeline.set_playhead(4.0)
        window.timeline.set_in_point()
        window.timeline.set_playhead(9.0)
        window.timeline.set_out_point()
        self.assertTrue(window.make_subclip("Laugh"))
        subclip = window.project.subclips[0]
        self.assertEqual((subclip.source_in, subclip.source_out, subclip.name), (4.0, 9.0, "Laugh"))
        items = [window.media_bin.list_widget.item(i) for i in range(window.media_bin.list_widget.count())]
        from PySide6.QtCore import Qt

        self.assertIn(subclip.item_id, [item.data(Qt.UserRole) for item in items])
        self.assertEqual(window.media_bin.selected_asset_id(), subclip.item_id)  # revealed
        window.timeline.set_playhead(30.0)
        window._place_selected_asset(overwrite=True)
        placed = [c for c in video.clips if abs(c.timeline_start - 30.0) < 1e-6][0]
        self.assertEqual((placed.source_in, placed.source_out), (4.0, 9.0))
        window._add_asset_to_timeline_end(subclip.item_id)
        appended = max(video.clips, key=lambda c: c.timeline_start)
        self.assertEqual((appended.source_in, appended.source_out), (4.0, 9.0))
        window.undo()
        window.undo()
        window.undo()  # the subclip itself
        self.assertEqual(window.project.subclips, [])
        self.assertEqual(window.media_bin.list_widget.count(), 1)


if __name__ == "__main__":
    unittest.main()
