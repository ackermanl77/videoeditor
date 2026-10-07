"""Offscreen Qt tests for the fourth round (programmatic calls and signals only).

Reverse / freeze-frame controls in the Properties panel, their timeline
badges and trims, subtitle import in the main window, and the preview's
audio level meters and stepped playback of reversed/frozen clips. No
window is shown and no input is simulated; drags go through
ClipItem.begin_drag/update_drag/finish_drag like tests/test_p3_ui.py.
Skipped without PySide6.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from core.clip import Clip, MediaType
    from core.history import History
    from core.media_asset import MediaAsset
    from core.project import Project
    from core.track import TrackType
    from ui.preview_player import POLL_INTERVAL_MS, PreviewPlayer
    from ui.properties_panel import PropertiesPanel
    from ui.widgets.timeline_widget import TimelineWidget

    _PYSIDE6_AVAILABLE = True
except ImportError:
    _PYSIDE6_AVAILABLE = False


def _video(start, length=4.0, path="/m/a.mp4", **kw):
    return Clip(media_type=MediaType.VIDEO, source_path=path, source_in=0.0, source_out=length,
                timeline_start=start, source_duration=20.0, **kw)


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class _QtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        QApplication.clipboard().clear()  # see tests/test_p2_ui.py: offscreen clipboard crash at exit


class ReverseFreezePanelTests(_QtCase):
    def setUp(self):
        self.project = Project()
        self.clip, self.next = _video(0.0), _video(5.0)
        self.project.tracks[0].clips += [self.clip, self.next]
        self.history = History(validator=self.project.validate)
        self.panel = PropertiesPanel()
        self.addCleanup(self.panel.deleteLater)
        self.panel.set_project(self.project, self.history)
        self.messages = []
        self.panel.status_message.connect(self.messages.append)
        self.panel.set_selected_clip(self.clip.id)

    def test_reverse_checkbox_is_one_undo_step(self):
        self.panel.reverse_checkbox.setChecked(True)
        self.assertTrue(self.clip.reverse)
        self.history.undo()
        self.assertFalse(self.clip.reverse)

    def test_freeze_frame_commits_and_is_refused_into_the_next_clip(self):
        self.panel.freeze_spin.setValue(0.5)
        self.panel._commit_freeze()
        self.assertAlmostEqual(self.clip.duration, 4.5)
        self.panel.freeze_spin.setValue(3.0)  # would end at 7 s, over the clip at 5 s
        self.panel._commit_freeze()
        self.assertAlmostEqual(self.clip.freeze_frame_duration, 0.5)
        self.assertAlmostEqual(self.panel.freeze_spin.value(), 0.5)  # shows the real value again
        self.assertTrue(any("overlap" in m for m in self.messages))

    def test_controls_follow_the_media_type(self):
        self.assertTrue(self.panel.reverse_checkbox.isEnabled() and self.panel.freeze_spin.isEnabled())
        image = Clip(media_type=MediaType.IMAGE, source_path="/m/still.png", source_out=3.0, timeline_start=10.0)
        audio = Clip(media_type=MediaType.AUDIO, source_path="/m/a.wav", source_out=3.0)
        self.project.tracks[0].clips.append(image)
        next(t for t in self.project.tracks if t.track_type == TrackType.AUDIO).clips.append(audio)
        self.panel.set_selected_clip(image.id)
        self.assertFalse(self.panel.reverse_checkbox.isEnabled() or self.panel.freeze_spin.isEnabled())
        self.panel.set_selected_clip(audio.id)
        self.assertTrue(self.panel.reverse_checkbox.isEnabled())
        self.assertFalse(self.panel.freeze_spin.isEnabled())

    def test_speed_key_on_a_reversed_clip_uses_the_frame_on_screen(self):
        self.clip.reverse = True
        self.panel.set_selected_clip(self.clip.id)
        self.panel.set_playhead_time(1.0)  # shows source 3.0 of 0..4
        self.assertTrue(self.panel.key_at_playhead("speed"))
        self.assertAlmostEqual(self.clip.speed_keyframes[0][0], 3.0)


class ReverseFreezeTimelineTests(_QtCase):
    def setUp(self):
        self.project = Project()
        self.rev = _video(0.0, reverse=True)
        self.rev.source_in, self.rev.source_out = 2.0, 6.0
        self.frozen = _video(10.0, freeze_frame_duration=2.0)
        self.project.tracks[0].clips += [self.rev, self.frozen]
        self.history = History(validator=self.project.validate)
        self.timeline = TimelineWidget()
        self.timeline.snap_enabled = False
        self.timeline.set_project(self.project, self.history)
        self.addCleanup(self.timeline.deleteLater)
        self.pps = self.timeline.pixels_per_second

    def _drag(self, clip, mode, seconds):
        item = self.timeline._clip_items[clip.id]
        x0 = item.scenePos().x()
        item.begin_drag(mode, x0)
        item.update_drag(x0 + seconds * self.pps)
        item.finish_drag()

    def test_badges(self):
        self.assertIn("R", self.timeline._clip_items[self.rev.id].active_effect_labels())
        self.assertIn("FF", self.timeline._clip_items[self.frozen.id].active_effect_labels())
        self.assertIn("Freeze Frame", self.timeline._clip_items[self.frozen.id].toolTip())

    def test_left_trim_of_a_reversed_clip_cuts_its_source_out_end(self):
        self._drag(self.rev, "trim_left", 1.0)
        self.assertEqual((round(self.rev.source_in, 3), round(self.rev.source_out, 3)), (2.0, 5.0))
        self.assertAlmostEqual(self.rev.timeline_start, 1.0)
        self.history.undo()
        self.assertEqual((self.rev.source_in, self.rev.source_out), (2.0, 6.0))

    def test_right_trim_shrinks_the_freeze_before_the_footage(self):
        self._drag(self.frozen, "trim_right", -1.5)
        self.assertAlmostEqual(self.frozen.freeze_frame_duration, 0.5)
        self.assertAlmostEqual(self.frozen.source_out, 4.0)
        self._drag(self.frozen, "trim_right", -1.5)
        self.assertAlmostEqual(self.frozen.freeze_frame_duration, 0.0)
        self.assertAlmostEqual(self.frozen.source_out, 3.0)
        self.history.undo()
        self.history.undo()
        self.assertAlmostEqual(self.frozen.freeze_frame_duration, 2.0)

    def test_split_inside_a_freeze_is_refused_with_a_message(self):
        messages = []
        self.timeline.status_message.connect(messages.append)
        self.timeline.signals.clip_razor.emit(self.frozen.id, 15.0)
        self.assertEqual(len(self.project.tracks[0].clips), 2)
        self.assertTrue(any("freeze frame" in m for m in messages))
        self.timeline.signals.clip_razor.emit(self.frozen.id, 12.0)
        self.assertEqual(len(self.project.tracks[0].clips), 3)


class SubtitleImportTests(_QtCase):
    def _window(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), \
                patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        self.addCleanup(window.deleteLater)
        return window

    def _srt(self, text, name="subs.srt"):
        path = os.path.join(tempfile.mkdtemp(prefix="ve_srt_"), name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_import_adds_a_subtitle_track_in_one_undo_step(self):
        window = self._window()
        tracks_before = len(window.project.tracks)
        path = self._srt("1\n00:00:01,000 --> 00:00:02,000\nHi\n\n2\n00:00:03,000 --> 00:00:04,000\nBye\n")
        self.assertEqual(window.import_subtitle_file(path), 2)
        track = next(t for t in window.project.tracks if t.name == "Subtitles")
        self.assertEqual([c.text_style.content for c in track.sorted_clips()], ["Hi", "Bye"])
        self.assertTrue(window.import_subtitles_action.text().startswith("Import Subtitles"))
        # Importing the same cues again can't overlap them: a second track.
        self.assertEqual(window.import_subtitle_file(path), 2)
        self.assertIn("Subtitles 2", [t.name for t in window.project.tracks])
        window.history.undo()
        window.history.undo()
        self.assertEqual(len(window.project.tracks), tracks_before)

    def test_unreadable_file_warns_and_subtitles_route_from_media_import(self):
        window = self._window()
        bad = self._srt("not subtitles at all", "bad.srt")
        with patch("ui.main_window.QMessageBox.warning") as warning:
            self.assertEqual(window.import_subtitle_file(bad), 0)
        warning.assert_called_once()
        good = self._srt("WEBVTT\n\n00:01.000 --> 00:02.000\nRouted\n", "good.vtt")
        with patch.object(window, "import_subtitle_file", wraps=window.import_subtitle_file) as spy:
            window.import_media_paths([good])
        spy.assert_called_once_with(good)
        self.assertIsNone(window._import_thread)  # no media import started for a subtitle file


class PreviewMeterTests(_QtCase):
    def setUp(self):
        self.project = Project()
        tmp = tempfile.mkdtemp(prefix="ve_meter_")
        waveform = os.path.join(tmp, "a.waveform.json")
        # Silent for 2 s, loud for 2 s (100 values/s).
        with open(waveform, "w", encoding="utf-8") as f:
            json.dump({"peaks_per_second": 100, "peaks": [0.0] * 200 + [0.9] * 200,
                       "rms": [0.0] * 200 + [0.5] * 200}, f)
        self.asset = MediaAsset(path="/m/a.mp4", duration=4.0, has_video=True, has_audio=True, waveform_path=waveform)
        self.project.add_media_asset(self.asset)
        self.clip = _video(0.0, asset_id=self.asset.id)
        self.project.tracks[0].clips.append(self.clip)
        self.preview = PreviewPlayer()
        self.addCleanup(self.preview.deleteLater)
        self.preview.set_project(self.project)

    def test_meters_read_the_cached_levels_at_the_playhead(self):
        self.preview.seek_to(1.0)
        self.assertEqual(self.preview.meters.levels(), (0.0, 0.0))
        self.preview.seek_to(3.0)
        peak, rms = self.preview.meters.levels()
        self.assertAlmostEqual(peak, 0.9)
        self.assertAlmostEqual(rms, 0.5)

    def test_meters_follow_reverse_mute_and_freeze(self):
        self.clip.reverse = True  # timeline 1.0 now plays source 3.0 (loud)
        self.preview.seek_to(1.0)
        self.assertGreater(self.preview.meters.levels()[0], 0.5)
        self.clip.muted = True
        self.preview.refresh_after_edit()
        self.assertEqual(self.preview.meters.levels(), (0.0, 0.0))
        self.clip.muted, self.clip.reverse, self.clip.freeze_frame_duration = False, False, 2.0
        self.preview.seek_to(5.0)  # in the freeze: silent
        self.assertEqual(self.preview.meters.levels(), (0.0, 0.0))

    def test_no_audio_data_means_quiet_meters(self):
        self.asset.waveform_path = None
        self.preview.set_project(self.project)
        self.preview.seek_to(3.0)
        self.assertEqual(self.preview.meters.levels(), (0.0, 0.0))
        self.preview.set_project(Project())  # empty project: nothing to meter, nothing breaks
        self.assertEqual(self.preview.meters.levels(), (0.0, 0.0))

    def test_reversed_and_frozen_clips_are_stepped_not_played(self):
        self.clip.reverse = True
        self.preview.seek_to(0.5)
        with patch.object(self.preview.player, "play") as play:
            self.preview._is_playing = True
            self.preview._on_poll()
            self.preview._is_playing = False
        play.assert_not_called()
        self.assertAlmostEqual(self.preview.current_time, 0.5 + POLL_INTERVAL_MS / 1000.0)
        self.assertTrue(PreviewPlayer._is_stepped(_video(0.0, freeze_frame_duration=1.0), 4.5))
        self.assertFalse(PreviewPlayer._is_stepped(_video(0.0, freeze_frame_duration=1.0), 3.5))


if __name__ == "__main__":
    unittest.main()


class FrameExportUiTests(_QtCase):  # F15: File > Export Frame
    _window = SubtitleImportTests._window

    def test_export_frame_writes_png_and_jpeg_at_the_playhead(self):
        from PySide6.QtGui import QColor, QImage

        window = self._window()
        window.project.tracks[0].clips.append(Clip(media_type=MediaType.VIDEO, source_path="x.mp4", source_out=5.0))
        window.timeline.set_playhead(2.5)
        image = QImage(64, 36, QImage.Format_RGB32)
        image.fill(QColor("#336699"))
        from PySide6.QtCore import QBuffer

        buffer = QBuffer()
        buffer.open(QBuffer.WriteOnly)
        image.save(buffer, "PNG")
        calls = []
        folder = tempfile.mkdtemp(prefix="ve_frame_")
        with patch("ui.main_window.render_frame", lambda project, t, size, timeout: calls.append((t, size)) or bytes(buffer.data())):
            png = window.export_frame_dialog(os.path.join(folder, "still.png"))
            jpg = window.export_frame_dialog(os.path.join(folder, "still.jpg"))
            bare = window.export_frame_dialog(os.path.join(folder, "still"))
        self.assertEqual([c[0] for c in calls], [2.5, 2.5, 2.5])
        self.assertEqual(calls[0][1], (window.project.settings.width // 2 * 2, window.project.settings.height // 2 * 2))
        self.assertTrue(QImage(png).width() == 64 and QImage(jpg).width() == 64)
        self.assertTrue(bare.endswith("still.png"))
        with open(jpg, "rb") as handle:
            self.assertEqual(handle.read(2), b"\xff\xd8")  # really a JPEG


class MarkerColourTests(_QtCase):  # F15: marker colours
    def test_recolouring_a_marker_is_undoable_and_saved(self):
        from core.commands import RecolorMarkerCommand
        from core.project import MARKER_COLORS

        project = Project()
        marker = project.add_marker(2.0, "Intro")
        history = History()
        timeline = TimelineWidget()
        timeline.set_project(project, history)
        timeline.signals.marker_color_requested.emit(marker.id, MARKER_COLORS["Red"])
        self.assertEqual(marker.color, MARKER_COLORS["Red"])
        self.assertEqual(Project.from_dict(project.to_dict()).markers[0].color, MARKER_COLORS["Red"])
        self.assertEqual(timeline._marker_items[0].color, MARKER_COLORS["Red"])
        history.undo()
        self.assertEqual(marker.color, "#3b82f6")
        # Picking the colour it already has adds no undo step.
        before = len(history._undo_stack)
        timeline.signals.marker_color_requested.emit(marker.id, "#3b82f6")
        self.assertEqual(len(history._undo_stack), before)
        self.assertIsInstance(RecolorMarkerCommand(project, marker.id, "#000000").label, str)


class CanvasTransformTests(_QtCase):  # F15: on-canvas transform
    def test_dragging_the_box_moves_and_a_corner_scales_in_one_undo_step_each(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        window = SubtitleImportTests._window(self)
        asset = MediaAsset(path="/m/a.mp4", width=1920, height=1080, duration=20.0, has_video=True)
        window.project.add_media_asset(asset)
        clip = _video(0.0, 10.0, asset_id=asset.id)
        window.project.tracks[0].clips.append(clip)
        window.timeline.rebuild()
        window.timeline.set_playhead(2.0)
        window.timeline._on_clip_selected(clip.id)
        window.preview.resize(640, 480)
        window.preview.transform_checkbox.setChecked(True)
        overlay = window.preview.transform_overlay
        overlay.setGeometry(0, 0, 640, 360)  # a 16:9 view: canvas pixels = 3x widget pixels
        self.assertTrue(overlay.isVisibleTo(window.preview))
        self.assertEqual(overlay.target["clip_id"], clip.id)

        QTest.mousePress(overlay, Qt.LeftButton, Qt.NoModifier, QPoint(320, 180))
        QTest.mouseMove(overlay, QPoint(384, 144))
        QTest.mouseRelease(overlay, Qt.LeftButton, Qt.NoModifier, QPoint(384, 144))
        self.assertAlmostEqual(clip.transform_x, 0.6, places=3)
        self.assertAlmostEqual(clip.transform_y, 0.4, places=3)
        undo_steps = len(window.history._undo_stack)

        corner = overlay._to_widget(*overlay._box().corners()[2])
        QTest.mousePress(overlay, Qt.LeftButton, Qt.NoModifier, corner.toPoint())
        centre = overlay._to_widget(overlay._box().cx, overlay._box().cy)
        half = QPoint(int((corner.x() + centre.x()) / 2), int((corner.y() + centre.y()) / 2))
        QTest.mouseMove(overlay, half)
        QTest.mouseRelease(overlay, Qt.LeftButton, Qt.NoModifier, half)
        self.assertAlmostEqual(clip.transform_scale, 0.5, delta=0.02)
        self.assertEqual(len(window.history._undo_stack), undo_steps + 1)
        window.history.undo()
        self.assertEqual(clip.transform_scale, 1.0)
        self.assertAlmostEqual(clip.transform_x, 0.6, places=3)

    def test_no_target_outside_the_clip_or_for_fill_or_unknown_size(self):
        window = SubtitleImportTests._window(self)
        asset = MediaAsset(path="/m/b.mp4", width=0, height=0, duration=20.0, has_video=True)
        window.project.add_media_asset(asset)
        clip = _video(0.0, 4.0, path="/m/b.mp4", asset_id=asset.id)
        window.project.tracks[0].clips.append(clip)
        window.timeline.rebuild()
        window.timeline._on_clip_selected(clip.id)
        self.assertIsNone(window._transform_target())  # source size unknown
        asset.width, asset.height = 1280, 720
        self.assertIsNotNone(window._transform_target())
        window.timeline.set_playhead(8.0)
        self.assertIsNone(window._transform_target())  # not under the playhead


class GpuDecodingMenuTests(_QtCase):  # PG2: GPU decoding setting in the UI
    def test_the_choice_switches_decoding_and_is_remembered(self):
        from core import ffmpeg_utils

        with patch.dict(os.environ, {}, clear=False), patch.object(ffmpeg_utils, "LAUNCH_DECODE_HWACCEL", None), \
                patch("ui.main_window.LAUNCH_DECODE_HWACCEL", None):
            os.environ.pop(ffmpeg_utils.DECODE_HWACCEL_ENV, None)
            window = SubtitleImportTests._window(self)
            self.assertTrue(window.gpu_decoding_actions["off"].isChecked())
            self.assertEqual(ffmpeg_utils.decode_hwaccel_modes(), [None])
            saved = {}

            class _Settings:  # the tests' QSettings has no organisation, so it stores nothing
                def setValue(self, key, value):
                    saved[key] = value

            with patch("ui.main_window.QSettings", _Settings):
                window.gpu_decoding_actions["cuda"].trigger()
            self.assertEqual(ffmpeg_utils.decode_hwaccel_modes(), ["cuda", None])
            self.assertEqual(saved, {"decode/hwaccel": "cuda"})
            self.assertTrue(window.gpu_decoding_actions["cuda"].isChecked())
            window.set_gpu_decoding("off")
            self.assertEqual(ffmpeg_utils.decode_hwaccel_modes(), [None])

    def test_a_value_set_outside_the_app_wins_and_locks_the_menu(self):
        with patch.dict(os.environ, {"VIDEO_EDITOR_HWACCEL": "auto"}), patch("ui.main_window.LAUNCH_DECODE_HWACCEL", "auto"):
            window = SubtitleImportTests._window(self)
            self.assertTrue(window.gpu_decoding_actions["auto"].isChecked())
            self.assertFalse(window.gpu_decoding_actions["off"].isEnabled())
            self.assertEqual(os.environ["VIDEO_EDITOR_HWACCEL"], "auto")
