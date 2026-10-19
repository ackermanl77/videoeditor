"""Audio track mixer (F31): fader, pan, mute/solo in the export mix, the meters and the dock."""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
import unittest
from array import array
from unittest.mock import patch

from core.clip import Clip, MediaType
from core.filter_graph import build_filter_graph
from core.history import History
from core.mixer import pan_gains, track_audible, track_filter, track_levels
from core.project import Project, ProjectSettings
from core.track import Track, TrackType
from tests.integration import test_p2_real_ffmpeg as p2

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PYSIDE6_AVAILABLE = False


def _two_audio_tracks(path_a="/m/a.wav", path_b="/m/b.wav"):
    project = Project()
    first = next(t for t in project.tracks if t.track_type == TrackType.AUDIO)
    second = Track(track_type=TrackType.AUDIO, name="Music")
    project.tracks.append(second)
    first.clips.append(Clip(media_type=MediaType.AUDIO, source_path=path_a, source_in=0, source_out=2, timeline_start=0))
    second.clips.append(Clip(media_type=MediaType.AUDIO, source_path=path_b, source_in=0, source_out=2, timeline_start=0))
    return project, first, second


class CoreTests(unittest.TestCase):
    def test_neutral_tracks_add_nothing_and_settings_round_trip(self):
        track = Track(track_type=TrackType.AUDIO)
        self.assertEqual(track_filter(track), "")
        track.gain_db, track.pan, track.solo = -6.0, -0.5, True
        self.assertEqual(track_filter(track), "volume=0.501187,pan=stereo|c0=1.0000*c0|c1=0.5000*c1")
        self.assertEqual(pan_gains(1.0), (0.0, 1.0))
        restored = Track.from_dict(track.to_dict())
        self.assertEqual((restored.gain_db, restored.pan, restored.solo), (-6.0, -0.5, True))
        data = track.to_dict()
        data.update(gain_db=99, pan="left")
        self.assertEqual((Track.from_dict(data).gain_db, Track.from_dict(data).pan), (12.0, 0.0))

    def test_solo_and_mute(self):
        project, first, second = _two_audio_tracks()
        self.assertTrue(track_audible(first, project.tracks))
        second.solo = True
        self.assertFalse(track_audible(first, project.tracks))
        self.assertTrue(track_audible(second, project.tracks))
        second.muted = True
        self.assertFalse(track_audible(second, project.tracks))  # mute wins over solo

    def test_export_graph_applies_fader_pan_and_solo(self):
        project, first, second = _two_audio_tracks()
        second.gain_db = -12.0
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("volume=0.251189[mix0]", graph.filter_complex)
        self.assertEqual(graph.filter_complex.count("/m/"), 0)  # paths live in the inputs
        self.assertEqual(len(graph.input_specs), 2)
        first.solo = True
        graph = build_filter_graph(project, ProjectSettings())
        self.assertEqual(len(graph.input_specs), 1)  # the unsoloed track isn't even read

    def test_meter_levels_follow_fader_pan_and_solo(self):
        project, first, second = _two_audio_tracks()
        second.gain_db = -6.0
        levels = track_levels(project, 1.0, lambda clip, offset: (0.8, 0.4))
        self.assertEqual(levels[first.id], (0.8, 0.4))
        self.assertAlmostEqual(levels[second.id][0], 0.8 * 10 ** (-6 / 20), places=4)
        first.solo = True
        self.assertEqual(track_levels(project, 1.0, lambda clip, offset: (0.8, 0.4))[second.id], (0.0, 0.0))


@unittest.skipUnless(p2.FFMPEG, "FFmpeg not available")
class RealFfmpegTests(unittest.TestCase):
    def test_hard_left_pan_silences_the_right_channel(self):
        folder = tempfile.mkdtemp(prefix="ve_mixer_")
        tone = os.path.join(folder, "tone.wav")
        subprocess.run([p2.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                        "-ac", "2", tone], check=True)
        project, first, second = _two_audio_tracks(tone, tone)
        second.muted = True
        first.pan = -1.0
        graph = build_filter_graph(project, ProjectSettings())
        command = [p2.FFMPEG, "-v", "error"]
        for spec in graph.input_specs:
            command += spec.pre_args + ["-i", spec.path]
        command += ["-filter_complex", f"{graph.filter_complex};[{graph.video_out_label}]nullsink", "-map", f"[{graph.audio_out_label}]",
                    "-ac", "2", "-ar", "8000", "-f", "f32le", "-"]
        samples = array("f", subprocess.run(command, check=True, capture_output=True).stdout)
        left = math.sqrt(sum(v * v for v in samples[0::2][800:]) / max(1, len(samples[0::2][800:])))
        right = math.sqrt(sum(v * v for v in samples[1::2][800:]) / max(1, len(samples[1::2][800:])))
        graph.cleanup()
        self.assertGreater(left, 0.05)
        self.assertLess(right, left * 0.01)


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dock_edits_tracks_with_undo(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        self.addCleanup(window.deleteLater)
        self.assertEqual(window.mixer_action.shortcut().toString(), "Ctrl+Alt+A")
        panel = window.mixer_panel
        panel.refresh()
        track = next(t for t in window.project.tracks if t.track_type == TrackType.AUDIO)
        strip = panel.strips[track.id]
        strip.fader.setValue(-12)  # keyboard-style change: committed at once
        self.assertEqual(track.gain_db, -6.0)
        self.assertEqual(strip.gain_label.text(), "-6.0 dB")
        strip.solo_btn.setChecked(True)
        self.assertTrue(track.solo)
        window.undo()
        self.assertFalse(track.solo)
        window.undo()
        self.assertEqual(track.gain_db, 0.0)
        self.assertEqual(strip.fader.value(), 0)  # the strip follows undo


if __name__ == "__main__":
    unittest.main()
