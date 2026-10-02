"""Offscreen Qt tests for the P1 UI fixes (F1, F2, F3, F5, F8, F9, R1).

Programmatic widget calls and signal emissions only -- no real input is
simulated and no window is shown. Skipped (not failed) without PySide6, so
the dependency-free CI job is unaffected.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QApplication

    from core.clip import Clip, MediaType
    from core.history import History
    from core.media_asset import MediaAsset
    from core.project import Project
    from core.track import TrackType
    from ui.export_dialog import ExportDialog, apply_container_extension
    from ui.properties_panel import PropertiesPanel
    from ui.widgets.timeline_items import RULER_HEIGHT, TRACK_GAP, TRACK_HEIGHT, ClipItem, TimelineSignals, row_at_scene_y
    from ui.widgets.timeline_widget import TimelineWidget, zoom_scroll_value

    _PYSIDE6_AVAILABLE = True
except ImportError:
    _PYSIDE6_AVAILABLE = False


def _video(start, length=4.0, **kw):
    return Clip(media_type=MediaType.VIDEO, source_path="/m/a.mp4", source_in=0.0, source_out=length,
                timeline_start=start, **kw)


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class _QtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


class PropertiesPanelUndoTests(_QtCase):
    def setUp(self):
        self.project = Project()
        self.a, self.b = _video(0.0), _video(4.0)
        self.project.tracks[0].clips += [self.a, self.b]
        self.history = History()
        self.panel = PropertiesPanel()
        self.panel.set_project(self.project, self.history)
        self.panel.set_selected_clip(self.a.id)
        self.changed = []
        self.panel.clip_changed.connect(self.changed.append)

    def test_programmatic_slider_changes_are_one_undoable_step(self):
        # setValue() is exactly what keyboard/wheel/groove input does: valueChanged
        # with no sliderPressed/sliderReleased. It used to bypass undo entirely.
        for value in (90, 70, 40):
            self.panel.opacity_slider.setValue(value)
        self.assertAlmostEqual(self.a.opacity, 0.4)
        self.assertTrue(self.history.can_undo)
        self.history.undo()
        self.assertAlmostEqual(self.a.opacity, 1.0)
        self.assertFalse(self.history.can_undo)
        self.assertIn(self.a.id, self.changed)

    def test_released_drag_ends_the_gesture(self):
        self.panel.brightness_slider.setValue(10)
        self.panel.brightness_slider.sliderReleased.emit()
        self.panel.brightness_slider.setValue(30)
        self.history.undo()
        self.assertAlmostEqual(self.a.brightness, 0.10)

    def test_speed_that_would_overlap_is_refused(self):
        messages = []
        self.panel.status_message.connect(messages.append)
        self.panel.speed_spin.setValue(0.5)
        self.panel.speed_spin.editingFinished.emit()
        self.assertEqual(self.a.speed, 1.0)
        self.assertFalse(self.history.can_undo)
        self.assertTrue(messages)
        self.assertEqual(self.panel.speed_spin.value(), 1.0)

    def test_speed_change_refreshes_the_timeline_item(self):
        timeline = TimelineWidget()
        timeline.set_project(self.project, self.history)
        self.panel.clip_changed.connect(timeline.refresh_clip)
        width_before = timeline._clip_items[self.b.id].width_px()
        self.panel.set_selected_clip(self.b.id)
        self.panel.speed_spin.setValue(2.0)
        self.panel.speed_spin.editingFinished.emit()
        self.assertAlmostEqual(timeline._clip_items[self.b.id].width_px(), width_before / 2)


class ExportDialogFormatTests(_QtCase):
    def test_extension_helper(self):
        self.assertEqual(apply_container_extension("C:/v/a.mp4", ".webm"), "C:/v/a.webm")
        self.assertEqual(apply_container_extension("C:/v/a.MOV", ".gif"), "C:/v/a.gif")
        self.assertEqual(apply_container_extension("C:/v/a.final", ".mp4"), "C:/v/a.final.mp4")

    def _dialog(self, **kw):
        project = Project()
        project.tracks[0].clips.append(_video(0.0))
        with patch("ui.export_dialog.nvenc_available", return_value=False):
            return ExportDialog(project, **kw)

    def test_format_combo_rewrites_the_path(self):
        dialog = self._dialog()
        dialog.container_combo.setCurrentText(".webm")
        self.assertTrue(dialog.path_edit.text().endswith(".webm"))
        dialog.container_combo.setCurrentText(".gif")
        self.assertTrue(dialog.path_edit.text().endswith(".gif"))
        self.assertFalse(dialog.loudness_combo.isEnabled())

    def test_typed_extension_selects_the_format(self):
        dialog = self._dialog()
        dialog.path_edit.setText("C:/v/out.mkv")
        dialog.path_edit.editingFinished.emit()
        self.assertEqual(dialog.container_combo.currentText(), ".mkv")

    def test_in_out_points_prefill_trim_range(self):
        dialog = self._dialog(in_out_range=(1.5, 3.0))
        self.assertTrue(dialog.trim_checkbox.isChecked())
        self.assertAlmostEqual(dialog.trim_start_spin.value(), 1.5)
        self.assertAlmostEqual(dialog.trim_end_spin.value(), 3.0)


class MainWindowShortcutTests(_QtCase):
    def test_no_two_actions_share_a_shortcut(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), \
                patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
            window._autosave_timer.stop()
        seen = {}
        for action in window.findChildren(QAction):
            for sequence in action.shortcuts():
                key = sequence.toString()
                if not key:
                    continue
                self.assertNotIn(key, seen, f"{key!r} is bound to both {seen.get(key)!r} and {action.text()!r}")
                seen[key] = action.text()
        self.assertIn("Ctrl+E", seen)
        window.deleteLater()


class TimelineDropAndTrackTests(_QtCase):
    def setUp(self):
        self.project = Project()
        self.history = History()
        self.timeline = TimelineWidget()
        self.timeline.set_project(self.project, self.history)

    def test_drop_row_uses_floor(self):
        pitch = TRACK_HEIGHT + TRACK_GAP
        self.assertEqual(row_at_scene_y(RULER_HEIGHT + 5), 0)
        self.assertEqual(row_at_scene_y(RULER_HEIGHT + pitch - 1), 0)  # lower half of row 0 (round() gave 1)
        self.assertEqual(row_at_scene_y(RULER_HEIGHT + pitch + 1), 1)
        self.assertEqual(row_at_scene_y(0), 0)

    def test_drop_onto_an_occupied_spot_makes_a_new_track_undoably(self):
        self.project.add_media_asset(MediaAsset(path="/m/a.mp4", duration=4.0, has_video=True, width=640))
        asset = self.project.media_pool[0]
        self.timeline.add_media_to_timeline(asset, at_time=0.0)
        tracks = len(self.project.tracks)
        self.timeline.add_media_to_timeline(asset, at_time=1.0)
        self.assertEqual(len(self.project.tracks), tracks + 1)
        for track in self.project.tracks:
            for clip in track.clips:
                self.assertFalse(track.has_overlap(clip))
        self.history.undo()
        self.assertEqual(len(self.project.tracks), tracks)

    def test_text_over_an_existing_clip_does_not_overlap(self):
        self.project.tracks[0].clips.append(_video(0.0))
        self.timeline.rebuild()
        clip_id = self.timeline.add_text_clip(at_time=1.0)
        track = self.project.find_clip_track(clip_id)
        self.assertFalse(track.has_overlap(self.project.find_clip(clip_id)))

    def test_empty_selected_track_can_be_deleted(self):
        self.timeline.add_track(TrackType.AUDIO)
        count = len(self.project.tracks)
        row = len(self.timeline._row_tracks) - 1  # the new, empty audio track
        self.timeline.signals.track_selected.emit(row)
        self.timeline.delete_selected_track()
        self.assertEqual(len(self.project.tracks), count - 1)


class TimelineTransportTests(_QtCase):
    def setUp(self):
        self.project = Project()
        self.project.tracks[0].clips += [_video(0.0), _video(6.0)]
        self.timeline = TimelineWidget()
        self.timeline.resize(900, 300)
        self.timeline.set_project(self.project, History())

    def test_frame_steps_edit_points_and_in_out(self):
        self.timeline.set_playhead(1.0)
        self.timeline.step_frames(1)
        self.assertAlmostEqual(self.timeline.playhead_time, 1.0 + 1 / 30)
        self.timeline.go_to_edit_point(1)
        self.assertAlmostEqual(self.timeline.playhead_time, 4.0)
        self.timeline.set_in_point()
        self.timeline.go_to_end()
        self.timeline.set_out_point()
        self.assertEqual(self.timeline.in_out_range(), (4.0, 10.0))
        self.timeline.clear_in_out()
        self.assertIsNone(self.timeline.in_out_range())

    def test_zoom_is_in_place_and_anchored(self):
        self.assertEqual(zoom_scroll_value(10.0, 100.0, 300.0), 130 + 1000 - 300)
        items_before = dict(self.timeline._clip_items)
        self.timeline.set_zoom(120.0, anchor_viewport_x=200.0)
        self.assertEqual(self.timeline._clip_items, items_before)  # same item objects: no rebuild
        item = self.timeline._clip_items[self.project.tracks[0].clips[1].id]
        self.assertAlmostEqual(item.width_px(), 4.0 * 120.0)


class ClipItemRampTrimTests(_QtCase):
    def test_trim_right_on_a_speed_ramp_uses_the_real_duration(self):
        clip = _video(0.0, speed_keyframes=[(0.0, 1.0), (2.0, 0.5)])  # 6 s on the timeline
        item = ClipItem(clip, row=0, pixels_per_second=40.0, signals=TimelineSignals())
        item._press_start, item._press_duration = 0.0, clip.duration
        item._press_source_in, item._press_source_out = clip.source_in, clip.source_out
        item._apply_trim_right(-2.0)  # drag the right edge 2 s left
        self.assertAlmostEqual(item._duration, 4.0)
        self.assertAlmostEqual(item._source_out, 3.0)  # constant-speed math gave 2.0


if __name__ == "__main__":
    unittest.main()
