"""Offscreen Qt tests for the third improvement round (programmatic calls and signals only).

Export dialog colour modes and "Add to Queue" (B6/PG1, F4), the render
queue panel (F4), Ctrl/Alt/Shift drag gestures (F7), effect-parameter
keyframes in the Properties panel (F12) and the timeline's accessibility
tree (F13). No window is shown and no input is simulated; drags are driven
through ClipItem.begin_drag/update_drag/finish_drag, the same methods the
mouse handlers call. Skipped without PySide6.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QObject, Qt, Signal
    from PySide6.QtGui import QAccessible
    from PySide6.QtWidgets import QApplication, QFormLayout

    from core.clip import Clip, MediaType
    from core.effects import EffectInstance
    from core.exporter import ExportSettings
    from core.history import History
    from core.media_asset import MediaAsset
    from core.project import Project, ProjectSettings
    from core.render_queue import JobStatus
    from ui.export_dialog import ExportDialog, color_modes_for
    from ui.properties_panel import PropertiesPanel
    from ui.render_queue import RenderQueueWidget
    from ui.widgets.timeline_items import drag_mode_for
    from ui.widgets.timeline_widget import TimelineWidget

    _PYSIDE6_AVAILABLE = True
except ImportError:
    _PYSIDE6_AVAILABLE = False

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _video(start, length=4.0, path="/m/a.mp4", **kw):
    return Clip(media_type=MediaType.VIDEO, source_path=path, source_in=0.0, source_out=length,
                timeline_start=start, source_duration=20.0, **kw)


def _wait_for(predicate, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class _QtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        QApplication.clipboard().clear()  # see tests/test_p2_ui.py: offscreen clipboard crash at exit


class ExportDialogColorTests(_QtCase):  # B6 / PG1
    def _dialog(self, project=None):
        with patch("ui.export_dialog.nvenc_available", return_value=False), \
                patch("ui.export_dialog._cpu_encoder_10bit", return_value=True):
            dialog = ExportDialog(project or Project(name="p3"), queue_available=True)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_modes_follow_codec_machine_and_sources(self):
        self.assertEqual(color_modes_for("h264", ".mp4", "pq", lambda c: True), ["sdr8"])
        self.assertEqual(color_modes_for("hevc", ".mp4", None, lambda c: True), ["sdr8", "sdr10"])
        self.assertEqual(color_modes_for("hevc", ".mkv", "hlg", lambda c: True), ["sdr8", "sdr10", "hlg"])
        self.assertEqual(color_modes_for("av1", ".mkv", "pq", lambda c: False), ["sdr8"])  # encoder can't
        self.assertEqual(color_modes_for("auto", ".gif", "pq", lambda c: True), ["sdr8"])

    def test_dialog_lists_hdr_only_for_hdr_sources_and_builds_settings(self):
        asset = MediaAsset(path="/m/hdr.mov", duration=5.0, width=64, height=36, has_video=True,
                           color_transfer="smpte2084")
        project = Project(name="hdr")
        project.add_media_asset(asset)
        project.tracks[0].clips.append(_video(0.0, path=asset.path, asset_id=asset.id))
        dialog = self._dialog(project)
        dialog.container_combo.setCurrentText(".mkv")
        self.assertFalse(dialog.color_combo.isEnabled())  # container default = H.264: 8-bit only
        dialog.codec_combo.setCurrentIndex(dialog.codec_combo.findData("hevc"))
        modes = [dialog.color_combo.itemData(i) for i in range(dialog.color_combo.count())]
        self.assertEqual(modes, ["sdr8", "sdr10", "pq"])
        dialog.color_combo.setCurrentIndex(2)
        self.assertIn("mastering metadata", dialog.color_note_label.text())
        dialog.path_edit.setText(os.path.join(tempfile.mkdtemp(prefix="ve_p3_dlg_"), "out.mkv"))
        settings = dialog.build_settings()
        self.assertEqual((settings.color_mode, settings.video_codec), ("pq", "hevc"))
        self.assertEqual(dialog.current_settings_dict()["color_mode"], "pq")
        dialog.codec_combo.setCurrentIndex(dialog.codec_combo.findData("h264"))
        self.assertEqual(dialog.current_color_mode(), "sdr8")  # an unsupported mode is never left selected

    def test_sdr_project_has_no_hdr_choice(self):
        dialog = self._dialog()
        dialog.container_combo.setCurrentText(".mov")
        dialog.codec_combo.setCurrentIndex(dialog.codec_combo.findData("prores"))
        modes = [dialog.color_combo.itemData(i) for i in range(dialog.color_combo.count())]
        self.assertEqual(modes, ["sdr8", "sdr10"])

    def test_add_to_queue_emits_settings_and_closes(self):
        dialog = self._dialog()
        self.assertFalse(dialog.queue_btn.isHidden())
        dialog.path_edit.setText(os.path.join(tempfile.mkdtemp(prefix="ve_p3_dlg_"), "queued.mp4"))
        received = []
        dialog.queue_requested.connect(received.append)
        dialog._on_add_to_queue()
        self.assertEqual(len(received), 1)
        self.assertTrue(received[0].output_path.endswith("queued.mp4"))
        self.assertFalse(dialog.is_running())


if _PYSIDE6_AVAILABLE:
    class _FakeWorker(QObject):
        """Stands in for ExportWorker: finishes (or fails) when told to, from the test."""

        progress = Signal(float, str)
        finished = Signal(str)
        failed = Signal(str)
        instances = []

        def __init__(self, project, settings):
            super().__init__()
            self.settings = settings
            self.exporter = type("E", (), {"warnings": []})()
            self.cancelled = False
            _FakeWorker.instances.append(self)

        def run(self):
            self.progress.emit(0.5, "Rendering... 50% (1.0x realtime, ~00:03 left)")
            if self.settings.output_path.endswith("fail.mp4"):
                self.failed.emit("FFmpeg exited with code 1 (Rendering).\nlog")
            elif not self.settings.output_path.endswith("hang.mp4"):
                self.finished.emit(self.settings.output_path)
            else:
                while not self.cancelled:
                    time.sleep(0.005)
                self.failed.emit("Export cancelled.")

        def cancel(self):
            self.cancelled = True


class RenderQueueWidgetTests(_QtCase):  # F4
    def setUp(self):
        _FakeWorker.instances = []
        self.widget = RenderQueueWidget(worker_factory=_FakeWorker)
        self.addCleanup(self.widget.deleteLater)
        self.project = Project()
        self.project.tracks[0].clips.append(_video(0.0))

    def _settings(self, name):
        return ExportSettings(output_path=os.path.join(tempfile.gettempdir(), "ve_p3_queue", name))

    def test_jobs_run_one_after_another_and_report_status(self):
        done = []
        self.widget.job_finished.connect(lambda job_id, status: done.append(status))
        for name in ("a.mp4", "fail.mp4", "c.mp4"):
            self.widget.add_job(self.project, self._settings(name))
        self.assertTrue(_wait_for(lambda: len(done) == 3, 10))
        self.assertEqual(done, ["Done", "Failed", "Done"])
        self.assertEqual([w.settings.output_path[-8:] for w in _FakeWorker.instances][1], "fail.mp4")
        self.assertIn("FFmpeg exited", self.widget.table.item(1, 1).text())
        self.assertNotIn("\nlog", self.widget.table.item(1, 1).text())
        self.widget.table.selectRow(1)
        self.widget.retry_selected()
        self.assertTrue(_wait_for(lambda: len(done) == 4, 10))
        self.widget.clear_finished()
        self.assertEqual(self.widget.queue.jobs, [])

    def test_cancel_running_and_queued_jobs(self):
        self.widget.add_job(self.project, self._settings("hang.mp4"))
        self.widget.add_job(self.project, self._settings("next.mp4"))
        self.assertTrue(_wait_for(lambda: self.widget.queue.jobs[0].progress == 0.5, 10))
        self.assertEqual(self.widget.table.item(0, 3).text(), "00:03")
        self.widget.table.selectRow(1)
        self.widget.cancel_selected()
        self.assertEqual(self.widget.queue.jobs[1].status, JobStatus.CANCELLED)
        self.widget.table.selectRow(0)
        self.assertFalse(self.widget.remove_btn.isEnabled())
        self.widget.cancel_selected()
        self.assertTrue(_wait_for(lambda: not self.widget.is_running() and self.widget._thread is None, 10))
        self.assertEqual(self.widget.queue.jobs[0].status, JobStatus.CANCELLED)
        self.assertEqual(len(_FakeWorker.instances), 1)  # the cancelled waiting job never started

    def test_cancel_all_and_wait(self):
        self.widget.add_job(self.project, self._settings("hang.mp4"))
        self.widget.add_job(self.project, self._settings("other.mp4"))
        self.assertTrue(_wait_for(lambda: self.widget.is_running(), 10))
        self.assertTrue(self.widget.cancel_all_and_wait())
        self.assertEqual({j.status for j in self.widget.queue.jobs}, {JobStatus.CANCELLED})


class ClipGestureTests(_QtCase):  # F7
    def setUp(self):
        self.project = Project()
        self.a, self.b, self.c = _video(0.0), _video(4.0), _video(10.0)
        self.b.source_in, self.b.source_out = 2.0, 6.0
        self.project.tracks[0].clips += [self.a, self.b, self.c]
        self.history = History(validator=self.project.validate)
        self.timeline = TimelineWidget()
        self.timeline.set_project(self.project, self.history)
        self.addCleanup(self.timeline.deleteLater)
        self.pps = self.timeline.pixels_per_second

    def _drag(self, clip, mode, seconds):
        item = self.timeline._clip_items[clip.id]
        x0 = item.scenePos().x()
        item.begin_drag(mode, x0)
        item.update_drag(x0 + seconds * self.pps)
        item.finish_drag()

    def test_modifier_mapping(self):
        none, ctrl, alt, shift = Qt.NoModifier, Qt.ControlModifier, Qt.AltModifier, Qt.ShiftModifier
        self.assertEqual(drag_mode_for(2, 100, none), "trim_left")
        self.assertEqual(drag_mode_for(98, 100, ctrl), "roll_right")
        self.assertEqual(drag_mode_for(2, 100, ctrl), "roll_left")
        self.assertEqual(drag_mode_for(50, 100, alt), "slip")
        self.assertEqual(drag_mode_for(50, 100, ctrl | alt), "slide")
        self.assertEqual(drag_mode_for(50, 100, shift), "ripple_move")
        self.assertEqual(drag_mode_for(50, 100, ctrl), "toggle_select")
        self.assertEqual(drag_mode_for(50, 100, none), "move")

    def test_ctrl_edge_drag_rolls(self):
        self.timeline.snap_enabled = False
        self.timeline.signals.snap_guide_show.disconnect()
        self._drag(self.b, "roll_left", -1.0)
        self.assertAlmostEqual(self.a.timeline_end, 3.0)
        self.assertAlmostEqual(self.b.timeline_start, 3.0)
        self.assertAlmostEqual(self.b.source_in, 1.0)
        self.history.undo()
        self.assertAlmostEqual(self.a.timeline_end, 4.0)

    def test_alt_drag_slips_and_shift_drag_ripple_moves(self):
        self._drag(self.b, "slip", -1.5)  # drag left: later source shows
        self.assertAlmostEqual(self.b.source_in, 3.5)
        self.assertAlmostEqual(self.b.timeline_start, 4.0)
        self._drag(self.b, "ripple_move", 1.0)
        self.assertEqual([round(c.timeline_start, 3) for c in (self.a, self.b, self.c)], [0.0, 5.0, 11.0])

    def test_ctrl_alt_drag_slides_and_refusals_explain(self):
        messages = []
        self.timeline.status_message.connect(messages.append)
        self._drag(self.b, "slide", 0.5)
        self.assertAlmostEqual(self.b.timeline_start, 4.5)
        self.assertAlmostEqual(self.a.timeline_end, 4.5)
        self._drag(self.c, "ripple_move", -5.0)  # into b: refused, nothing moves
        self.assertAlmostEqual(self.c.timeline_start, 10.0)
        self.assertTrue(messages)
        self.assertAlmostEqual(self.timeline._clip_items[self.c.id].pos().x(), 130 + 10.0 * self.pps)

    def test_ripple_move_menu_entry(self):
        menu = self.timeline.build_clip_menu(self.b.id)
        self.assertIn("Ripple Move...", [a.text() for a in menu.actions()])
        self.assertTrue(self.timeline.ripple_move_selected(2.0))
        self.assertAlmostEqual(self.c.timeline_start, 12.0)


class EffectKeyframeUiTests(_QtCase):  # F12
    def setUp(self):
        self.project = Project()
        self.clip = _video(10.0, effects=[EffectInstance.create("color_correction", brightness=0.2)])
        self.project.tracks[0].clips.append(self.clip)
        self.history = History()
        self.panel = PropertiesPanel()
        self.addCleanup(self.panel.deleteLater)
        self.panel.set_project(self.project, self.history)
        self.panel.set_selected_clip(self.clip.id)

    def test_key_effect_parameter_at_playhead_animates_it(self):
        self.panel.set_playhead_time(11.0)
        self.assertTrue(self.panel.key_effect_param_at_playhead(0, "brightness"))
        self.panel.set_effect_param(0, "brightness", -0.4)
        self.panel.set_playhead_time(13.0)
        self.assertTrue(self.panel.key_effect_param_at_playhead(0, "brightness"))
        self.assertEqual(self.clip.effects[0].keyframes["brightness"], [(1.0, 0.2), (3.0, -0.4)])
        self.assertIn(":eval=frame", self.clip.effects[0].filter())
        holder = self.panel.effect_params_form.itemAt(0, QFormLayout.ItemRole.FieldRole).widget()
        self.assertEqual(holder.key_count_label.text(), "2 keys")
        self.assertTrue(self.panel.clear_effect_param_keyframes(0, "brightness"))
        self.assertNotIn("brightness", self.clip.effects[0].keyframes)
        for _ in range(4):
            self.history.undo()
        self.assertEqual(self.clip.effects[0].keyframes, {})

    def test_only_animatable_parameters_get_key_buttons(self):
        self.clip.effects.append(EffectInstance.create("blur"))
        self.panel.set_selected_clip(self.clip.id)
        self.panel.effects_list.setCurrentRow(1)
        field = self.panel.effect_params_form.itemAt(0, QFormLayout.ItemRole.FieldRole).widget()
        self.assertFalse(hasattr(field, "key_button"))
        self.panel.set_playhead_time(11.0)
        self.assertFalse(self.panel.key_effect_param_at_playhead(1, "sigma"))
        self.panel.set_playhead_time(30.0)
        self.assertFalse(self.panel.key_effect_param_at_playhead(0, "brightness"))  # playhead off the clip

    def test_color_sliders_edit_the_stack(self):
        clip = _video(20.0)
        self.project.tracks[0].clips.append(clip)
        self.panel.set_selected_clip(clip.id)
        self.panel.contrast_slider.setValue(150)
        self.assertEqual([e.kind for e in clip.effects], ["color_correction"])
        self.assertEqual(self.panel.effects_list.count(), 1)


class TimelineAccessibilityTests(_QtCase):  # F13
    def setUp(self):
        self.project = Project()
        self.a = _video(0.0, name="Arbol.mp4")
        self.b = _video(4.0, name="Rio.mp4")
        self.project.tracks[0].clips += [self.b, self.a]
        self.project.tracks[0].muted = True
        self.timeline = TimelineWidget()
        self.timeline.set_project(self.project, History())
        self.addCleanup(self.timeline.deleteLater)

    def test_tracks_and_clips_form_an_accessible_tree(self):
        view = QAccessible.queryAccessibleInterface(self.timeline.view)
        self.assertEqual(view.text(QAccessible.Name), "Timeline")
        self.assertEqual(view.childCount(), 2)  # Video 1, Audio 1
        track = view.child(0)
        self.assertEqual(track.role(), QAccessible.List)
        self.assertEqual(track.text(QAccessible.Name), "Video track Video 1, 2 clips, muted")
        self.assertEqual(view.indexOfChild(track), 0)
        self.assertEqual(track.childCount(), 2)
        first = track.child(0)
        self.assertEqual(first.role(), QAccessible.ListItem)
        self.assertEqual(first.text(QAccessible.Name),
                         "Video clip Arbol.mp4, 00:00:00:00 to 00:00:04:00, track Video 1")
        self.assertIn("OFFLINE", first.text(QAccessible.Description))
        self.assertEqual(track.indexOfChild(first), 0)
        self.assertEqual(first.parent().text(QAccessible.Name), track.text(QAccessible.Name))
        self.assertTrue(first.state().focusable)
        self.assertFalse(first.rect().isEmpty())
        self.assertEqual(view.child(1).childCount(), 0)

    def test_keyboard_selection_moves_focus_and_state(self):
        QAccessible.setActive(True)  # as with a screen reader running: events are delivered
        self.addCleanup(QAccessible.setActive, False)
        self.timeline.select_adjacent_clip(1)
        item = self.timeline._clip_items[self.a.id]
        iface = QAccessible.queryAccessibleInterface(item)
        self.assertTrue(iface.state().selected)
        self.timeline.select_adjacent_clip(1)
        self.assertEqual(self.timeline.selected_clip_id, self.b.id)
        self.assertTrue(QAccessible.queryAccessibleInterface(self.timeline._clip_items[self.b.id]).state().selected)

    def test_interfaces_follow_rebuilds(self):
        view = QAccessible.queryAccessibleInterface(self.timeline.view)
        self.timeline._layout_key = None
        self.timeline.rebuild()  # full rebuild: old items (and their interfaces) are deleted
        self.assertEqual(view.child(0).childCount(), 2)
        self.assertEqual(view.child(0).child(1).text(QAccessible.Name)[:19], "Video clip Rio.mp4,")


def _find_ffmpeg():
    bundled = os.path.join(REPO, "bin", "ffmpeg" + (".exe" if os.name == "nt" else ""))
    return bundled if os.path.isfile(bundled) else shutil.which("ffmpeg")


@unittest.skipUnless(_PYSIDE6_AVAILABLE and _find_ffmpeg(), "PySide6 and FFmpeg are needed")
class RenderQueueRealExportTests(_QtCase):  # F4, real FFmpeg through the real worker
    def test_two_queued_exports_render_in_order(self):
        ffmpeg = _find_ffmpeg()
        tmp = tempfile.mkdtemp(prefix="ve_p3_queue_")
        self.addCleanup(shutil.rmtree, tmp, True)
        src = os.path.join(tmp, "src.mp4")
        subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=64x36:r=30:d=1",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", src], check=True)
        project = Project()
        project.settings = ProjectSettings(64, 36, 30.0)
        asset = MediaAsset(path=src, duration=1.0, width=64, height=36, has_video=True)
        project.add_media_asset(asset)
        project.tracks[0].clips.append(_video(0.0, length=1.0, path=src, asset_id=asset.id))
        widget = RenderQueueWidget()
        self.addCleanup(widget.deleteLater)
        outputs = [os.path.join(tmp, f"q{i}.mp4") for i in (1, 2)]
        with patch("core.exporter.find_binary", return_value=ffmpeg):
            for out in outputs:
                widget.add_job(project, ExportSettings(output_path=out, width=64, height=36, use_gpu=False))
            project.tracks[0].clips.clear()  # later edits don't affect the queued snapshots
            self.assertTrue(_wait_for(lambda: widget.queue.pending_count() == 0 and widget._thread is None, 120))
        self.assertEqual([j.status for j in widget.queue.jobs], [JobStatus.DONE, JobStatus.DONE])
        self.assertTrue(all(os.path.getsize(out) > 0 for out in outputs))


if __name__ == "__main__":
    unittest.main()
