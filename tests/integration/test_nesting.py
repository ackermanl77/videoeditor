"""Compound clips / nested sequences (F20), with the real FFmpeg."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType
from core.exporter import ExportSettings, Exporter
from core.history import History
from core.media_asset import MediaAsset
from core.nesting import (
    NestClipsCommand,
    NestingError,
    UnnestClipCommand,
    ensure_nested_renders,
    missing_renders,
    render_path,
)
from core.project import Project, ProjectSettings
from core.track import TrackType
from tests.integration import test_p2_real_ffmpeg as p2

FFMPEG = p2.FFMPEG
FFPROBE = p2.FFPROBE
_TMP = None
SRC = {}


def setUpModule():
    global _TMP
    if not FFMPEG:
        return
    _TMP = tempfile.mkdtemp(prefix="ve_nest_")
    for name, color in (("red", "red"), ("blue", "blue")):
        path = os.path.join(_TMP, f"{name}.mp4")
        subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=160x90:r=25:d=2",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:d=2", "-c:v", "libx264",
                        "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path],
                       check=True, capture_output=True)
        SRC[name] = path


def tearDownModule():
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)


def _project():
    project = Project()
    project.settings = ProjectSettings(width=160, height=90, fps=25.0)
    clips = {}
    for name in ("red", "blue"):
        asset = MediaAsset(path=SRC[name], duration=2.0, width=160, height=90, has_video=True, has_audio=True)
        project.add_media_asset(asset)
        clips[name] = asset
    v1 = project.tracks[0]
    v2 = project.add_track(TrackType.VIDEO)
    red = Clip(media_type=MediaType.VIDEO, source_path=SRC["red"], source_out=1.0, timeline_start=1.0,
               source_duration=2.0, asset_id=clips["red"].id, name="red")
    blue = Clip(media_type=MediaType.VIDEO, source_path=SRC["blue"], source_out=1.0, timeline_start=1.5,
                source_duration=2.0, asset_id=clips["blue"].id, name="blue", transform_scale=0.5)
    after = Clip(media_type=MediaType.VIDEO, source_path=SRC["blue"], source_out=0.5, timeline_start=3.0,
                 source_duration=2.0, asset_id=clips["blue"].id, name="after")
    v1.clips += [red, after]
    v2.clips.append(blue)
    return project, red, blue, after


def _frame(path, t):
    out = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    return out


def _mean_diff(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a))


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg not found")
class NestingTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ve_home_")
        patcher = patch.dict(os.environ, {"USERPROFILE": self.home, "HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_nesting_replaces_the_clips_and_the_export_looks_the_same(self):
        project, red, blue, after = _project()
        before_export = os.path.join(self.home, "before.mp4")
        Exporter(project).export(ExportSettings(output_path=before_export, width=160, height=90, fps=25.0))

        history = History()
        command = NestClipsCommand(project, [red.id, blue.id])
        history.execute(command)
        compound = command.compound
        self.assertEqual([c.name for c in project.tracks[0].sorted_clips()], [compound.name, "after"])
        self.assertEqual(project.tracks[-1].clips, [])
        self.assertAlmostEqual(compound.timeline_start, 1.0)
        self.assertAlmostEqual(compound.duration, 1.5)
        self.assertEqual(missing_renders(project), [compound])
        self.assertEqual(ensure_nested_renders(project), 1)
        self.assertTrue(os.path.isfile(compound.source_path))
        self.assertEqual(ensure_nested_renders(project), 0)  # cached

        after_export = os.path.join(self.home, "after.mp4")
        Exporter(project).export(ExportSettings(output_path=after_export, width=160, height=90, fps=25.0))
        for t in (1.2, 1.8, 2.3, 3.2):  # red alone, red + small blue, blue alone, the clip after
            self.assertLess(_mean_diff(_frame(before_export, t), _frame(after_export, t)), 6, t)

        history.undo()
        self.assertEqual({c.id for c in project.tracks[0].clips}, {red.id, after.id})
        self.assertEqual([c.id for c in project.tracks[-1].clips], [blue.id])

    def test_expand_puts_the_clips_back_and_refuses_when_it_isnt_exact(self):
        project, red, blue, _after = _project()
        nest = NestClipsCommand(project, [red.id, blue.id])
        nest.do()
        compound = nest.compound
        expand = UnnestClipCommand(project, compound.id)
        expand.do()
        v1, v2 = project.tracks[0], project.tracks[-1]
        self.assertEqual(sorted((c.name, round(c.timeline_start, 6)) for c in v1.clips),
                         [("after", 3.0), ("red", 1.0)])
        self.assertEqual([(c.name, round(c.timeline_start, 6), c.transform_scale) for c in v2.clips],
                         [("blue", 1.5, 0.5)])
        expand.undo()
        self.assertEqual([c.id for c in v1.clips if c.nested], [compound.id])
        compound.source_out = 1.0  # trimmed
        with self.assertRaisesRegex(NestingError, "trimmed"):
            UnnestClipCommand(project, compound.id)

    def test_selections_that_cant_be_nested(self):
        project, red, blue, after = _project()
        with self.assertRaisesRegex(NestingError, "Select"):
            NestClipsCommand(project, [])
        project.tracks[-1].locked = True
        with self.assertRaisesRegex(NestingError, "locked"):
            NestClipsCommand(project, [red.id, blue.id])
        project.tracks[-1].locked = False
        after.timeline_start = 1.2  # now inside the range on V1, not selected
        with self.assertRaisesRegex(NestingError, "in the way"):
            NestClipsCommand(project, [red.id, blue.id])

    def test_the_compound_clip_saves_and_re_renders_when_missing_or_the_settings_change(self):
        project, red, blue, _after = _project()
        NestClipsCommand(project, [red.id, blue.id]).do()
        ensure_nested_renders(project)
        path = os.path.join(self.home, "p.vproj")
        project.save(path)
        loaded = Project.load(path)
        compound = next(c for t in loaded.tracks for c in t.clips if c.nested)
        self.assertEqual(compound.nested, next(c for t in project.tracks for c in t.clips if c.nested).nested)
        os.remove(compound.source_path)
        out = os.path.join(self.home, "out.mp4")
        Exporter(loaded).export(ExportSettings(output_path=out, width=160, height=90, fps=25.0))  # re-renders
        self.assertTrue(os.path.isfile(compound.source_path))
        old = compound.source_path
        loaded.settings.width, loaded.settings.height = 320, 180
        self.assertNotEqual(render_path(compound.nested, loaded.settings), old)
        self.assertEqual(missing_renders(loaded), [compound])

    def test_the_menu_commands(self):
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PySide6 not installed")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        with patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
            window._autosave_timer.stop()
            try:
                project, red, blue, _after = _project()
                window.project = project
                window.history = window._new_history()
                window._load_project_into_panels()
                window.timeline.select_clips([red.id, blue.id])
                self.assertTrue(window.nest_selected_clips())
                compound = next(c for t in project.tracks for c in t.clips if c.nested)
                self.assertTrue(os.path.isfile(compound.source_path))
                self.assertEqual(window.preview.media_path_for(compound), compound.source_path)
                self.assertEqual(window.timeline.selected_clip_ids, {compound.id})
                self.assertTrue(window.expand_compound_clip())
                self.assertFalse(any(c.nested for t in project.tracks for c in t.clips))
                window.history.undo()
                window.history.undo()
                self.assertEqual({c.id for c in project.tracks[0].clips} | {c.id for c in project.tracks[-1].clips},
                                 {red.id, blue.id, _after.id})
            finally:
                window._clear_autosave()
                window.deleteLater()
                app.processEvents()


if __name__ == "__main__":
    unittest.main()
