"""Tests for Project save/load round-tripping."""

import os
import tempfile
import unittest

from core.clip import Clip, MediaType
from core.media_asset import MediaAsset
from core.project import Project


class ProjectSerializationTests(unittest.TestCase):
    def test_round_trip(self):
        project = Project(name="My Movie")
        project.settings.width = 1280
        project.settings.height = 720
        project.add_media_asset(MediaAsset(path="/tmp/clip.mp4", duration=12.0, has_video=True, has_audio=True))

        video_track = project.tracks[0]
        clip = Clip(media_type=MediaType.VIDEO, source_path="/tmp/clip.mp4", source_in=0, source_out=5, timeline_start=0)
        video_track.clips.append(clip)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.vproj")
            project.save(path)
            self.assertTrue(os.path.exists(path))

            loaded = Project.load(path)
            self.assertEqual(loaded.name, "My Movie")
            self.assertEqual(loaded.settings.width, 1280)
            self.assertEqual(len(loaded.media_pool), 1)
            self.assertEqual(loaded.media_pool[0].path, "/tmp/clip.mp4")
            loaded_track = loaded.tracks[0]
            self.assertEqual(len(loaded_track.clips), 1)
            self.assertEqual(loaded_track.clips[0].source_path, "/tmp/clip.mp4")

    def test_default_tracks_created(self):
        project = Project()
        self.assertGreaterEqual(len(project.tracks), 2)

    def test_find_asset_by_path(self):
        project = Project()
        asset = MediaAsset(path="/a/b/c.mp4")
        project.add_media_asset(asset)
        found = project.find_asset_by_path("/a/b/c.mp4")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, asset.id)

    def test_markers_round_trip(self):
        project = Project(name="Marker Test")
        m1 = project.add_marker(2.5, "Intro")
        m2 = project.add_marker(10.0)  # auto-named
        self.assertEqual(m2.name, "Marker 2")

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "markers.vproj")
            project.save(path)
            loaded = Project.load(path)

        self.assertEqual(len(loaded.markers), 2)
        loaded_m1 = loaded.find_marker(m1.id)
        self.assertIsNotNone(loaded_m1)
        self.assertEqual(loaded_m1.name, "Intro")
        self.assertAlmostEqual(loaded_m1.time, 2.5)
        self.assertEqual([m.time for m in loaded.sorted_markers()], [2.5, 10.0])

    def test_remove_marker(self):
        project = Project()
        marker = project.add_marker(5.0, "Cut")
        project.remove_marker(marker.id)
        self.assertEqual(len(project.markers), 0)
        self.assertIsNone(project.find_marker(marker.id))


if __name__ == "__main__":
    unittest.main()
