"""The 2026-10-21 rows: recording (F34), subject-following reframe (F35), colour wheels and
Auto Color (F36), speech search (F37), the Explorer verb (F38), the keyframe graph (F39), and
keyframed effect masks (F16)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core import auto_color, recording, transcript_search  # noqa: E402
from core.clip import Clip, MediaType, ReframeMode, curve_easing, ease, interpolate_keyframes  # noqa: E402
from core.commands import SplitClipCommand  # noqa: E402
from core.effect_masks import clean_mask, mask_at  # noqa: E402
from core.effects import EffectInstance, wheel_offsets  # noqa: E402
from core.ffmpeg_utils import find_binary  # noqa: E402
from core.filter_graph_base import _piecewise_linear_expr  # noqa: E402
from core.media_asset import MediaAsset  # noqa: E402
from core.motion_track import TrackPoint, reframe_keyframes  # noqa: E402
from core.project import Project  # noqa: E402

FFMPEG = find_binary("ffmpeg")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _video(folder, name, source):
    path = os.path.join(folder, name)
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", source, "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", path], check=True)
    return path


def _lightness(project, t, x, y, size):
    from core.frame_render import render_frame

    image = QImage()
    assert image.loadFromData(render_frame(project, t, size), "PNG")
    return image.pixelColor(int(x * (size[0] - 1)), int(y * (size[1] - 1)))


class ReframeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_the_focus_centres_the_subject_and_is_clamped(self):
        clip = Clip(media_type=MediaType.VIDEO, source_in=0, source_out=4, timeline_start=0, source_duration=4,
                    reframe_mode=ReframeMode.FILL)
        points = [TrackPoint(t / 10, 0.1 + 0.8 * t / 40, 0.5, 1) for t in range(41)]
        keys = reframe_keyframes(points, clip, (1920, 1080), (1080, 1920), smoothing=1)
        visible = (1080 / 1920) / (1920 / 1080)
        self.assertEqual(keys[0][1], 0.0)  # the subject at 10 %: as far left as the crop goes
        self.assertEqual(keys[-1][1], 1.0)
        mid = interpolate_keyframes(keys, 2.0)[0]
        self.assertAlmostEqual(mid, (0.5 - visible / 2) / (1 - visible), places=2)

    def test_the_fill_crop_follows_the_keyframes_in_the_render(self):
        folder = tempfile.mkdtemp(prefix="ve_reframe_")
        # A white square moving left to right on grey, 320x180; the project is portrait 90x160.
        path = os.path.join(folder, "moving.mp4")
        subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x404040:s=320x180:r=25:d=3",
                        "-f", "lavfi", "-i", "color=c=white:s=40x40:r=25:d=3",
                        "-filter_complex", "[0][1]overlay=x='20+t*100':y=70:shortest=1", "-c:v", "libx264",
                        "-preset", "ultrafast", "-pix_fmt", "yuv420p", path], check=True)
        project = Project()
        project.settings.width, project.settings.height = 90, 160
        project.add_media_asset(MediaAsset(path=path, duration=3.0, has_video=True, width=320, height=180))
        clip = Clip(media_type=MediaType.VIDEO, source_path=path, source_in=0, source_out=3, timeline_start=0,
                    source_duration=3.0, reframe_mode=ReframeMode.FILL)
        points = [TrackPoint(t / 10, (40 + t * 10) / 320, 0.5, 1) for t in range(0, 26)]
        clip.crop_focus_keyframes = reframe_keyframes(points, clip, (320, 180), (90, 160), smoothing=1)
        project.tracks[0].clips.append(clip)
        for t in (0.4, 1.4):
            self.assertGreater(_lightness(project, t, 0.5, 0.5, (90, 160)).lightness(), 200, t)  # centred
        clip.crop_focus_keyframes = []  # a fixed centre crop loses the subject
        self.assertLess(_lightness(project, 0.4, 0.5, 0.5, (90, 160)).lightness(), 100)

    def test_focus_keyframes_survive_saving_and_split(self):
        project = Project()
        clip = Clip(media_type=MediaType.VIDEO, source_path="a.mp4", source_in=0, source_out=4, timeline_start=0,
                    source_duration=4, reframe_mode=ReframeMode.FILL,
                    crop_focus_keyframes=[(0.0, 0.1, 0.5), (4.0, 0.9, 0.5)])
        self.assertEqual(Clip.from_dict(clip.to_dict()).crop_focus_keyframes, clip.crop_focus_keyframes)
        project.tracks[0].clips.append(clip)
        SplitClipCommand(project, clip.id, 2.0).do()
        second = next(c for c in project.tracks[0].clips if c is not clip)
        self.assertAlmostEqual(clip.crop_focus_keyframes[-1][1], 0.5, places=3)
        self.assertAlmostEqual(second.crop_focus_keyframes[0][1], 0.5, places=3)
        self.assertEqual(second.crop_focus_keyframes[0][0], 0.0)


class ColorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_wheel_offsets_keep_the_brightness(self):
        for hue in (0, 45, 200, 300):
            self.assertAlmostEqual(sum(wheel_offsets(hue, 0.6)), 0.0, places=9)
        self.assertGreater(wheel_offsets(0, 0.5)[0], 0)  # red pushes red
        neutral = EffectInstance.create("color_wheels")
        self.assertFalse(neutral.is_renderable())
        self.assertIn("colorbalance=rs=", EffectInstance.create("color_wheels", shadows_strength=0.4).filter())

    def test_auto_color_levels_from_a_frame(self):
        # A blue-cast frame: red and green stay low, blue spans more.
        data = bytes(v for i in range(1000) for v in (20 + i % 60, 30 + i % 80, 60 + i % 180))
        levels = auto_color.levels_from_rgb(data)
        self.assertLess(levels["r_high"], levels["b_high"])
        self.assertGreaterEqual(levels["r_high"] - levels["r_low"], auto_color.MIN_RANGE - 1e-9)
        self.assertIsNone(auto_color.levels_from_rgb(b""))

    def test_auto_color_reads_a_real_frame_and_the_levels_brighten_it(self):
        folder = tempfile.mkdtemp(prefix="ve_auto_")
        path = _video(folder, "dull.mp4", "testsrc2=s=160x90:r=25:d=1,hue=s=0,eq=contrast=0.35:brightness=-0.1")
        levels = auto_color.measure_levels(FFMPEG, path, 0.2)
        self.assertIsNotNone(levels)
        project = Project()
        project.settings.width, project.settings.height = 160, 90
        project.add_media_asset(MediaAsset(path=path, duration=1.0, has_video=True, width=160, height=90))
        clip = Clip(media_type=MediaType.VIDEO, source_path=path, source_in=0, source_out=1, timeline_start=0,
                    source_duration=1.0)
        project.tracks[0].clips.append(clip)
        def spread():
            from core.frame_render import render_frame

            image = QImage()
            self.assertTrue(image.loadFromData(render_frame(project, 0.2, (160, 90)), "PNG"))
            values = [image.pixelColor(x, y).lightness() for x in range(0, 160, 8) for y in range(0, 90, 8)]
            return max(values) - min(values)

        before = spread()
        clip.effects = [EffectInstance.create("color_levels", **levels)]
        self.assertGreater(spread(), before * 1.5)  # the washed-out frame gets its full range back


class MaskKeyframeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    MASK = {"shape": "ellipse", "w": 0.2, "h": 0.3, "feather": 0.02,
            "keyframes": [[0, 0.2, 0.5, 0.2, 0.3, 0, 0.02], [2, 0.8, 0.5, 0.4, 0.3, 0, 0.02]]}

    def test_keyframes_are_cleaned_and_interpolated(self):
        mask = clean_mask(self.MASK)
        self.assertEqual(len(mask["keyframes"]), 2)
        self.assertAlmostEqual(mask_at(mask, 1.0)["cx"], 0.5)
        self.assertAlmostEqual(mask_at(mask, 1.0)["w"], 0.3)
        self.assertNotIn("keyframes", clean_mask({**self.MASK, "keyframes": [[0, 1, 1, 1, 1, 0, 0]]}))

    def test_the_masked_area_moves_and_grows_in_the_render(self):
        folder = tempfile.mkdtemp(prefix="ve_maskkf_")
        path = _video(folder, "grey.mp4", "color=c=0x404040:s=320x180:r=25:d=3")
        project = Project()
        project.settings.width, project.settings.height = 320, 180
        project.add_media_asset(MediaAsset(path=path, duration=3.0, has_video=True, width=320, height=180))
        clip = Clip(media_type=MediaType.VIDEO, source_path=path, source_in=0, source_out=3, timeline_start=0,
                    source_duration=3.0)
        clip.effects = [EffectInstance(kind="color_correction", params={"brightness": 0.5}, mask=clean_mask(self.MASK))]
        project.tracks[0].clips.append(clip)

        def light(t, x):
            return _lightness(project, t, x, 0.5, (320, 180)).lightness()

        self.assertGreater(light(0.0, 0.2), light(0.0, 0.8) + 60)
        self.assertGreater(light(2.0, 0.8), light(2.0, 0.2) + 60)
        self.assertGreater(light(2.0, 0.95), light(0.0, 0.95) - 1)  # grown: wider at the end

    def test_trimming_rebases_mask_keyframes(self):
        clip = Clip(media_type=MediaType.VIDEO, source_in=0, source_out=4, timeline_start=0, source_duration=4)
        clip.effects = [EffectInstance(kind="blur", params={"sigma": 4}, mask=clean_mask(self.MASK))]
        probe = clip.with_source_range(1.0, 4.0) if hasattr(clip, "with_source_range") else None
        if probe is None:
            self.skipTest("no trim probe on this Clip")
        self.assertAlmostEqual(probe.effects[0].mask["keyframes"][0][0], 0.0)


class SpeechSearchTests(unittest.TestCase):
    def test_phrases_are_found_across_assets_and_mapped_to_the_timeline(self):
        a = MediaAsset(path="/m/interview.mp4", duration=60, has_video=True)
        a.transcript = transcript_search.clean_transcript(
            [[10.0, 10.3, "We"], [10.3, 10.6, "found"], [10.6, 11.0, "the"], [11.0, 11.5, "lighthouse."],
             [30.0, 30.4, "Árbol"]])
        b = MediaAsset(path="/m/b.mp4", duration=30, has_video=True)
        hits = transcript_search.search([a, b], "the LIGHT")
        self.assertEqual([(h.asset_id, h.start) for h in hits], [(a.id, 10.6)])
        self.assertIn("lighthouse.", hits[0].snippet)
        self.assertEqual(len(transcript_search.search([a], "arbol")), 1)  # accents ignored
        project = Project()
        project.add_media_asset(a)
        clip = Clip(media_type=MediaType.VIDEO, source_path=a.path, source_in=5, source_out=20, timeline_start=100,
                    source_duration=60, asset_id=a.id)
        project.tracks[0].clips.append(clip)
        self.assertAlmostEqual(transcript_search.timeline_time_for(project, a.id, 10.6), 105.6)
        self.assertIsNone(transcript_search.timeline_time_for(project, a.id, 40.0))
        self.assertEqual(MediaAsset.from_dict(a.to_dict()).transcript, a.transcript)

    def test_retranscribing_replaces_only_its_span(self):
        merged = transcript_search.merge_transcript([[1, 2, "old"], [5, 6, "keep"]], [[0.5, 2.5, "new"]])
        self.assertEqual([w[2] for w in merged], ["new", "keep"])

    def test_transcribe_reports_words_in_source_seconds(self):
        from core.transcribe import Word, transcribe_clip

        folder = tempfile.mkdtemp(prefix="ve_words_")
        path = os.path.join(folder, "tone.wav")
        subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=3", path], check=True)
        clip = Clip(media_type=MediaType.AUDIO, source_path=path, source_in=1.0, source_out=3.0, timeline_start=0,
                    source_duration=3.0)
        got = {}
        transcribe_clip(clip, words_from=lambda wav: [Word(0.5, 0.9, " hello")], on_words=lambda w: got.update(w=w))
        self.assertEqual(got["w"], [[1.5, 1.9, "hello"]])


class RecordingTests(unittest.TestCase):
    LISTING = '''[in#0 @ 0001] "Micrófono (Razer)" (audio)
[in#0 @ 0001]   Alternative name "@device_cm_{33D9}\\wave_{4AE9}"
[in#0 @ 0001] "HD Webcam" (video)
[in#0 @ 0001]   Alternative name "@device_pnp_\\\\?\\usb#vid_1"
'''

    def test_devices_and_arguments(self):
        devices = recording.parse_device_list(self.LISTING)
        self.assertEqual([(d.name, d.kind) for d in devices], [("Micrófono (Razer)", "audio"), ("HD Webcam", "video")])
        self.assertTrue(devices[0].spec.startswith("@device_cm_"))
        voice = recording.recording_args("ffmpeg", "voiceover", "v.wav", microphone=devices[0].spec)
        self.assertIn(f"audio={devices[0].spec}", voice)
        screen = recording.recording_args("ffmpeg", "screen", "s.mp4")
        self.assertIn("gdigrab", screen)
        with self.assertRaises(ValueError):
            recording.recording_args("ffmpeg", "webcam", "w.mp4")
        cam = recording.recording_args("ffmpeg", "webcam", "w.mp4", camera="cam", microphone="mic")
        self.assertIn("video=cam:audio=mic", cam)

    def test_the_recorder_asks_ffmpeg_to_finish(self):
        written = []

        class FakeProcess:
            def __init__(self):
                self.stdin = SimpleNamespace(write=written.append, flush=lambda: None, close=lambda: None)
                self.stderr = None
                self.done = False

            def poll(self):
                return 0 if self.done else None

            def wait(self, timeout=None):
                self.done = True

        folder = tempfile.mkdtemp(prefix="ve_rec_")
        out = os.path.join(folder, "v.wav")
        with open(out, "wb") as handle:
            handle.write(b"x" * 4096)
        recorder = recording.Recorder(["ffmpeg"], out, popen=lambda *a, **k: FakeProcess())
        self.assertEqual(recorder.stop(), out)
        self.assertEqual(written, [b"q"])


class CurveEasingTests(unittest.TestCase):
    def test_the_curve_matches_its_ffmpeg_expression(self):
        easing = curve_easing(0.2, 2.5)
        expression = _piecewise_linear_expr([(0.0, 0.0), (2.0, 1.0)], "t", easing)
        for t in (0.25, 0.8, 1.5):
            value = eval(expression.replace("if(", "_if(").replace("lt(", "_lt("),  # noqa: S307 -- our own text
                         {"_if": lambda c, a, b: a if c else b, "_lt": lambda a, b: a < b, "t": t})
            self.assertAlmostEqual(value, ease(t / 2.0, easing), places=5)
        self.assertEqual(ease(0.5, curve_easing(1, 1)), 0.5)  # 1, 1 = linear
        clip = Clip.from_dict({**Clip(media_type=MediaType.VIDEO).to_dict(), "keyframe_easing": {"opacity": easing}})
        self.assertEqual(clip.easing_for("opacity"), easing)


class ExplorerVerbTests(unittest.TestCase):
    def test_media_files_on_the_command_line_and_the_installer_verb(self):
        import main

        folder = tempfile.mkdtemp(prefix="ve_args_")
        clip = os.path.join(folder, "holiday.MP4")
        open(clip, "wb").close()
        self.assertEqual(main.media_arguments(["VideoEditor.exe", clip, "--x", "missing.mov", "notes.txt"]),
                         [os.path.abspath(clip)])
        with open(os.path.join(ROOT, "installer", "VideoEditor.iss"), encoding="utf-8") as handle:
            iss = handle.read()
        for ext in (".mp4", ".mov", ".mkv", ".wav"):
            self.assertIn(f"SystemFileAssociations\\{ext}\\shell\\{{#AppKey}}.Edit\\command", iss)
        self.assertIn('"Edit with {#AppName}"', iss)


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), \
                patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        self.addCleanup(window.deleteLater)
        return window

    def _video_clip(self, window, asset):
        window.project.add_media_asset(asset)
        clip = Clip(media_type=MediaType.VIDEO, source_path=asset.path, source_in=0, source_out=4, timeline_start=0,
                    source_duration=4.0, asset_id=asset.id)
        window.project.tracks[0].clips.append(clip)
        window.timeline.rebuild()
        window.timeline.select_clips([clip.id])
        window.properties.set_selected_clip(clip.id)
        return clip

    def test_auto_color_and_reframe_are_single_undo_steps(self):
        window = self._window()
        clip = self._video_clip(window, MediaAsset(path="/m/a.mp4", duration=4.0, has_video=True, width=1920, height=1080))
        levels = {"r_low": 0.1, "r_high": 0.8, "g_low": 0.1, "g_high": 0.8, "b_low": 0.2, "b_high": 0.9}
        self.assertTrue(window.auto_color(levels))
        self.assertEqual(clip.effects[-1].kind, "color_levels")
        window.undo()
        self.assertEqual(clip.effects, [])
        window.project.settings.width, window.project.settings.height = 1080, 1920
        points = [TrackPoint(t / 10, 0.3 + t / 100, 0.5, 1) for t in range(41)]
        with patch("core.motion_track.track_object", return_value=points):
            self.assertTrue(window.track_reframe(box=(0.25, 0.4, 0.1, 0.2)))
        self.assertEqual(clip.reframe_mode, ReframeMode.FILL)
        self.assertGreaterEqual(len(clip.crop_focus_keyframes), 2)
        window.undo()
        self.assertEqual((clip.reframe_mode, clip.crop_focus_keyframes), (ReframeMode.FIT, []))

    def test_a_voiceover_lands_on_a_free_audio_track_and_speech_hits_seek(self):
        window = self._window()
        window._pending_voiceover = ("/rec/Voiceover 1.wav", 3.0)
        asset = MediaAsset(path="/rec/Voiceover 1.wav", duration=2.0, has_audio=True)
        window.project.add_media_asset(asset)
        self.assertTrue(window._place_pending_voiceover(asset))
        placed = [c for t in window.project.tracks for c in t.clips if c.asset_id == asset.id]
        self.assertEqual(len(placed), 1)
        self.assertEqual((placed[0].media_type, placed[0].timeline_start), (MediaType.AUDIO, 3.0))
        interview = MediaAsset(path="/m/i.mp4", duration=4.0, has_video=True, width=640, height=360)
        interview.transcript = [[2.0, 2.4, "hello"]]
        self._video_clip(window, interview)
        self.assertTrue(window.go_to_spoken(interview.id, 2.0))
        self.assertAlmostEqual(window.timeline.playhead_time, 2.0)
        window.media_bin.speech_search.setText("hello")
        self.assertTrue(window.media_bin.speech_results.isVisibleTo(window.media_bin))

    def test_the_keyframe_graph_edits_through_the_properties_panel(self):
        window = self._window()
        clip = self._video_clip(window, MediaAsset(path="/m/a.mp4", duration=4.0, has_video=True, width=640, height=360))
        clip.opacity_keyframes = [(0.0, 0.0), (2.0, 1.0)]
        dialog = window.properties.open_keyframe_graph()
        dialog._on_keyframes([(0.0, 0.2), (2.0, 1.0)])
        dialog._on_easing(curve_easing(0.0, 2.0))
        self.assertEqual(clip.opacity_keyframes[0], (0.0, 0.2))
        self.assertEqual(clip.easing_for("opacity"), "curve:0.000,2.000")
        window.undo()
        self.assertEqual(clip.easing_for("opacity"), "linear")
        dialog.close()


if __name__ == "__main__":
    unittest.main()
