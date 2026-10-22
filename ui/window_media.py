"""The media side of the window: importing media and subtitles, captions, the media bin's
thumbnails, filmstrips and waveforms, subclips, Match Frame and proxies (R8: split out of
main_window.py). A mixin of MainWindow."""

from __future__ import annotations

import os
import threading
from typing import Optional

from PySide6.QtCore import QSettings, QThread, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QMessageBox,
)

from core.clip import Clip
from core.commands import AddClipCommand, AddTrackCommand
from core.history import CompositeCommand
from core.media_asset import MediaAsset
from core.proxy import generate_proxy, needs_proxy
from core.subtitles import (
    SubtitleParseError,
    cues_to_text_clips,
    is_subtitle_path,
    new_script_id,
    parse_subtitle_file,
    read_subtitle_file,
    split_ass_script,
    styled_subtitle_tracks,
)
from core.timeline_nav import format_timecode
from core.track import TrackType
from core.waveform import WaveformData, load_waveform
from ui.import_worker import ImportWorker


def _cache_dir() -> str:
    path = os.path.join(os.path.expanduser("~"), ".video_editor", "thumbnails")
    os.makedirs(path, exist_ok=True)
    return path

def _waveform_cache_dir() -> str:
    path = os.path.join(os.path.expanduser("~"), ".video_editor", "waveforms")
    os.makedirs(path, exist_ok=True)
    return path

AUTO_PROXY_SETTING = "media/auto_proxies"


class MediaMixin:
    # ------------------------------------------------------------------
    # Media import
    # ------------------------------------------------------------------
    def import_media_dialog(self) -> None:
        from ui.media_bin import SUPPORTED_EXTENSIONS

        paths, _ = QFileDialog.getOpenFileNames(self, "Import Media", "", f"Media Files ({SUPPORTED_EXTENSIONS});;All Files (*)")
        if paths:
            self.import_media_paths(paths)

    def import_subtitles_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Subtitles",
            "",
            "Subtitle Files (*.srt *.vtt *.ass *.ssa);;All Files (*)",
        )
        if path:
            self.import_subtitle_file(path)

    def import_subtitle_file(self, path: str, keep_styling: Optional[bool] = None) -> int:
        """Add an SRT/WebVTT file's cues as text clips on a "Subtitles" video track (one undo step).

        Reuses the existing "Subtitles" track when the new cues fit on it
        (no overlap, not locked); otherwise adds "Subtitles 2", "Subtitles 3"...
        An ASS/SSA file asks first (``keep_styling`` answers for tests):
        keep its styling -- styled lines rendered by libass, see
        :meth:`_import_styled_subtitles` -- or import its text and timing only.
        Returns the number of clips added (0 on an empty or unreadable file).
        """
        if self.history is None:
            return 0
        if os.path.splitext(path)[1].lower() in (".ass", ".ssa"):
            if keep_styling is None:
                keep_styling = self._ask_keep_subtitle_styling()
                if keep_styling is None:
                    return 0
            if keep_styling:
                return self._import_styled_subtitles(path)
        try:
            cues = parse_subtitle_file(path)
        except (OSError, SubtitleParseError) as exc:
            QMessageBox.warning(self, "Import Subtitles", f"Could not read subtitles from\n{path}\n\n{exc}")
            return 0
        clips = cues_to_text_clips(cues)
        if not clips:
            self.status_bar.showMessage("No subtitle cues were found in that file.", 4000)
            return 0
        self._add_caption_clips(clips, "Import Subtitles")
        self.status_bar.showMessage(f"Imported {len(clips)} subtitle cue(s).", 4000)
        return len(clips)

    def _add_caption_clips(self, clips, label: str) -> None:
        """Put caption text clips on a "Subtitles" track that has room (adding one if needed), as one undo step."""
        track = self._subtitle_track_for(clips)
        commands = []
        if track is None:
            existing = sum(1 for t in self.project.tracks if t.name.startswith("Subtitles"))
            add_track = AddTrackCommand(
                self.project, TrackType.VIDEO, name="Subtitles" if existing == 0 else f"Subtitles {existing + 1}"
            )
            commands.append(add_track)
            track_id = add_track.track_id
        else:
            track_id = track.id
        commands.extend(AddClipCommand(self.project, track_id, clip) for clip in clips)
        self.history.execute(CompositeCommand(commands, label))
        self.timeline.rebuild()
        self._on_project_modified()

    def generate_captions(self, model_size: Optional[str] = None, language: Optional[str] = "",
                          words_from=None, style: str = "lines", translate: bool = False) -> int:
        """Edit > Generate Captions (F17): transcribe the selected clip's speech with faster-whisper
        on a background thread and add the cues as caption clips (one undo step). ``model_size`` /
        ``language`` skip the dialog and ``words_from`` replaces the model (tests)."""
        from PySide6.QtWidgets import (
            QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QProgressDialog,
        )

        from core.transcribe import (
            CAPTION_STYLES,
            DEFAULT_MODEL,
            INSTALL_HINT,
            LANGUAGES,
            MODEL_DOWNLOAD,
            MODEL_SIZES,
            TranscriptionCancelled,
            transcribable_problem,
            transcribe_clip,
            whisper_available,
        )

        title = "Generate Captions"
        clip = self.project.find_clip(self.timeline.selected_clip_id) if self.timeline.selected_clip_id else None
        problem = transcribable_problem(clip)
        if problem:
            QMessageBox.information(self, title, problem)
            return 0
        if words_from is None and not whisper_available():
            QMessageBox.information(self, title, INSTALL_HINT)
            return 0
        if model_size is None:
            dialog = QDialog(self)
            dialog.setWindowTitle(title)
            form = QFormLayout(dialog)
            model_combo = QComboBox()
            for size in MODEL_SIZES:
                model_combo.addItem(f"{size} (downloads {MODEL_DOWNLOAD[size]} the first time)", size)
            model_combo.setCurrentIndex(MODEL_SIZES.index(DEFAULT_MODEL))
            form.addRow("Speech model", model_combo)
            language_combo = QComboBox()
            for name, code in LANGUAGES:
                language_combo.addItem(name, code)
            form.addRow("Language", language_combo)
            style_combo = QComboBox()
            for name, key in CAPTION_STYLES:
                style_combo.addItem(name, key)
            style_combo.setToolTip("One word at a time shows each word as it is spoken (the short-video look).")
            form.addRow("Caption style", style_combo)
            translate_check = QCheckBox("Translate the speech into English")
            translate_check.setToolTip("Whisper's own translation, with the same model: the captions come out in English.")
            form.addRow("", translate_check)
            note = QLabel("Bigger models are more accurate and slower. A graphics card (CUDA) is used "
                          "when available.")
            note.setWordWrap(True)
            form.addRow(note)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            form.addRow(buttons)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return 0
            model_size, language = model_combo.currentData(), language_combo.currentData()
            style, translate = style_combo.currentData(), translate_check.isChecked()
        progress = QProgressDialog("Transcribing the clip's speech…", "Cancel", 0, 0, self)
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(300)
        result: dict = {}

        class _Worker(QThread):
            def run(self_inner):
                try:
                    result["cues"] = transcribe_clip(clip, model_size, language or None,
                                                     should_cancel=progress.wasCanceled, words_from=words_from,
                                                     style=style, translate=translate,
                                                     on_words=lambda words: result.update(words=words))
                except TranscriptionCancelled:
                    result["cancelled"] = True
                except Exception as exc:  # noqa: BLE001 -- shown to the user
                    result["error"] = str(exc)

        worker = _Worker(self)
        worker.start()
        while not worker.wait(50):
            QApplication.processEvents()
        progress.close()
        if result.get("cancelled"):
            return 0
        if "error" in result:
            QMessageBox.warning(self, title, f"The clip couldn't be transcribed:\n{result['error']}")
            return 0
        self._remember_transcript(clip, result.get("words") if not translate else None)
        clips = cues_to_text_clips(result.get("cues", []), name_prefix="Caption")
        if not clips:
            QMessageBox.information(self, title, "No speech was recognised in this clip.")
            return 0
        self._add_caption_clips(clips, "Generate Captions")
        self.status_bar.showMessage(f"Added {len(clips)} caption(s).", 5000)
        return len(clips)

    def _remember_transcript(self, clip, words) -> None:
        """Keep the recognised words on the clip's asset for the media bin's speech search (F37)."""
        from core.transcript_search import merge_transcript

        asset = self.project.asset_for_clip(clip)
        if asset is None or not words:
            return
        asset.transcript = merge_transcript(asset.transcript, words)
        self._refresh_media_bin()
        self._on_project_modified()

    def go_to_spoken(self, asset_id: str, source_time: float) -> bool:
        """A speech-search hit (F37): move the playhead to where the timeline plays that moment."""
        from core.transcript_search import timeline_time_for

        when = timeline_time_for(self.project, asset_id, source_time)
        if when is None:
            self.status_bar.showMessage("That moment isn't on the timeline yet: add the clip first.", 6000)
            return False
        self.timeline.set_playhead(when)
        return True

    def _ask_keep_subtitle_styling(self) -> Optional[bool]:
        """True = keep the ASS/SSA styling, False = plain text, None = cancel."""
        box = QMessageBox(self)
        box.setWindowTitle("Import Subtitles")
        box.setText("This subtitle file has its own styling (fonts, colours, positions).")
        box.setInformativeText(
            "Keep the styling: the export draws the subtitles exactly as styled (with libass).\n"
            "Plain text: import only the text and timing, as editable captions."
        )
        keep = box.addButton("Keep Styling", QMessageBox.ButtonRole.AcceptRole)
        plain = box.addButton("Plain Text", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(keep)
        box.exec()
        clicked = box.clickedButton()
        return True if clicked is keep else False if clicked is plain else None

    def _import_styled_subtitles(self, path: str) -> int:
        """An ASS/SSA script as styled subtitle lines (one undo step): its header goes into
        ``project.subtitle_scripts``; its lines become text clips spread over as many
        "Subtitles" tracks as keep overlapping lines apart (the first may reuse an existing one)."""
        try:
            script = split_ass_script(read_subtitle_file(path))
        except (OSError, SubtitleParseError) as exc:
            QMessageBox.warning(self, "Import Subtitles", f"Could not read subtitles from\n{path}\n\n{exc}")
            return 0
        script_id = new_script_id()
        track_clips = styled_subtitle_tracks(script, script_id)
        if not track_clips:
            self.status_bar.showMessage("No subtitle lines were found in that file.", 4000)
            return 0
        self.project.subtitle_scripts[script_id] = script.header
        commands = []
        existing = sum(1 for t in self.project.tracks if t.name.startswith("Subtitles"))
        for index, clips in enumerate(track_clips):
            track = self._subtitle_track_for(clips) if index == 0 else None
            if track is None:
                existing += 1
                add_track = AddTrackCommand(
                    self.project, TrackType.VIDEO, name="Subtitles" if existing == 1 else f"Subtitles {existing}"
                )
                commands.append(add_track)
                track_id = add_track.track_id
            else:
                track_id = track.id
            commands.extend(AddClipCommand(self.project, track_id, clip) for clip in clips)
        self.history.execute(CompositeCommand(commands, "Import Styled Subtitles"))
        self.timeline.rebuild()
        self._on_project_modified()
        count = sum(len(clips) for clips in track_clips)
        self.status_bar.showMessage(
            f"Imported {count} styled subtitle line(s) on {len(track_clips)} track(s).", 4000
        )
        return count

    def _subtitle_track_for(self, clips) -> Optional["Track"]:
        """An existing unlocked "Subtitles..." video track all of ``clips`` fit on, else None."""
        for track in self.project.tracks:
            if track.track_type != TrackType.VIDEO or not track.name.startswith("Subtitles") or track.locked:
                continue
            if not any(track.has_overlap(clip) for clip in clips):
                return track
        return None

    def import_media_paths(self, paths) -> None:
        """Probe/thumbnail/waveform each path on a background thread.

        This used to run inline on the UI thread (papered over with
        ``processEvents()``), which visibly froze the app on a batch of
        large files. The heavy lifting now happens in :class:`ImportWorker`
        on its own ``QThread``; only the final project/UI updates happen
        back on the main thread via the signal handlers below.
        """
        # Subtitle files (dropped or picked with media) become text clips, not media.
        paths = list(paths)
        for path in [p for p in paths if is_subtitle_path(p)]:
            self.import_subtitle_file(path)
        paths = [p for p in paths if not is_subtitle_path(p)]
        new_paths = [p for p in paths if self.project.find_asset_by_path(p) is None]
        if not new_paths:
            return
        if self._import_thread is not None and self._import_thread.isRunning():
            QMessageBox.information(self, "Import Media", "An import is already in progress. Please wait for it to finish.")
            return

        self._import_errors = []
        self.import_progress.setRange(0, len(new_paths))
        self.import_progress.setValue(0)
        self.import_progress.setVisible(True)
        self.import_cancel_btn.setVisible(True)
        self.status_bar.showMessage(f"Importing {len(new_paths)} file(s)...")

        self._import_thread = QThread(self)
        self._import_worker = ImportWorker(new_paths, _cache_dir(), _waveform_cache_dir())
        self._import_worker.moveToThread(self._import_thread)

        self._import_thread.started.connect(self._import_worker.run)
        self._import_worker.asset_ready.connect(self._on_asset_imported)
        self._import_worker.asset_updated.connect(self._on_asset_details)
        self._import_worker.failed.connect(self._on_import_failed)
        self._import_worker.finished.connect(self._on_import_finished)
        self._import_worker.finished.connect(self._import_thread.quit)

        self._import_thread.start()

    def cancel_import(self) -> None:
        if self._import_worker is not None:
            self._import_worker.cancel()
            self.status_bar.showMessage("Cancelling import...")

    def _on_asset_imported(self, asset: MediaAsset) -> None:
        self.project.add_media_asset(asset)
        if hasattr(self, "_place_pending_voiceover"):
            self._place_pending_voiceover(asset)  # F34: a recorded voiceover goes onto the timeline
        self.import_progress.setValue(self.import_progress.value() + 1)
        self._refresh_media_bin()
        self._mark_dirty()
        if self._auto_proxies_enabled() and needs_proxy(asset):
            self.generate_proxy_for(asset.id)
        if asset.has_video and not asset.is_image:
            self.measure_decoders_for([asset.path])

    def _on_asset_details(self, asset_id: str, thumbnail_path, filmstrip_path, waveform_path) -> None:
        """Second import stage: a thumbnail/waveform arrived for an already-listed asset."""
        asset = self.project.find_asset(asset_id)
        if asset is None:
            return
        asset.thumbnail_path = thumbnail_path
        asset.filmstrip_path = filmstrip_path
        asset.waveform_path = waveform_path
        self._thumb_pixmap_cache.pop(asset_id, None)
        self._filmstrip_pixmap_cache.pop(asset_id, None)
        self._waveform_cache.pop(asset_id, None)
        self._refresh_media_bin()
        self.timeline.rebuild()

    def _on_import_failed(self, path: str, message: str) -> None:
        self._import_errors.append(f"{os.path.basename(path)}: {message}")
        self.import_progress.setValue(self.import_progress.value() + 1)

    def _on_import_finished(self) -> None:
        cancelled = self._import_worker is not None and self._import_worker.cancelled
        self.import_progress.setVisible(False)
        self.import_cancel_btn.setVisible(False)
        self.status_bar.showMessage("Import cancelled." if cancelled else "Import complete.", 3000)
        self._import_thread = None
        self._import_worker = None
        if self._import_errors:
            QMessageBox.warning(
                self,
                "Import Media",
                "Some files could not be imported:\n\n" + "\n".join(self._import_errors),
            )
            self._import_errors = []

    def _add_asset_to_timeline_end(self, asset_id: str) -> None:
        from core.subclips import media_item

        asset, source_in, source_out = media_item(self.project, asset_id)
        if asset is not None:
            self.timeline.add_media_to_timeline(asset, source_range=(source_in, source_out) if source_out is not None else None)

    def _thumbnail_for_asset(self, asset: MediaAsset) -> Optional[QPixmap]:
        if asset.id in self._thumb_pixmap_cache:
            return self._thumb_pixmap_cache[asset.id]
        if not asset.thumbnail_path or not os.path.exists(asset.thumbnail_path):
            return None
        pixmap = QPixmap(asset.thumbnail_path)
        self._thumb_pixmap_cache[asset.id] = pixmap
        return pixmap

    def _thumbnail_for_clip(self, clip: Clip) -> Optional[QPixmap]:
        asset = self.project.asset_for_clip(clip)
        if asset is None:
            return None
        return self._thumbnail_for_asset(asset)

    def _filmstrip_for_clip(self, clip: Clip) -> Optional[QPixmap]:
        asset = self.project.asset_for_clip(clip)
        if asset is None or not asset.filmstrip_path or not os.path.exists(asset.filmstrip_path):
            return None
        if asset.id in self._filmstrip_pixmap_cache:
            return self._filmstrip_pixmap_cache[asset.id]
        pixmap = QPixmap(asset.filmstrip_path)
        self._filmstrip_pixmap_cache[asset.id] = pixmap
        return pixmap

    def _waveform_for_clip(self, clip: Clip) -> Optional[WaveformData]:
        asset = self.project.asset_for_clip(clip)
        if asset is None or not asset.waveform_path:
            return None
        if asset.id in self._waveform_cache:
            return self._waveform_cache[asset.id]
        data = load_waveform(asset.waveform_path)
        self._waveform_cache[asset.id] = data
        return data

    def _place_selected_asset(self, overwrite: bool) -> None:
        from core.subclips import media_item

        asset_id = self.media_bin.selected_asset_id()
        asset, source_in, source_out = media_item(self.project, asset_id) if asset_id else (None, 0.0, None)
        if asset is None:
            self.status_bar.showMessage("Select a file in the Media Bin first.", 4000)
            return
        self.timeline.insert_asset_at_playhead(asset, overwrite=overwrite,
                                               source_range=(source_in, source_out) if source_out is not None else None)

    def _refresh_media_bin(self) -> None:
        self.media_bin.set_assets(self.project.media_pool, self._thumbnail_for_asset, self.project.subclips)

    def make_subclip(self, name: Optional[str] = None) -> bool:
        """Edit > Make Subclip (F29): the selected clip's source range -- or the part of it inside the
        timeline's In/Out -- as a named subclip in the Media Bin. ``name`` skips the dialog (tests)."""
        from core.subclips import AddSubclipCommand, Subclip, subclip_range

        clip = self.project.find_clip(self.timeline.selected_clip_id) if self.timeline.selected_clip_id else None
        asset = self.project.asset_for_clip(clip) if clip is not None else None
        if clip is None or asset is None or asset.is_image:
            self.status_bar.showMessage("Select a video or audio clip on the timeline first.", 4000)
            return False
        span = subclip_range(clip, self.timeline.in_point, self.timeline.out_point)
        if span is None:
            self.status_bar.showMessage("The In/Out range doesn't cover enough of the selected clip.", 4000)
            return False
        if name is None:
            default = f"{asset.display_name} {sum(1 for s in self.project.subclips if s.asset_id == asset.id) + 1}"
            name, ok = QInputDialog.getText(self, "Make Subclip", "Subclip name:", text=default)
            if not ok:
                return False
        subclip = Subclip(asset.id, span[0], span[1], name.strip() or "Subclip")
        self.history.execute(AddSubclipCommand(self.project, subclip))
        self._refresh_media_bin()
        self._reveal_asset(subclip.item_id)
        self._on_project_modified()
        self.status_bar.showMessage(f"Subclip {subclip.name}: {span[0]:.2f}–{span[1]:.2f} s of {asset.display_name}", 5000)
        return True

    def _rename_subclip(self, item_id: str) -> None:
        from core.subclips import RenameSubclipCommand, find_subclip

        subclip = find_subclip(self.project, item_id)
        if subclip is None:
            return
        name, ok = QInputDialog.getText(self, "Rename Subclip", "Subclip name:", text=subclip.name)
        if ok and name.strip():
            self.history.execute(RenameSubclipCommand(self.project, subclip.id, name.strip()))
            self._refresh_media_bin()
            self._on_project_modified()

    def _delete_subclip(self, item_id: str) -> None:
        from core.subclips import RemoveSubclipCommand, find_subclip

        subclip = find_subclip(self.project, item_id)
        if subclip is not None:
            self.history.execute(RemoveSubclipCommand(self.project, subclip.id))
            self._refresh_media_bin()
            self._on_project_modified()

    def match_frame(self) -> bool:
        """Match Frame (F28): find the source of the frame at the playhead -- select its file in the
        Media Bin, and make the next Insert (,) / Overwrite (.) of it start at that very frame."""
        from core.edit_ops import match_frame

        found = match_frame(self.project, self.timeline.playhead_time, self.timeline.selected_clip_id)
        if found is None:
            self.status_bar.showMessage("No clip with a source file under the playhead.", 4000)
            return False
        clip, source_t = found
        asset = self.project.asset_for_clip(clip)
        if asset is None:
            self.status_bar.showMessage("The clip's source file isn't in the Media Bin.", 4000)
            return False
        self._reveal_asset(asset.id)
        self.timeline.matched_source = (asset.id, source_t)
        self.status_bar.showMessage(
            f"Match Frame: {asset.display_name} at {format_timecode(source_t, self.project.settings.fps)} -- "
            "Insert (,) or Overwrite (.) places it from this frame.", 8000)
        return True

    def _reveal_asset(self, asset_id: str) -> None:
        widget = self.media_bin.list_widget
        for i in range(widget.count()):
            if str(widget.item(i).data(Qt.UserRole)) == asset_id:
                widget.setCurrentRow(i)
                widget.scrollToItem(widget.item(i))
                return

    # ------------------------------------------------------------------
    # Proxies (B10)
    # ------------------------------------------------------------------
    def _auto_proxies_enabled(self) -> bool:
        return QSettings().value(AUTO_PROXY_SETTING, False, type=bool)

    def _on_auto_proxy_toggled(self, checked: bool) -> None:
        QSettings().setValue(AUTO_PROXY_SETTING, bool(checked))

    def _proxy_for_selected_asset(self) -> None:
        asset_id = self.media_bin.selected_asset_id()
        if asset_id:
            self.generate_proxy_for(asset_id)
        else:
            self.status_bar.showMessage("Select a video in the Media Bin first.", 4000)

    def generate_proxy_for(self, asset_id: str) -> None:
        """Encode a 540p preview proxy in the background (export keeps using the original)."""
        asset = self.project.find_asset(asset_id)
        if asset is None or not asset.has_video or asset_id in self._proxy_jobs:
            return
        self._proxy_jobs.add(asset_id)
        self.status_bar.showMessage(f"Generating preview proxy for {asset.display_name}...")
        snapshot = MediaAsset.from_dict(asset.to_dict())

        def work() -> None:
            try:
                path = generate_proxy(snapshot)
            except Exception:  # noqa: BLE001 - reported as a failed proxy
                path = None
            self._proxy_finished.emit(asset_id, path)

        threading.Thread(target=work, name="proxy", daemon=True).start()

    def _on_proxy_finished(self, asset_id: str, path) -> None:
        self._proxy_jobs.discard(asset_id)
        asset = self.project.find_asset(asset_id)
        if asset is None:
            return
        if path:
            asset.proxy_path = path
            self._mark_dirty()
            self.status_bar.showMessage(f"Preview proxy ready: {asset.display_name}", 4000)
            self._refresh_media_bin()
            self.preview.refresh_after_edit()
        else:
            self.status_bar.showMessage(f"Couldn't create a proxy for {asset.display_name}.", 6000)
