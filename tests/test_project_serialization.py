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


if __name__ == "__main__":
    unittest.main()
