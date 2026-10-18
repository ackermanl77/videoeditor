"""Smooth speed ramps (F22): a linear speed glide between keyframes, drawn as short steps."""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
import unittest

from core.clip import SMOOTH_RAMP_MAX_STEPS, Clip, MediaType
from core.commands import SplitClipCommand
from core.project import Project, ProjectSettings

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _clip(**kw):
    defaults = dict(media_type=MediaType.VIDEO, source_path="x.mp4", source_in=0.0, source_out=4.0,
                    source_duration=10.0, speed_keyframes=[(0.0, 1.0), (2.0, 3.0)], speed_ramp_smooth=True)
    defaults.update(kw)
    return Clip(**defaults)


def _true_glide_seconds(length, s0, s1):
    """Timeline seconds of a glide linear in source time from s0 to s1 over ``length`` source seconds."""
    return length / s0 if s0 == s1 else length * math.log(s1 / s0) / (s1 - s0)


class SmoothRampTests(unittest.TestCase):
    def test_the_glide_is_drawn_as_rising_steps_and_then_holds(self):
        clip = _clip()
        segments = clip.speed_segments
        glide = [s for s in segments if s[1] <= 2.0 + 1e-9]
        self.assertEqual(len(glide), 10)  # 2 s of source at 0.2 s per step
        speeds = [s[2] for s in glide]
        self.assertTrue(all(b > a for a, b in zip(speeds, speeds[1:])))
        self.assertTrue(1.0 < speeds[0] < 1.2 and 2.8 < speeds[-1] < 3.0)
        self.assertEqual(segments[-1], (2.0, 4.0, 3.0))  # after the last keyframe: hold
        # contiguous cover of the trimmed region
        self.assertEqual(segments[0][0], 0.0)
        self.assertTrue(all(abs(a[1] - b[0]) < 1e-9 for a, b in zip(segments, segments[1:])))

    def test_the_duration_matches_the_true_curve(self):
        clip = _clip()
        expected = _true_glide_seconds(2.0, 1.0, 3.0) + 2.0 / 3.0
        self.assertAlmostEqual(clip.duration, expected, places=9)
        stepped = _clip(speed_ramp_smooth=False)
        self.assertAlmostEqual(stepped.duration, 2.0 / 1.0 + 2.0 / 3.0)
        slow = _clip(speed_keyframes=[(0.0, 1.0), (3.0, 0.25)], source_out=3.0)
        self.assertAlmostEqual(slow.duration, _true_glide_seconds(3.0, 1.0, 0.25), places=9)

    def test_mapping_round_trips_and_the_speed_rises_on_the_timeline(self):
        clip = _clip()
        for offset in (0.0, 0.3, 0.9, 1.4, clip.duration - 0.01):
            source = clip.source_offset_at(offset)
            self.assertAlmostEqual(clip.timeline_offset_at(source), offset, places=9)
        self.assertLess(clip.speed_at(0.1), clip.speed_at(0.8))
        self.assertLess(clip.speed_at(0.8), clip.speed_at(1.15))

    def test_long_gaps_are_capped_and_equal_speeds_stay_one_segment(self):
        long_ramp = _clip(source_out=60.0, speed_keyframes=[(0.0, 0.5), (60.0, 2.0)])
        self.assertEqual(len(long_ramp.speed_segments), SMOOTH_RAMP_MAX_STEPS)
        flat = _clip(speed_keyframes=[(0.0, 2.0), (2.0, 2.0)])
        self.assertEqual(flat.speed_segments, [(0.0, 2.0, 2.0), (2.0, 4.0, 2.0)])

    def test_the_flag_round_trips_and_old_projects_stay_stepped(self):
        clip = _clip()
        self.assertTrue(Clip.from_dict(clip.to_dict()).speed_ramp_smooth)
        data = clip.to_dict()
        del data["speed_ramp_smooth"]
        self.assertFalse(Clip.from_dict(data).speed_ramp_smooth)

    def test_a_left_trim_keeps_the_glide_where_it_was(self):
        clip = _clip()
        trimmed = clip.with_source_range(1.0, 4.0)
        self.assertAlmostEqual(trimmed.speed_keyframes[0][1], 2.0)  # interpolated, not the step value 1.0
        self.assertAlmostEqual(trimmed.duration, _true_glide_seconds(1.0, 2.0, 3.0) + 2.0 / 3.0, places=9)

    def test_splitting_keeps_the_total_length_and_the_speed_at_the_cut(self):
        project = Project()
        clip = _clip()
        project.tracks[0].clips.append(clip)
        total = clip.duration
        cut_at = clip.timeline_offset_at(1.0)  # source 1.0 s, mid-glide (speed 2.0)
        SplitClipCommand(project, clip.id, clip.timeline_start + cut_at).do()
        first, second = sorted(project.tracks[0].clips, key=lambda c: c.timeline_start)
        self.assertAlmostEqual(first.duration + second.duration, total, places=6)
        self.assertAlmostEqual(first.speed_keyframes[-1][1], 2.0)
        self.assertAlmostEqual(second.speed_keyframes[0][1], 2.0)
        self.assertTrue(first.speed_ramp_smooth and second.speed_ramp_smooth)


class SmoothRampPanelTests(unittest.TestCase):
    def test_the_checkbox_toggles_the_ramp_as_one_undo_step(self):
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PySide6 not installed")
        from unittest.mock import patch

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}),                 patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
            window._autosave_timer.stop()
            try:
                clip = _clip(speed_ramp_smooth=False)
                window.project.tracks[0].clips.append(clip)
                window.timeline.rebuild()
                window.timeline.select_clips([clip.id])
                panel = window.properties
                self.assertFalse(panel.speed_smooth_checkbox.isChecked())
                panel.speed_smooth_checkbox.setChecked(True)
                self.assertTrue(window.project.find_clip(clip.id).speed_ramp_smooth)
                window.history.undo()
                self.assertFalse(window.project.find_clip(clip.id).speed_ramp_smooth)
            finally:
                window._clear_autosave()
                window.deleteLater()
                app.processEvents()
        shutil.rmtree(home, ignore_errors=True)


def _find(tool):
    bundled = os.path.join(REPO, "bin", tool + (".exe" if os.name == "nt" else ""))
    return bundled if os.path.isfile(bundled) else shutil.which(tool)


@unittest.skipUnless(_find("ffmpeg") and _find("ffprobe"), "FFmpeg not available")
class SmoothRampExportTests(unittest.TestCase):
    def test_a_smooth_ramp_exports_at_the_curve_s_length_with_audio(self):
        from core.exporter import ExportSettings, Exporter
        from core.media_asset import MediaAsset

        folder = tempfile.mkdtemp(prefix="ve_smooth_")
        self.addCleanup(shutil.rmtree, folder, True)
        source = os.path.join(folder, "src.mp4")
        subprocess.run([_find("ffmpeg"), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=30:d=4",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:d=4", "-c:v", "libx264",
                        "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", source],
                       check=True, capture_output=True)
        asset = MediaAsset(path=source, duration=4.0, width=160, height=120, has_video=True, has_audio=True)
        project = Project()
        project.settings = ProjectSettings(width=160, height=120, fps=30.0)
        project.add_media_asset(asset)
        clip = _clip(source_path=source, source_duration=4.0, asset_id=asset.id)
        project.tracks[0].clips.append(clip)
        out = os.path.join(folder, "out.mp4")
        Exporter(project).export(ExportSettings(output_path=out, width=160, height=120))
        probe = subprocess.run([_find("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type,duration",
                                "-of", "csv=p=0", out], check=True, capture_output=True, text=True).stdout
        durations = {kind: float(value) for kind, value in (line.split(",")[:2] for line in probe.split())
                     if value not in ("", "N/A")}
        self.assertIn("video", durations)
        self.assertIn("audio", durations)
        self.assertAlmostEqual(durations["video"], clip.duration, delta=0.1)
        self.assertAlmostEqual(durations["audio"], clip.duration, delta=0.15)


if __name__ == "__main__":
    unittest.main()
