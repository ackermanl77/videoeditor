"""Background rendering from the window: preview ranges, decoder measurement, scene-cut
detection, nested (compound) clips, motion tracking, the export queue and OTIO export (R8: split
out of main_window.py). A mixin of MainWindow; OTIO import and Export Frame stay in
main_window.py, where the tests patch its QMessageBox and render_frame."""

from __future__ import annotations

import os
import threading
from typing import List, Optional, Tuple

from PySide6.QtCore import QThread, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMessageBox,
)

from core.clip import Clip, MediaType
from core.commands import UpdateClipPropertyCommand
from core.decode_choice import is_measured, measure_and_remember
from core.ffmpeg_utils import (
    PER_SOURCE,
    decode_hwaccel_modes,
    find_binary,
)
from core.exporter import ExportCancelled, Exporter
from core.preview_render import cached_render, render_preview
from core.project import Project
from core.timeline_nav import format_timecode
from core.track import TrackType
from ui.export_dialog import ExportDialog


class RenderMixin:
    # -- background preview renders (F10c) -----------------------------------------
    def render_preview_range(self) -> bool:
        """Render the In/Out range (the whole timeline without one) in the background at
        preview size; the preview then plays it over that range. False if nothing to do."""
        if self.project.total_duration <= 0:
            self.status_bar.showMessage("Nothing to render.", 3000)
            return False
        start, end = self.timeline.in_out_range() or (0.0, self.project.total_duration)
        existing = cached_render(self.project, start, end)
        if existing is not None:
            self.preview.add_rendered_range(existing)
            self.status_bar.showMessage("Preview render already up to date.", 3000)
            return True
        self.cancel_preview_render()
        # A snapshot: edits made while it renders don't leak into the render (they make it stale instead).
        exporter = Exporter(Project.from_dict(self.project.to_dict()))
        self._preview_render_exporter = exporter
        self.status_bar.showMessage(f"Rendering preview {format_timecode(start, self.project.settings.fps)} - "
                                    f"{format_timecode(end, self.project.settings.fps)}...")

        def work() -> None:
            def progress(fraction: float, _label: str = "") -> None:
                self._preview_rendered.emit(None, f"progress:{fraction:.3f}")

            try:
                render = render_preview(exporter.project, start, end, on_progress=progress, exporter=exporter)
                self._preview_rendered.emit(render, "")
            except ExportCancelled:
                self._preview_rendered.emit(None, "cancelled")
            except Exception as exc:  # noqa: BLE001 -- shown in the status bar
                self._preview_rendered.emit(None, str(exc) or type(exc).__name__)

        self._preview_render_thread = threading.Thread(target=work, name="preview-render", daemon=True)
        self._preview_render_thread.start()
        return True

    def cancel_preview_render(self) -> None:
        if self._preview_render_exporter is not None:
            self._preview_render_exporter.cancel()
            self._preview_render_exporter = None

    def _on_preview_rendered(self, render, message: str) -> None:
        if message.startswith("progress:"):
            if self._preview_render_exporter is not None:
                self.status_bar.showMessage(f"Rendering preview... {float(message[9:]) * 100:.0f}%")
            return
        self._preview_render_exporter = None
        if render is not None:
            self.preview.add_rendered_range(render)
            self.status_bar.showMessage("Preview render ready: the preview plays it until the range is edited.", 5000)
        elif message == "cancelled":
            self.status_bar.showMessage("Preview render cancelled.", 3000)
        else:
            self.status_bar.showMessage(f"Preview render failed: {message}", 8000)

    # -- per-source GPU decoding (PG2) -------------------------------------------
    def measure_decoders_for(self, paths) -> bool:
        """In "Per source" GPU decoding, time the decoders on each not-yet-measured
        video file in the background (core/decode_choice.py); a no-op otherwise.
        True if a measurement was queued."""
        if decode_hwaccel_modes()[0] != PER_SOURCE:
            return False
        new = [p for p in paths if p and os.path.isfile(p) and not is_measured(p) and p not in self._decoder_queue]
        if not new:
            return False
        self._decoder_queue.extend(new)
        if self._decoder_thread is None or not self._decoder_thread.is_alive():
            self.status_bar.showMessage(f"Timing GPU and software decoding for {len(self._decoder_queue)} file(s)...")
            self._decoder_thread = threading.Thread(target=self._measure_decoder_queue, name="decoders", daemon=True)
            self._decoder_thread.start()
        return True

    def _measure_decoder_queue(self) -> None:
        measured = on_gpu = 0
        while self._decoder_queue:
            path = self._decoder_queue[0]
            try:
                winner = measure_and_remember(path)
            except Exception:  # noqa: BLE001 -- that file just keeps software decoding
                winner = None
            measured += 1
            on_gpu += winner is not None
            self._decoder_queue.pop(0)
        self._decoders_measured.emit(measured, on_gpu)

    def _on_decoders_measured(self, measured: int, on_gpu: int) -> None:
        self.status_bar.showMessage(
            f"Decoders timed for {measured} file(s): {on_gpu} decode faster on the GPU, the rest in software.", 6000)

    # ------------------------------------------------------------------
    def open_export_dialog(self) -> None:
        """Open a non-modal Export dialog: the editor stays usable while it renders a snapshot."""
        if not self.project.tracks or self.project.total_duration <= 0:
            QMessageBox.information(self, "Export Video", "Add some clips to the timeline before exporting.")
            return
        dialog = ExportDialog(self.project, self, in_out_range=self.timeline.in_out_range(), queue_available=True)
        dialog.queue_requested.connect(self.queue_export)
        dialog.setModal(False)
        self._export_dialogs.append(dialog)
        dialog.destroyed.connect(lambda _obj=None, d=dialog: self._forget_export_dialog(d))
        dialog.show()

    # -- OpenTimelineIO (F21) ----------------------------------------------------------
    def export_otio_dialog(self, path: Optional[str] = None) -> Optional[str]:
        """Save the timeline as an .otio file for Kdenlive, DaVinci Resolve and other OTIO readers."""
        from core.otio_io import export_otio

        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Export Timeline (OTIO)", f"{self.project.name}.otio",
                                                  "OpenTimelineIO (*.otio)")
            if not path:
                return None
        try:
            export_otio(self.project, path)
        except OSError as exc:
            QMessageBox.warning(self, "Export Timeline (OTIO)", f"The timeline couldn't be saved:\n{exc}")
            return None
        self.statusBar().showMessage(f"Timeline exported to {os.path.basename(path)}", 5000)
        return path

    # -- scene-cut detection (F19) ---------------------------------------------------
    def detect_scene_cuts(self, choice: Optional[str] = None) -> bool:
        """Find the shot changes in the selected video clip (FFmpeg scdet, on a background thread
        with a cancellable progress dialog), then split the clip there or mark them -- one undo
        step. ``choice`` ("split"/"markers") skips the question (tests)."""
        from PySide6.QtWidgets import QProgressDialog

        from core.clip import MediaType
        from core.scene_detect import SceneDetectionCancelled, build_scene_cut_command, detect_scene_cuts

        clip = self.project.find_clip(self.timeline.selected_clip_id) if self.timeline.selected_clip_id else None
        if clip is None or clip.media_type != MediaType.VIDEO:
            QMessageBox.information(self, "Detect Scene Cuts", "Select a video clip on the timeline first.")
            return False
        progress = QProgressDialog("Looking for scene cuts…", "Cancel", 0, 0, self)
        progress.setWindowTitle("Detect Scene Cuts")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(300)
        result: dict = {}

        class _Worker(QThread):
            def run(self_inner):
                try:
                    result["cuts"] = detect_scene_cuts(clip.source_path, clip.source_in, clip.source_out,
                                                       should_cancel=progress.wasCanceled)
                except SceneDetectionCancelled:
                    result["cancelled"] = True
                except Exception as exc:  # noqa: BLE001 -- shown to the user
                    result["error"] = str(exc)

        worker = _Worker(self)
        worker.start()
        while not worker.wait(50):
            QApplication.processEvents()
        progress.close()
        if result.get("cancelled"):
            return False
        if "error" in result:
            QMessageBox.warning(self, "Detect Scene Cuts", f"The clip couldn't be analysed:\n{result['error']}")
            return False
        cuts = result.get("cuts", [])
        if not cuts or build_scene_cut_command(self.project, clip.id, cuts, "markers") is None:
            QMessageBox.information(self, "Detect Scene Cuts", "No scene cuts found in this clip.")
            return False
        if choice is None:
            box = QMessageBox(self)
            box.setWindowTitle("Detect Scene Cuts")
            box.setText(f"Found {len(cuts)} scene cut{'s' if len(cuts) != 1 else ''} in this clip.")
            split = box.addButton("Split the Clip", QMessageBox.ButtonRole.AcceptRole)
            markers = box.addButton("Add Markers", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            clicked = box.clickedButton()
            choice = "split" if clicked is split else "markers" if clicked is markers else None
        if choice is None:
            return False
        ok = self.timeline._execute_and_select(build_scene_cut_command(self.project, clip.id, cuts, choice), False)
        self.statusBar().showMessage(
            f"Split the clip at {len(cuts)} scene cuts." if choice == "split" else f"Added {len(cuts)} scene-cut markers.", 5000)
        return ok

    # -- compound clips (F20) ---------------------------------------------------------
    def _render_compound_clips(self) -> bool:
        """Render the compound clips whose intermediate is missing or out of date, on a background
        thread with a progress dialog. False (with a message) if a render failed."""
        from PySide6.QtWidgets import QProgressDialog

        from core.nesting import ensure_nested_renders, missing_renders

        missing = missing_renders(self.project)
        if not missing:
            ensure_nested_renders(self.project, render_missing=False)
            return True
        progress = QProgressDialog(f"Rendering {len(missing)} compound clip{'s' if len(missing) != 1 else ''}…",
                                   None, 0, 0, self)
        progress.setWindowTitle("Compound Clips")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(300)
        result: dict = {}
        project = self.project

        class _Worker(QThread):
            def run(self_inner):
                try:
                    ensure_nested_renders(project)
                except Exception as exc:  # noqa: BLE001 -- shown to the user
                    result["error"] = str(exc)

        worker = _Worker(self)
        worker.start()
        while not worker.wait(50):
            QApplication.processEvents()
        progress.close()
        if "error" in result:
            QMessageBox.warning(self, "Compound Clips", f"A compound clip couldn't be rendered:\n{result['error']}")
            return False
        self.timeline.rebuild()
        self.preview.refresh_after_edit()
        return True

    def nest_selected_clips(self) -> bool:
        """Timeline > Nest Selected Clips (F20): the selected clips become one compound clip."""
        from core.nesting import NestClipsCommand, NestingError

        ids = list(self.timeline.selected_clip_ids)
        try:
            command = NestClipsCommand(self.project, ids)
        except NestingError as exc:
            QMessageBox.information(self, "Nest Clips", str(exc))
            return False
        self.timeline._execute_and_select(command, False)
        if not self._render_compound_clips():
            self.history.undo()
            self.timeline.rebuild()
            return False
        self.timeline.select_clips([command.compound.id])
        self.statusBar().showMessage(f"Nested {len(command.originals)} clips into “{command.compound.name}”.", 5000)
        return True

    def expand_compound_clip(self) -> bool:
        """Timeline > Expand Compound Clip (F20): put a compound clip's clips back on the timeline."""
        from core.nesting import NestingError, UnnestClipCommand

        clip_id = self.timeline.selected_clip_id
        try:
            command = UnnestClipCommand(self.project, clip_id) if clip_id else None
        except NestingError as exc:
            QMessageBox.information(self, "Expand Compound Clip", str(exc))
            return False
        if command is None:
            QMessageBox.information(self, "Expand Compound Clip", "Select a compound clip first.")
            return False
        ok = self.timeline._execute_and_select(command, False)
        self.timeline.select_clips([clip.id for _track, clip in command.placements])
        return ok

    # -- motion tracking (F18) -------------------------------------------------------
    def _motion_track_target(self, follower: Clip) -> Optional[Clip]:
        """The video clip under the playhead on the nearest video track below ``follower``'s."""
        video_tracks = [t for t in self.project.tracks if t.track_type == TrackType.VIDEO]
        follower_track = self.project.find_clip_track(follower.id)
        if follower_track not in video_tracks:
            return None
        playhead = self.timeline.playhead_time
        for track in reversed(video_tracks[:video_tracks.index(follower_track)]):
            clip = track.clip_at(playhead)
            if clip is not None and clip.media_type == MediaType.VIDEO:
                return clip
        return None

    def track_motion(self, box: Optional[Tuple[float, float, float, float]] = None, keep_offset: bool = True) -> bool:
        """Make the selected video/image clip follow an object in the video clip beneath it (F18):
        the user draws a box on the frame under the playhead, FFmpeg's find_rect tracks it on a
        background thread, and the result becomes the clip's position keyframes -- one undo step.
        ``box`` (fractions of the frame) skips the dialog (tests)."""
        from PySide6.QtWidgets import QProgressDialog

        from core.motion_track import (
            TRACK_WIDTH,
            TrackingCancelled,
            follow_keyframes,
            trackable_problem,
            track_object,
        )

        title = "Track Motion"
        follower = self.project.find_clip(self.timeline.selected_clip_id) if self.timeline.selected_clip_id else None
        if follower is None or follower.media_type not in (MediaType.VIDEO, MediaType.IMAGE):
            QMessageBox.information(self, title, "Select the video or image clip that should follow the object "
                                                 "(on a track above the clip to track), then put the playhead "
                                                 "on a frame showing the object.")
            return False
        tracked = self._motion_track_target(follower)
        if tracked is None:
            QMessageBox.information(self, title, "There's no video clip under the playhead on a track below the "
                                                 "selected clip.")
            return False
        problem = trackable_problem(tracked)
        if problem:
            QMessageBox.information(self, title, f"That clip can't be tracked: {problem}.")
            return False
        asset = self.project.asset_for_clip(tracked)
        if asset is None or not asset.width or not asset.height:
            QMessageBox.information(self, title, "The tracked clip's video size is unknown (is its file offline?).")
            return False
        local = min(max(0.0, self.timeline.playhead_time - tracked.timeline_start), tracked.playable_duration)
        frame_time = tracked.source_time_at(local)
        if box is None:
            import subprocess
            import tempfile

            from ui.motion_track_dialog import MotionTrackDialog

            handle, png = tempfile.mkstemp(prefix="ve_track_frame_", suffix=".png")
            os.close(handle)
            try:
                subprocess.run([find_binary("ffmpeg"), "-hide_banner", "-v", "error", "-y", "-ss",
                                f"{frame_time:.3f}", "-i", tracked.source_path, "-frames:v", "1",
                                "-vf", f"scale={TRACK_WIDTH}:-2", png],
                               capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                image = QImage(png)
            finally:
                try:
                    os.remove(png)
                except OSError:
                    pass
            if image.isNull():
                QMessageBox.warning(self, title, "The frame under the playhead couldn't be read.")
                return False
            dialog = MotionTrackDialog(image, tracked.name, follower.name, self)
            if dialog.exec() != dialog.DialogCode.Accepted or dialog.picker.box() is None:
                return False
            box, keep_offset = dialog.picker.box(), dialog.keep_offset.isChecked()
        progress = QProgressDialog("Tracking the object…", "Cancel", 0, 0, self)
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(300)
        result: dict = {}
        source_size = (asset.width, asset.height)

        class _Worker(QThread):
            def run(self_inner):
                try:
                    result["points"] = track_object(tracked.source_path, tracked.source_in, tracked.source_out,
                                                    frame_time, box, source_size, should_cancel=progress.wasCanceled)
                except TrackingCancelled:
                    result["cancelled"] = True
                except Exception as exc:  # noqa: BLE001 -- shown to the user
                    result["error"] = str(exc)

        worker = _Worker(self)
        worker.start()
        while not worker.wait(50):
            QApplication.processEvents()
        progress.close()
        if result.get("cancelled"):
            return False
        if "error" in result:
            QMessageBox.warning(self, title, f"The object couldn't be tracked:\n{result['error']}")
            return False
        canvas = (self.project.settings.width, self.project.settings.height)
        keyframes = follow_keyframes(result.get("points", []), tracked, follower, source_size, canvas, keep_offset)
        if len(keyframes) < 2:
            QMessageBox.information(self, title, "The object wasn't found in enough frames while the selected "
                                                 "clip is on screen. Try a box with more detail.")
            return False
        command = UpdateClipPropertyCommand(self.project, follower.id, "position_keyframes", keyframes, "Track Motion")
        ok = self.timeline._execute_and_select(command, False)
        self.properties.set_selected_clip(follower.id)
        self.statusBar().showMessage(f"Tracked the object: {len(keyframes)} position keyframes.", 5000)
        return ok

    def track_effect_mask(self, row: int, box: Optional[Tuple[float, float, float, float]] = None) -> bool:
        """Effect > Track Mask (F32): track an object in the selected clip's own footage and make the
        effect's mask centre follow it -- one undo step. ``box`` (fractions of the frame) skips the dialog."""
        from PySide6.QtWidgets import QProgressDialog

        from core.effect_masks import clean_mask
        from core.motion_track import TRACK_WIDTH, TrackingCancelled, mask_track_keyframes, track_object

        title = "Track Mask"
        clip = self.project.find_clip(self.timeline.selected_clip_id) if self.timeline.selected_clip_id else None
        if clip is None or clip.media_type != MediaType.VIDEO or not (0 <= row < len(clip.effects)):
            QMessageBox.information(self, title, "Select a video clip and one of its effects with a mask.")
            return False
        mask = clean_mask(clip.effects[row].mask)
        asset = self.project.asset_for_clip(clip)
        if mask is None:
            QMessageBox.information(self, title, "Give the effect a mask shape first (rectangle, ellipse or gradient).")
            return False
        if asset is None or not asset.width or not asset.height:
            QMessageBox.information(self, title, "The clip's video size is unknown (is its file offline?).")
            return False
        local = min(max(0.0, self.timeline.playhead_time - clip.timeline_start), clip.playable_duration)
        frame_time = clip.source_time_at(local)
        if box is None:
            import subprocess
            import tempfile

            from ui.motion_track_dialog import MotionTrackDialog

            handle, png = tempfile.mkstemp(prefix="ve_track_frame_", suffix=".png")
            os.close(handle)
            try:
                subprocess.run([find_binary("ffmpeg"), "-hide_banner", "-v", "error", "-y", "-ss", f"{frame_time:.3f}",
                                "-i", clip.source_path, "-frames:v", "1", "-vf", f"scale={TRACK_WIDTH}:-2", png],
                               capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                image = QImage(png)
            finally:
                try:
                    os.remove(png)
                except OSError:
                    pass
            if image.isNull():
                QMessageBox.warning(self, title, "The frame under the playhead couldn't be read.")
                return False
            dialog = MotionTrackDialog(image, clip.name, "the mask", self)
            dialog.setWindowTitle(title)
            dialog.keep_offset.setVisible(False)
            if dialog.exec() != dialog.DialogCode.Accepted or dialog.picker.box() is None:
                return False
            box = dialog.picker.box()
        progress = QProgressDialog("Tracking the object…", "Cancel", 0, 0, self)
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(300)
        result: dict = {}
        source_size = (asset.width, asset.height)

        class _Worker(QThread):
            def run(self_inner):
                try:
                    result["points"] = track_object(clip.source_path, clip.source_in, clip.source_out, frame_time, box,
                                                    source_size, should_cancel=progress.wasCanceled)
                except TrackingCancelled:
                    result["cancelled"] = True
                except Exception as exc:  # noqa: BLE001 -- shown to the user
                    result["error"] = str(exc)

        worker = _Worker(self)
        worker.start()
        while not worker.wait(50):
            QApplication.processEvents()
        progress.close()
        if result.get("cancelled"):
            return False
        if "error" in result:
            QMessageBox.warning(self, title, "The object couldn't be tracked:\n" + result["error"])
            return False
        track = mask_track_keyframes(result.get("points", []), clip, source_size,
                                     (self.project.settings.width, self.project.settings.height))
        if len(track) < 2:
            QMessageBox.information(self, title, "The object wasn't found in enough frames. Try a box with more detail.")
            return False
        ok = self.properties.set_effect_mask(row, {**mask, "track": track})
        self.properties.set_selected_clip(clip.id)
        self.statusBar().showMessage(f"The mask now follows the object: {len(track)} keyframes.", 5000)
        return ok

    def track_reframe(self, box: Optional[Tuple[float, float, float, float]] = None) -> bool:
        """Edit > Keep Subject in Frame (F35): track an object in the selected Fill clip and animate its
        crop focus so a narrower export (9:16 from 16:9) keeps the subject centred -- one undo step.
        ``box`` (fractions of the frame) skips the dialog."""
        from PySide6.QtWidgets import QProgressDialog

        from core.clip import ReframeMode
        from core.commands import UpdateClipPropertyCommand
        from core.motion_track import TRACK_WIDTH, TrackingCancelled, reframe_keyframes, track_object

        title = "Keep Subject in Frame"
        clip = self.project.find_clip(self.timeline.selected_clip_id) if self.timeline.selected_clip_id else None
        if clip is None or clip.media_type != MediaType.VIDEO:
            QMessageBox.information(self, title, "Select a video clip first.")
            return False
        asset = self.project.asset_for_clip(clip)
        if asset is None or not asset.width or not asset.height:
            QMessageBox.information(self, title, "The clip's video size is unknown (is its file offline?).")
            return False
        canvas = (self.project.settings.width, self.project.settings.height)
        if abs(asset.width / asset.height - canvas[0] / canvas[1]) < 0.01:
            QMessageBox.information(self, title, "The clip already has the project's shape: there is nothing to crop. "
                                                 "Use it in a project with another aspect (e.g. 9:16).")
            return False
        local = min(max(0.0, self.timeline.playhead_time - clip.timeline_start), clip.playable_duration)
        frame_time = clip.source_time_at(local)
        if box is None:
            import subprocess
            import tempfile

            from ui.motion_track_dialog import MotionTrackDialog

            handle, png = tempfile.mkstemp(prefix="ve_track_frame_", suffix=".png")
            os.close(handle)
            try:
                subprocess.run([find_binary("ffmpeg"), "-hide_banner", "-v", "error", "-y", "-ss", f"{frame_time:.3f}",
                                "-i", clip.source_path, "-frames:v", "1", "-vf", f"scale={TRACK_WIDTH}:-2", png],
                               capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                image = QImage(png)
            finally:
                try:
                    os.remove(png)
                except OSError:
                    pass
            if image.isNull():
                QMessageBox.warning(self, title, "The frame under the playhead couldn't be read.")
                return False
            dialog = MotionTrackDialog(image, clip.name, "the crop", self)
            dialog.setWindowTitle(title)
            dialog.keep_offset.setVisible(False)
            if dialog.exec() != dialog.DialogCode.Accepted or dialog.picker.box() is None:
                return False
            box = dialog.picker.box()
        progress = QProgressDialog("Tracking the subject…", "Cancel", 0, 0, self)
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(300)
        result: dict = {}
        source_size = (asset.width, asset.height)

        class _Worker(QThread):
            def run(self_inner):
                try:
                    result["points"] = track_object(clip.source_path, clip.source_in, clip.source_out, frame_time, box,
                                                    source_size, should_cancel=progress.wasCanceled)
                except TrackingCancelled:
                    result["cancelled"] = True
                except Exception as exc:  # noqa: BLE001 -- shown to the user
                    result["error"] = str(exc)

        worker = _Worker(self)
        worker.start()
        while not worker.wait(50):
            QApplication.processEvents()
        progress.close()
        if result.get("cancelled"):
            return False
        if "error" in result:
            QMessageBox.warning(self, title, "The subject couldn't be tracked:\n" + result["error"])
            return False
        keyframes = reframe_keyframes(result.get("points", []), clip, source_size, canvas)
        if len(keyframes) < 2:
            QMessageBox.information(self, title, "The subject wasn't found in enough frames. Try a box with more detail.")
            return False
        from core.history import CompositeCommand

        self.history.execute(CompositeCommand([
            UpdateClipPropertyCommand(self.project, clip.id, "reframe_mode", ReframeMode.FILL, title),
            UpdateClipPropertyCommand(self.project, clip.id, "crop_focus_keyframes", keyframes, title),
        ], title))
        self.timeline.refresh_clip(clip.id)
        self.properties.set_selected_clip(clip.id)
        self._on_project_modified()
        self.statusBar().showMessage(f"The crop now follows the subject: {len(keyframes)} keyframes.", 5000)
        return True

    def queue_export(self, settings) -> None:
        """Queue an export of the project as it is right now (a snapshot) and show the queue."""
        job = self.render_queue.add_job(self.project, settings)
        self.render_queue_dock.show()
        self.render_queue_dock.raise_()
        self.status_bar.showMessage(f"Queued: {job.name}", 5000)

    def _on_queue_job_finished(self, job_id: str, status: str) -> None:
        job = self.render_queue.queue.find(job_id)
        if job is not None:
            self.status_bar.showMessage(f"Render queue: {job.name} -- {status}", 8000)

    def _forget_export_dialog(self, dialog) -> None:
        if dialog in self._export_dialogs:
            self._export_dialogs.remove(dialog)

    def running_exports(self) -> List[ExportDialog]:
        alive = []
        for dialog in list(self._export_dialogs):
            try:
                if dialog.is_running():
                    alive.append(dialog)
            except RuntimeError:  # already deleted
                self._forget_export_dialog(dialog)
        return alive
