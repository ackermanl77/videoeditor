"""OpenTimelineIO export/import (F21). The two fixtures in tests/data/otio were written by the real
opentimelineio 0.18.1 (Python 3.11): one re-serializes a file this module exported, one is a
timeline built natively with the library (a transition, a gap, a reversed clip, a marker) -- media
paths neutralised to C:/media/shots.mp4. (Separately checked: the library reads this module's export
with the right track kinds, trims, gap, LinearTimeWarp, text generator and marker colour.)"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from core.clip import Clip, MediaType, TextStyle
from core.effects import EffectInstance
from core.media_asset import MediaAsset
from core.otio_io import export_otio, import_otio, otio_to_project, project_to_otio
from core.project import Project, ProjectSettings

DATA = os.path.join(os.path.dirname(__file__), "data", "otio")
MEDIA = "C:/media/shots.mp4"


def _project() -> Project:
    asset = MediaAsset(path=MEDIA, duration=6.0, width=320, height=180, has_video=True, has_audio=True)
    project = Project(name="Cross Check")
    project.settings = ProjectSettings(width=320, height=180, fps=25.0)
    project.add_media_asset(asset)
    fast = Clip(media_type=MediaType.VIDEO, source_path=MEDIA, name="Fast part", source_in=1.0, source_out=5.0,
                timeline_start=0.0, source_duration=6.0, asset_id=asset.id, speed=2.0)
    fast.effects = [EffectInstance.create("color_correction", brightness=0.2)]
    project.tracks[0].clips.append(fast)
    project.tracks[0].clips.append(Clip(media_type=MediaType.TEXT, source_path="", name="Title",
                                        text_style=TextStyle(content="Hello OTIO"), source_in=0.0, source_out=1.5,
                                        timeline_start=3.0, source_duration=1.5))
    project.tracks[1].clips.append(Clip(media_type=MediaType.AUDIO, source_path=MEDIA, name="Music", source_in=0.0,
                                        source_out=4.0, timeline_start=1.0, source_duration=6.0, asset_id=asset.id))
    project.add_marker(0.5, "Intro")
    project.markers[0].color = "#22c55e"
    return project


def _summary(project: Project):
    return [
        (track.track_type.value, [(c.name, c.media_type.value, round(c.timeline_start, 3), round(c.source_in, 3),
                                   round(c.source_out, 3), c.speed, c.reverse) for c in track.sorted_clips()])
        for track in project.tracks
    ]


class OtioExportTests(unittest.TestCase):
    def test_the_exported_json_uses_otios_core_schemas(self):
        data = project_to_otio(_project())
        self.assertEqual(data["OTIO_SCHEMA"], "Timeline.1")
        video, audio = data["tracks"]["children"]
        self.assertEqual((video["kind"], audio["kind"]), ("Video", "Audio"))
        fast, gap, title = video["children"]
        self.assertEqual(fast["OTIO_SCHEMA"], "Clip.2")
        self.assertEqual(fast["media_references"]["DEFAULT_MEDIA"]["target_url"], "file:///C:/media/shots.mp4")
        self.assertEqual(fast["source_range"]["start_time"]["value"], 25.0)  # 1.0 s at 25 fps
        self.assertEqual(fast["source_range"]["duration"]["value"], 50.0)  # 2.0 s on the timeline
        self.assertEqual(fast["effects"], [{"OTIO_SCHEMA": "LinearTimeWarp.1", "name": "", "effect_name": "LinearTimeWarp",
                                            "time_scalar": 2.0, "metadata": {}}])
        self.assertEqual((gap["OTIO_SCHEMA"], gap["source_range"]["duration"]["value"]), ("Gap.1", 25.0))
        self.assertEqual(title["media_references"]["DEFAULT_MEDIA"]["generator_kind"], "text")
        self.assertEqual(data["tracks"]["markers"][0]["color"], "GREEN")

    def test_a_round_trip_through_this_editor_is_lossless(self):
        original = _project()
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "timeline.otio")
            export_otio(original, path)
            again = import_otio(path)
        self.assertEqual(_summary(again), _summary(original))
        self.assertEqual(again.tracks[0].sorted_clips()[0].effects[0].params["brightness"], 0.2)  # via our metadata
        self.assertEqual(again.tracks[0].sorted_clips()[1].text_style.content, "Hello OTIO")
        self.assertEqual([(m.name, m.time, m.color) for m in again.markers], [("Intro", 0.5, "#22c55e")])
        self.assertEqual(again.settings.fps, 25.0)
        self.assertTrue(any("Missing media" in w for w in again.load_warnings))  # C:/media isn't here


class OtioImportTests(unittest.TestCase):
    def test_a_file_reserialized_by_the_otio_library_imports_the_same(self):
        project = import_otio(os.path.join(DATA, "library_reserialized.otio"))
        self.assertEqual(_summary(project), _summary(_project()))

    def test_a_timeline_written_by_another_program_imports_from_otio_alone(self):
        project = import_otio(os.path.join(DATA, "library_native.otio"))
        self.assertEqual(project.settings.fps, 30.0)
        self.assertEqual(_summary(project), [("video", [("A", "video", 0.0, 1.0, 3.0, 1.0, False),
                                                        ("B", "video", 2.5, 3.0, 4.5, 1.0, True)])])
        self.assertTrue(any("transition" in w for w in project.load_warnings))
        self.assertEqual([(m.name, m.time) for m in project.markers], [("M", 2.0)])
        self.assertEqual(project.media_pool[0].duration, 6.0)  # from available_range: the file isn't here

    def test_speed_from_a_time_warp_and_bad_files(self):
        data = json.load(open(os.path.join(DATA, "library_native.otio"), encoding="utf-8"))
        clip = data["tracks"]["children"][0]["children"][0]
        clip["effects"] = [{"OTIO_SCHEMA": "LinearTimeWarp.1", "time_scalar": 2.0}]
        fast = otio_to_project(data).tracks[0].sorted_clips()[0]
        self.assertEqual((fast.speed, fast.source_in, fast.source_out, round(fast.duration, 3)), (2.0, 1.0, 5.0, 2.0))
        with self.assertRaises(ValueError):
            otio_to_project({"OTIO_SCHEMA": "Clip.2"})
        with tempfile.TemporaryDirectory() as folder:
            bad = os.path.join(folder, "bad.otio")
            open(bad, "w").write("{not json")
            with self.assertRaises(ValueError):
                import_otio(bad)


class OtioMenuTests(unittest.TestCase):
    def test_export_then_import_through_the_window(self):
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PySide6 not installed")
        import shutil
        from unittest.mock import patch

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), \
                patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
            window._autosave_timer.stop()
            try:
                window.project = _project()
                path = window.export_otio_dialog(os.path.join(home, "out.otio"))
                self.assertTrue(path and os.path.isfile(path))
                window._dirty = False  # nothing to confirm discarding
                with patch("ui.main_window.QMessageBox"):
                    self.assertTrue(window.import_otio_dialog(path))
                self.assertEqual(_summary(window.project), _summary(_project()))
                self.assertIsNone(window.project.file_path)  # a new, unsaved project
            finally:
                window._clear_autosave()
                window.deleteLater()
                QApplication.clipboard().clear()
                app.processEvents()
        shutil.rmtree(home, ignore_errors=True)
