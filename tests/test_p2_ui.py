"""Offscreen Qt tests for the P2 UI work (programmatic calls and signal emissions only).

No window is shown and no input is simulated. Skipped without PySide6.
"""

from __future__ import annotations

import os
import json
import tempfile
import unittest
from unittest.mock import patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication, QMessageBox

    from core.clip import Clip, MediaType
    from core.effects import EffectInstance
    from core.history import History
    from core.media_asset import MediaAsset
    from core.project import Project
    from core.track import TrackType
    from ui import theme
    from ui.export_dialog import ExportDialog, ExportWorker, codecs_for_container
    from ui.import_worker import ImportWorker
    from ui.preview_player import PreviewPlayer
    from ui.properties_panel import PropertiesPanel
    from ui.relink_dialog import RelinkDialog
    from ui.widgets.timeline_items import CLIP_COLORS, EFFECT_INDICATORS, ClipItem, TimelineSignals, ToggleButtonItem, clip_description
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
        # The offscreen platform's clipboard crashes the interpreter at exit (0xC0000005) if it
        # still holds a QMimeData set from Python (the copy/paste tests do). The real Windows
        # platform doesn't, so this is a test-only cleanup.
        QApplication.clipboard().clear()


class ExportDialogP2Tests(_QtCase):
    def _dialog(self, project=None):
        project = project or Project(name="p2")
        if not project.tracks[0].clips:
            project.tracks[0].clips.append(_video(0.0))
        with patch("ui.export_dialog.nvenc_available", return_value=False), \
                patch("ui.export_dialog.load_user_presets", return_value={}):
            return ExportDialog(project)

    def test_codec_choices_follow_the_container(self):
        self.assertEqual(codecs_for_container(".gif"), ["auto"])
        self.assertIn("prores", codecs_for_container(".mov"))
        self.assertNotIn("prores", codecs_for_container(".mp4"))
        dialog = self._dialog()
        dialog.container_combo.setCurrentText(".mov")
        dialog.codec_combo.setCurrentIndex(dialog.codec_combo.findData("prores"))
        self.assertFalse(dialog.two_pass_checkbox.isEnabled())  # no target size for intermediates
        self.assertFalse(dialog.quality_combo.isEnabled())

    def test_subtitle_mode_follows_the_container_and_reaches_the_settings(self):
        dialog = self._dialog()
        embed = dialog.subtitle_combo.findData("embed")
        self.assertEqual(dialog.subtitle_combo.currentData(), "burn")
        dialog.container_combo.setCurrentText(".mkv")
        dialog.subtitle_combo.setCurrentIndex(embed)
        self.assertEqual(dialog.current_settings_dict()["subtitles"], "embed")
        dialog.container_combo.setCurrentText(".avi")  # no text subtitle stream in AVI
        self.assertFalse(dialog.subtitle_combo.model().item(embed).isEnabled())
        self.assertEqual(dialog.subtitle_combo.currentData(), "burn")
        dialog.apply_settings({"subtitles": "sidecar"})
        self.assertEqual(dialog.subtitle_combo.currentData(), "sidecar")
        dialog.container_combo.setCurrentText(".webm")
        self.assertTrue(dialog.subtitle_combo.model().item(embed).isEnabled())

    def test_loudness_reading_is_shown_against_the_target(self):
        from core.exporter import LoudnessReading

        dialog = self._dialog()
        dialog.loudness_combo.setCurrentIndex(dialog.loudness_combo.findData(-14.0))
        dialog._on_loudness_measured(LoudnessReading(integrated=-17.25, true_peak=-2.0, loudness_range=5.5))
        self.assertEqual(
            dialog.loudness_reading_label.text(),
            "-17.2 LUFS integrated, true peak -2.0 dBTP, range 5.5 LU -- -3.2 LU from the -14 LUFS target",
        )
        dialog._on_loudness_measured(LoudnessReading(integrated=float("-inf"), true_peak=float("-inf"), loudness_range=0.0))
        self.assertEqual(dialog.loudness_reading_label.text(), "Silent mix")
        settings = dialog._measure_settings()
        self.assertEqual((settings.trim_end, settings.loudness_target), (None, -14.0))

    def test_builtin_preset_and_ntsc_rate(self):
        dialog = self._dialog()
        with patch("ui.export_dialog.load_user_presets", return_value={}):
            dialog.apply_settings(dialog.preset_data(("builtin", "Archive (ProRes 422 HQ)")))
        self.assertEqual(dialog.container_combo.currentText(), ".mov")
        self.assertEqual(dialog.current_codec(), "prores")
        dialog.fps_combo.setCurrentText("29.97")
        self.assertAlmostEqual(dialog.current_settings_dict()["fps"], 30000 / 1001)

    def test_defaults_come_from_the_dominant_source(self):
        project = Project()
        asset = MediaAsset(path="/m/cam.mp4", width=3840, height=2160, fps=30000 / 1001, has_video=True)
        project.add_media_asset(asset)
        project.tracks[0].clips.append(_video(0.0, path=asset.path, asset_id=asset.id))
        dialog = self._dialog(project)
        self.assertEqual((dialog.width_spin.value(), dialog.height_spin.value()), (3840, 2160))
        self.assertEqual(dialog.fps_combo.currentText(), "29.97")

    def test_dialog_is_deleted_on_close_and_renders_a_snapshot(self):
        project = Project()
        project.tracks[0].clips.append(_video(0.0))
        dialog = self._dialog(project)
        self.assertTrue(dialog.testAttribute(Qt.WA_DeleteOnClose))
        from core.exporter import ExportSettings

        worker = ExportWorker(project, ExportSettings(output_path="x.mp4"))
        self.assertIsNot(worker.exporter.project, project)
        self.assertEqual(worker.exporter.project.tracks[0].clips[0].id, project.tracks[0].clips[0].id)

    def test_escape_during_a_render_cancels_it_instead_of_hiding(self):
        dialog = self._dialog()
        events = []

        class FakeWorker:
            def cancel(self):
                events.append("cancel")

        class FakeThread:
            def isRunning(self):
                return "cancel" not in events

            def wait(self, ms):
                events.append("wait")
                return True

        dialog._worker, dialog._thread = FakeWorker(), FakeThread()
        dialog.reject()  # what Esc calls
        self.assertEqual(events, ["cancel", "wait"])


class TimelineP2Tests(_QtCase):
    def setUp(self):
        self.project = Project()
        self.a, self.b = _video(0.0), _video(4.0)
        self.project.tracks[0].clips += [self.a, self.b]
        self.history = History(validator=self.project.validate)
        self.timeline = TimelineWidget()
        self.timeline.set_project(self.project, self.history)

    def test_ctrl_click_builds_a_multi_selection_and_delete_removes_all(self):
        self.timeline.signals.clip_selected.emit(self.a.id)
        self.timeline.signals.clip_toggle_selected.emit(self.b.id)
        self.assertEqual(self.timeline.selected_clip_ids, {self.a.id, self.b.id})
        self.timeline.delete_selected()
        self.assertEqual(self.project.tracks[0].clips, [])
        self.history.undo()
        self.assertEqual(len(self.project.tracks[0].clips), 2)

    def test_copy_paste_and_duplicate(self):
        self.timeline.select_clips([self.a.id])
        self.assertTrue(self.timeline.copy_selected())
        self.timeline.set_playhead(8.0)
        self.assertTrue(self.timeline.paste_at_playhead())
        self.assertEqual(sorted(c.timeline_start for c in self.project.tracks[0].clips), [0.0, 4.0, 8.0])
        self.assertTrue(self.timeline.duplicate_selected())  # the pasted clip is selected: lands at 12
        self.assertEqual(max(c.timeline_start for c in self.project.tracks[0].clips), 12.0)

    def test_group_move_moves_every_selected_clip(self):
        self.timeline.select_clips([self.a.id, self.b.id])
        self.timeline._on_clip_moved(self.a.id, 2.0, 0)
        self.assertEqual((self.a.timeline_start, self.b.timeline_start), (2.0, 6.0))

    def test_razor_click_splits(self):
        self.timeline.set_razor_mode(True)
        self.assertTrue(all(item.razor_mode for item in self.timeline._clip_items.values()))
        self.timeline.signals.clip_razor.emit(self.a.id, 1.5)
        self.assertEqual(len(self.project.tracks[0].clips), 3)

    def test_clip_menu_has_the_editing_commands(self):
        menu = self.timeline.build_clip_menu(self.a.id)
        texts = [a.text() for a in menu.actions() if a.text()]
        for expected in ("Copy", "Paste Attributes", "Duplicate", "Detach Audio", "Slip...", "Ripple Delete"):
            self.assertIn(expected, texts)
        dict((a.text(), a) for a in menu.actions())["Duplicate"].trigger()
        # The spot after a is taken by b, so the copy lands on a new track at 4 s.
        self.assertEqual(sum(len(t.clips) for t in self.project.tracks), 3)
        self.assertEqual(self.project.validate(), [])

    def test_incremental_rebuild_reuses_items(self):
        items = dict(self.timeline._clip_items)
        from core.commands import MoveClipCommand

        self.history.execute(MoveClipCommand(self.project, self.b.id, 9.0, self.project.tracks[0].id))
        self.timeline.rebuild()
        self.assertIs(self.timeline._clip_items[self.a.id], items[self.a.id])
        self.assertAlmostEqual(self.timeline._clip_items[self.b.id].pos().x(),
                               130 + 9.0 * self.timeline.pixels_per_second)

    def test_offline_clips_are_flagged(self):
        self.assertTrue(self.timeline._clip_items[self.a.id].offline)
        self.assertIn("OFFLINE", self.timeline._clip_items[self.a.id].toolTip())

    def test_keyboard_selection_and_nudge(self):
        self.timeline.select_adjacent_clip(1)
        self.assertEqual(self.timeline.selected_clip_id, self.a.id)
        self.timeline.select_adjacent_clip(1)
        self.assertEqual(self.timeline.selected_clip_id, self.b.id)
        self.assertTrue(self.timeline.nudge_selected(3))
        self.assertAlmostEqual(self.b.timeline_start, 4.0 + 3 / 30.0)

    def test_keyframe_navigation(self):
        self.a.opacity_keyframes = [(1.0, 0.0), (2.5, 1.0)]
        self.timeline.select_clips([self.a.id])
        self.timeline.go_to_keyframe(1)
        self.assertAlmostEqual(self.timeline.playhead_time, 1.0)
        self.timeline.go_to_keyframe(1)
        self.assertAlmostEqual(self.timeline.playhead_time, 2.5)


class AccessibilityTests(_QtCase):  # F13
    def test_clip_labels_and_badges_reach_wcag_aa(self):
        for color in CLIP_COLORS.values():
            strip = theme.blend_over(color)
            self.assertGreaterEqual(theme.contrast_ratio("#ffffff", strip), theme.WCAG_AA_CONTRAST, color)
        for _attr, _letter, color in EFFECT_INDICATORS:
            self.assertGreaterEqual(theme.contrast_ratio(theme.readable_text_color(color), color), 4.5, color)

    def test_toggles_and_clips_describe_themselves(self):
        toggle = ToggleButtonItem("H", description="Hide track picture")
        self.assertEqual(toggle.toolTip(), "Hide track picture (off)")
        toggle.set_active(True)
        self.assertEqual(toggle.toolTip(), "Hide track picture (on)")
        clip = _video(12.0, length=8.0, brightness=0.2, name="Arbol.mp4")
        text = clip_description(clip, "Video 1", 30.0)
        self.assertIn("Video clip Arbol.mp4, 00:00:12:00 to 00:00:20:00, track Video 1", text)
        self.assertIn("Color Correction", text)
        item = ClipItem(clip, 0, 40.0, TimelineSignals())
        self.assertEqual(item.active_effect_labels(), ["C"])


class PropertiesP2Tests(_QtCase):
    def setUp(self):
        self.project = Project()
        self.clip = _video(10.0, length=4.0)
        self.project.tracks[0].clips.append(self.clip)
        self.history = History()
        self.panel = PropertiesPanel()
        self.panel.set_project(self.project, self.history)
        self.panel.set_selected_clip(self.clip.id)
        self.messages = []
        self.panel.status_message.connect(self.messages.append)

    def test_key_at_playhead_uses_clip_local_time(self):
        self.panel.set_playhead_time(5.0)
        self.assertFalse(self.panel.key_at_playhead("opacity"))
        self.assertTrue(self.messages)
        self.panel.set_playhead_time(11.0)
        self.panel.opacity_slider.setValue(40)
        self.history.end_merge()
        self.assertTrue(self.panel.key_at_playhead("opacity"))
        self.assertEqual(self.clip.opacity_keyframes, [(1.0, 0.4)])

    def test_speed_key_is_in_source_time(self):
        self.clip.speed = 2.0
        self.panel.set_selected_clip(self.clip.id)
        self.panel.set_playhead_time(11.0)  # 1 s into a 2x clip = 2 s of source
        self.assertTrue(self.panel.key_at_playhead("speed"))
        self.assertEqual(self.clip.speed_keyframes[0][0], 2.0)

    def test_volume_key(self):
        self.panel.set_playhead_time(12.0)
        self.assertTrue(self.panel.key_at_playhead("volume"))
        self.assertEqual(self.clip.volume_keyframes[0][0], 2.0)

    def test_effect_stack_add_edit_reorder_remove_undo(self):
        self.panel.effect_kind_combo.setCurrentIndex(self.panel.effect_kind_combo.findData("blur"))
        self.panel._add_effect()
        self.panel.effect_kind_combo.setCurrentIndex(self.panel.effect_kind_combo.findData("sharpen"))
        self.panel._add_effect()
        self.assertEqual([e.kind for e in self.clip.effects], ["blur", "sharpen"])
        self.assertTrue(self.panel.set_effect_param(0, "sigma", 9.0))
        self.assertEqual(self.clip.effects[0].params["sigma"], 9.0)
        self.panel.effects_list.setCurrentRow(1)
        self.panel._move_effect(-1)
        self.assertEqual([e.kind for e in self.clip.effects], ["sharpen", "blur"])
        self.panel._remove_effect()
        self.assertEqual(len(self.clip.effects), 1)
        for _ in range(5):  # add, add, edit, reorder, remove
            self.history.undo()
        self.assertEqual(self.clip.effects, [])
        self.assertGreater(self.panel.effect_params_form.rowCount(), -1)


def _png_bytes(color="#ff0000") -> bytes:
    image = QImage(8, 8, QImage.Format_RGB32)
    image.fill(QColor(color))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


class PreviewP2Tests(_QtCase):
    def setUp(self):
        self.project = Project()
        self.a, self.b = _video(0.0, path="/m/a.mp4"), _video(4.0, path="/m/b.mp4")
        self.project.tracks[0].clips += [self.a, self.b]
        self.preview = PreviewPlayer()
        self.preview.set_project(self.project)

    def test_next_clip_is_preloaded_and_players_swap_at_the_cut(self):
        self.preview.seek_to(3.0)
        first_player = self.preview.player
        self.assertEqual(os.path.normcase(self.preview.maybe_preload()), os.path.normcase("/m/b.mp4"))
        spare = self.preview._preload_player
        self.preview.seek_to(4.2)
        self.assertIs(self.preview.player, spare)
        self.assertIs(self.preview._preload_player, first_player)
        self.assertIsNone(self.preview._preloaded_source)

    def test_no_preload_far_from_the_cut(self):
        self.preview.seek_to(0.5)
        self.assertIsNone(self.preview.maybe_preload())

    def test_proxy_is_played_when_present(self):
        proxy = os.path.join(tempfile.mkdtemp(prefix="ve_proxy_"), "p.mp4")
        open(proxy, "wb").close()
        asset = MediaAsset(path="/m/a.mp4", has_video=True, proxy_path=proxy)
        self.project.add_media_asset(asset)
        self.a.asset_id = asset.id
        self.assertEqual(self.preview.media_path_for(self.a), proxy)
        self.assertEqual(self.preview.media_path_for(self.b), "/m/b.mp4")

    def test_accurate_frames_drop_stale_results(self):
        self.preview.accurate_checkbox.setChecked(True)
        self.preview._frame_timer.stop()
        generation = self.preview._frame_generation
        self.preview.request_accurate_frame()
        self.preview._frame_timer.stop()
        self.preview._on_frame_ready(generation, _png_bytes())  # superseded: ignored
        self.assertNotEqual(self.preview.stack.currentWidget(), self.preview.frame_label)
        self.preview._on_frame_ready(self.preview._frame_generation, _png_bytes())
        self.assertIs(self.preview.stack.currentWidget(), self.preview.frame_label)
        self.assertFalse(self.preview.frame_label.pixmap().isNull())
        self.preview._on_frame_ready(self.preview._frame_generation, "FFmpeg broke")
        self.assertEqual(self.preview.last_frame_error, "FFmpeg broke")

    def test_accurate_frame_is_not_requested_while_playing_or_off(self):
        generation = self.preview._frame_generation
        self.preview.request_accurate_frame()
        self.assertEqual(self.preview._frame_generation, generation)


class ImportWorkerTests(_QtCase):  # PG4
    def test_two_stages_and_per_file_errors(self):
        tmp = tempfile.mkdtemp(prefix="ve_import_")
        good = [os.path.join(tmp, f"{i}.mp4") for i in range(3)]
        for path in good:
            open(path, "wb").close()
        worker = ImportWorker(good + [os.path.join(tmp, "missing.mp4")], tmp, tmp, max_workers=2)
        ready, updated, failed = [], [], []
        worker.asset_ready.connect(ready.append)
        worker.asset_updated.connect(lambda *a: updated.append(a))
        worker.failed.connect(lambda p, m: failed.append(p))
        with patch("ui.import_worker.probe_file", side_effect=lambda p: MediaAsset(path=p, duration=2.0, has_video=True, has_audio=True)), \
                patch("ui.import_worker.generate_thumbnail", return_value="t.jpg"), \
                patch("ui.import_worker.generate_waveform", return_value="w.json"):
            worker.run()
        QApplication.processEvents()
        self.assertEqual(len(ready), 3)
        self.assertEqual(len(updated), 3)
        self.assertEqual(len(failed), 1)

    def test_cancel_stops_before_thumbnails(self):
        worker = ImportWorker(["a", "b"], ".", ".", max_workers=1)
        updated = []
        worker.asset_updated.connect(lambda *a: updated.append(a))

        def probe(path):
            worker.cancel()
            return MediaAsset(path=path, has_video=True)

        with patch("ui.import_worker.os.path.isfile", return_value=True), patch("ui.import_worker.probe_file", side_effect=probe):
            worker.run()
        self.assertTrue(worker.cancelled)
        self.assertEqual(updated, [])


class RelinkDialogTests(_QtCase):  # B9
    def test_locating_one_file_relinks_its_siblings(self):
        tmp = tempfile.mkdtemp(prefix="ve_relink_ui_")
        for name in ("a.mp4", "b.mp4"):
            open(os.path.join(tmp, name), "wb").close()
        project = Project()
        a = MediaAsset(path="/gone/a.mp4", has_video=True)
        b = MediaAsset(path="/gone/b.mp4", has_video=True)
        project.media_pool += [a, b]
        project.tracks[0].clips.append(_video(0.0, path=a.path, asset_id=a.id))
        history = History()
        dialog = RelinkDialog(project, history)
        self.assertEqual(dialog.table.rowCount(), 2)
        with patch("core.relink.verify_candidate", return_value=True):
            count = dialog.relink_with_siblings(a.id, os.path.join(tmp, "a.mp4"))
        self.assertEqual(count, 2)
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertEqual(project.tracks[0].clips[0].source_path, os.path.join(tmp, "a.mp4"))
        history.undo()
        self.assertEqual(project.tracks[0].clips[0].source_path, "/gone/a.mp4")


class MainWindowP2Tests(_QtCase):
    def _window(self):
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), \
                patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        return window

    def test_dirty_flag_follows_the_undo_position(self):  # R4
        window = self._window()
        clip = _video(0.0)
        window.project.tracks[0].clips.append(clip)
        from core.commands import UpdateClipPropertyCommand

        self.assertFalse(window._dirty)
        window.history.execute(UpdateClipPropertyCommand(window.project, clip.id, "volume", 0.5))
        self.assertTrue(window._dirty)
        self.assertTrue(window.windowTitle().startswith("*"))
        window.history.undo()
        self.assertFalse(window._dirty)
        self.assertFalse(window.windowTitle().startswith("*"))
        window.deleteLater()

    def test_open_project_file_from_the_command_line(self):  # K1: .vproj file association
        from core.project import Project

        tmp = tempfile.mkdtemp(prefix="ve_open_")
        path = os.path.join(tmp, "Assoc.vproj")
        saved = Project()
        saved.name = "Assoc"
        saved.save(path)
        window = self._window()
        window.open_project_file(path)
        self.assertEqual(window.project.name, "Assoc")
        with patch("ui.main_window.QMessageBox.warning") as warning:
            window.open_project_file(os.path.join(tmp, "missing.vproj"))
        warning.assert_called_once()
        self.assertEqual(window.project.name, "Assoc")
        window.deleteLater()

    def test_oversized_project_shows_a_clear_open_error(self):
        with tempfile.TemporaryDirectory(prefix="ve_large_project_") as tmp:
            path = os.path.join(tmp, "too-large.vproj")
            with open(path, "wb") as f:
                f.write(b"x" * (1024 * 1024 + 1))
            window = self._window()
            original_project = window.project
            try:
                with (
                    patch("core.project.MAX_PROJECT_FILE_SIZE_BYTES", 1024 * 1024),
                    patch("ui.main_window.QMessageBox.critical") as critical,
                ):
                    window._open_project_from_path(path)
                self.assertEqual(window.project, original_project)
                self.assertIn("1 MiB size limit", critical.call_args.args[2])
            finally:
                window._autosave_session.close()
                window.deleteLater()

    def _recovery_window(self, home, autosave_dir):
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), \
                patch("ui.main_window._autosave_dir", return_value=autosave_dir), \
                patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
        window._autosave_timer.stop()
        return window

    def test_autosave_recovery_falls_back_and_saves_before_discarding_source(self):
        from core.autosave import AutosaveSession

        with tempfile.TemporaryDirectory(prefix="ve_recovery_") as home:
            autosave_dir = os.path.join(home, "autosave")
            os.makedirs(autosave_dir)
            orphan = AutosaveSession(autosave_dir, pid=999_999_999)
            orphan.write(Project(name="Fallback").to_dict(), {"name": "Fallback"})
            orphan.write(Project(name="Newer").to_dict(), {"name": "Newer"})
            with open(orphan.path, "w", encoding="utf-8") as f:
                f.write("{")
            os.utime(orphan.path, (2_000_000_000, 2_000_000_000))
            window = self._recovery_window(home, autosave_dir)
            try:
                with patch("ui.main_window._autosave_dir", return_value=autosave_dir), \
                        patch("ui.main_window.QMessageBox.question", return_value=QMessageBox.Yes):
                    window._offer_autosave_recovery()
                self.assertEqual(window.project.name, "Fallback")
                self.assertFalse(os.path.exists(orphan.path))
                saved = window._autosave_session.generations()
                self.assertTrue(saved)
                with open(saved[0], encoding="utf-8") as f:
                    self.assertEqual(json.load(f)["name"], "Fallback")
            finally:
                window._autosave_session.close()
                window.deleteLater()

    def test_recovery_source_is_retained_when_post_recovery_save_fails(self):
        from core.autosave import AutosaveSession

        with tempfile.TemporaryDirectory(prefix="ve_recovery_") as home:
            autosave_dir = os.path.join(home, "autosave")
            os.makedirs(autosave_dir)
            orphan = AutosaveSession(autosave_dir, pid=999_999_999)
            orphan.write(Project(name="Keep source").to_dict(), {"name": "Keep source"})
            window = self._recovery_window(home, autosave_dir)
            try:
                with patch("ui.main_window._autosave_dir", return_value=autosave_dir), \
                        patch("ui.main_window.QMessageBox.question", return_value=QMessageBox.Yes), \
                        patch.object(window._autosave_session, "write", side_effect=OSError("disk full")):
                    window._offer_autosave_recovery()
                self.assertEqual(window.project.name, "Keep source")
                self.assertTrue(os.path.exists(orphan.path))
            finally:
                window._autosave_session.close()
                window.deleteLater()

    def test_recent_projects_write_failure_preserves_previous_file(self):
        from ui import window_projects

        with tempfile.TemporaryDirectory(prefix="ve_recent_") as home, \
                patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}):
            path = window_projects._recent_projects_path()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(["previous.vproj"], f)
            with patch("core.project.os.replace", side_effect=OSError("replace failed")):
                window_projects._save_recent_projects(["new.vproj"])
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), ["previous.vproj"])
            self.assertEqual(os.listdir(os.path.dirname(path)), ["recent_projects.json"])

    def test_about_and_shortcut_list(self):  # K6, F14
        from core import __version__
        from ui.help_dialogs import shortcut_rows
        from PySide6.QtGui import QAction

        window = self._window()
        with patch("ui.main_window.binary_version", return_value="ffmpeg version 9.0.1"):
            self.assertIn(__version__, window.about_text())
        names = [name for name, _key in shortcut_rows(window.findChildren(QAction))]
        for expected in ("Copy", "Razor Tool", "Next Keyframe", "Export Video..."):
            self.assertIn(expected, names)
        window.deleteLater()


if __name__ == "__main__":
    unittest.main()
