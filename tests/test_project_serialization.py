"""Tests for Project save/load round-tripping."""

import os
import tempfile
import unittest

from core.clip import Clip, MediaType
from core.media_asset import MediaAsset
from core.project import Project, ProjectFileError
from core.track import TrackType


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

    def test_track_ducking_config_round_trips_through_save_and_load(self):
        project = Project(name="Ducking Test")
        narration_track = project.tracks[1]
        music_track = project.add_track(TrackType.AUDIO, name="Music")
        music_track.duck_trigger_track_id = narration_track.id
        music_track.duck_threshold = 0.1

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "duck.vproj")
            project.save(path)
            loaded = Project.load(path)

        loaded_music = loaded.find_track(music_track.id)
        self.assertIsNotNone(loaded_music)
        self.assertEqual(loaded_music.duck_trigger_track_id, narration_track.id)
        self.assertAlmostEqual(loaded_music.duck_threshold, 0.1)


class AtomicSaveTests(unittest.TestCase):
    """Project.save() used to open the destination with "w" and dump into
    it -- a crash or error mid-write truncated the user's only copy."""

    def test_failed_save_leaves_the_previous_file_intact_and_no_temp_files(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.vproj")
            project = Project(name="Original")
            project.save(path)
            with open(path, "r", encoding="utf-8") as f:
                original = f.read()

            project.name = "Changed"
            with patch("core.project.json.dump", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    project.save(path)

            with open(path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), original)
            self.assertEqual(os.listdir(tmp), ["p.vproj"])

    def test_failed_save_as_does_not_change_file_path(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            project = Project()
            with patch("core.project.json.dump", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    project.save(os.path.join(tmp, "new.vproj"))
            self.assertIsNone(project.file_path)

    def test_save_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.vproj")
            Project(name="One").save(path)
            Project(name="Two").save(path)
            self.assertEqual(Project.load(path).name, "Two")
            self.assertEqual(os.listdir(tmp), ["p.vproj"])


class ProjectLoadLimitTests(unittest.TestCase):
    def test_load_rejects_a_project_over_the_size_limit(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "too-large.vproj")
            with open(path, "wb") as f:
                f.seek(1024 * 1024)
                f.write(b"x")

            with (
                patch("core.project.MAX_PROJECT_FILE_SIZE_BYTES", 1024 * 1024),
                self.assertRaisesRegex(ProjectFileError, "1 MiB size limit"),
            ):
                Project.load(path)

    def test_load_reports_malformed_json_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "malformed.vproj")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{")

            with self.assertRaisesRegex(ProjectFileError, "contains invalid JSON"):
                Project.load(path)

    def test_from_dict_rejects_an_oversized_collection(self):
        from unittest.mock import patch

        data = {"tracks": [{}, {}, {}]}
        with (
            patch("core.project.MAX_PROJECT_COLLECTION_ITEMS", 2),
            self.assertRaisesRegex(
                ProjectFileError, "collection contains too many items"
            ),
        ):
            Project.from_dict(data)

    def test_from_dict_rejects_too_many_items_across_collections(self):
        from unittest.mock import patch

        data = {"name": "Large", "nested": {"items": [1, 2]}}
        with (
            patch("core.project.MAX_PROJECT_COLLECTION_ITEMS", 10),
            patch("core.project.MAX_PROJECT_TOTAL_ITEMS", 3),
            self.assertRaisesRegex(ProjectFileError, "too many collection items"),
        ):
            Project.from_dict(data)

    def test_from_dict_rejects_excessive_nesting(self):
        from unittest.mock import patch

        with (
            patch("core.project.MAX_PROJECT_NESTING_DEPTH", 1),
            self.assertRaisesRegex(ProjectFileError, "nested too deeply"),
        ):
            Project.from_dict({"a": {"b": []}})

    def test_from_dict_rejects_non_object_json(self):
        with self.assertRaisesRegex(ProjectFileError, "JSON object at the top level"):
            Project.from_dict([])


if __name__ == "__main__":
    unittest.main()
