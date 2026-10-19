"""Playback preview panel.

The preview plays the topmost active video clip (falling back to an
audio-only clip when no video is present at the current time) using
``QMediaPlayer``. Full multi-track alpha compositing, transitions and text
overlays are computed only at export time by the FFmpeg render engine — the
preview intentionally trades perfect What-You-See-Is-What-You-Get accuracy
for a simple, fast, maintainable implementation. A small on-screen note
reminds the user of this.

Three additions narrow the gap:

* **Accurate frame** (checkbox): while paused or scrubbing, the exact
  export composite at the playhead is rendered by the export engine itself
  (core/frame_render.py) on a worker thread and shown instead; a
  generation counter drops results a newer request has superseded.
* **Next-clip preloading**: a second player opens the next clip's file
  shortly before a cut, and the two players swap at the cut, so a cut
  between different files no longer stalls on opening the file.
* **Proxies**: when an asset has a preview proxy (core/proxy.py) the
  preview plays that instead of the heavy original.
"""

from __future__ import annotations

import os
import threading
from typing import Dict, Optional, Set, Tuple

from PySide6.QtCore import QPointF, QRectF, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QFontDatabase, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from core.clip import Clip, MediaType
from core.frame_render import FrameRenderError, render_frame
from core.mixer import track_audible, track_meter_gain
from core.preview_render import RenderedRange, range_key
from core.project import Project
from core.timeline_nav import format_timecode
from core.transform_geometry import clip_box, moved, scaled
from core.track import Track, TrackType
from core.loudness import (
    IntegratedLoudness,
    format_lufs,
    generate_loudness,
    load_loudness,
    loudness_at,
    loudness_cache_path,
)
from core.waveform import load_waveform, window_levels
from ui import theme

POLL_INTERVAL_MS = 40
# Open the next clip's file this long before a cut.
PRELOAD_LEAD_S = 1.5
# Wait this long after the last seek/edit before rendering an accurate frame.
ACCURATE_FRAME_DEBOUNCE_MS = 150
# J/L shuttle speeds (repeat presses step through them).
SHUTTLE_RATES = (1.0, 2.0, 4.0)


def default_loudness_cache_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".video_editor", "loudness")


class PreviewPlayer(QWidget):
    """Video/audio preview synced to the project timeline."""

    time_changed = Signal(float)
    playing_changed = Signal(bool)
    # (generation, PNG bytes or an error string) from the frame-render thread.
    _frame_ready = Signal(int, object)
    # On-canvas transform (F15): clip id, transform_x, transform_y, transform_scale after a drag.
    transform_edited = Signal(str, float, float, float)
    # A source file's loudness measurement finished (path, cache file or None), from a worker thread.
    _loudness_ready = Signal(str, object)
    # Background preview renders (F10c) were added or went stale/valid: [(start, end, valid)].
    render_ranges_changed = Signal(list)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project: Optional[Project] = None
        self._active_clip: Optional[Clip] = None
        self._current_source: Optional[str] = None
        self._is_playing = False
        self._current_time = 0.0
        self._suppress_slider_signal = False
        # Signed shuttle rate: 1 = normal play, 2/4 = fast forward (L), <0 =
        # reverse (J). Reverse is stepped by seeking (QMediaPlayer can't play
        # backwards).
        self._shuttle_rate = 1.0
        # What the player was last loaded for (see refresh_after_edit).
        self._loaded_signature = None
        self._waveform_cache = {}
        # Live loudness (core/loudness.py): measurements by source path, the
        # ones being measured, and the integrated value since Play.
        self.loudness_cache_dir = default_loudness_cache_dir()
        self._loudness_cache: Dict[str, object] = {}
        self._loudness_jobs: Set[str] = set()
        self._integrated = IntegratedLoudness()
        # Preview renders of In/Out ranges (core/preview_render.py), newest last.
        self._renders: list = []

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        # Second player: opens the next clip's file ahead of a cut (F10a).
        self._preload_player = QMediaPlayer(self)
        self._preload_audio = QAudioOutput(self)
        self._preload_audio.setMuted(True)
        self._preload_player.setAudioOutput(self._preload_audio)
        self._preloaded_source: Optional[str] = None

        # Accurate-frame rendering state (F10b).
        self._frame_generation = 0
        self._frame_timer = QTimer(self)
        self._frame_timer.setSingleShot(True)
        self._frame_timer.setInterval(ACCURATE_FRAME_DEBOUNCE_MS)
        self._frame_timer.timeout.connect(self._start_frame_render)
        self._frame_ready.connect(self._on_frame_ready)
        self.last_frame_error: Optional[str] = None

        self._build_ui()
        self._connect_signals()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._on_poll)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        title = QLabel("PREVIEW")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background-color: black;")
        self.video_widget = QVideoWidget()
        self.player.setVideoOutput(self.video_widget)
        self._preload_video = QVideoWidget()
        self._preload_player.setVideoOutput(self._preload_video)
        self.placeholder_label = QLabel()
        self.placeholder_label.setAlignment(Qt.AlignCenter)
        self.placeholder_label.setStyleSheet("background-color: black; color: white; font-size: 24px;")
        self.frame_label = QLabel()  # accurate rendered frames
        self.frame_label.setAlignment(Qt.AlignCenter)
        self.frame_label.setStyleSheet("background-color: black;")
        self.stack.addWidget(self.video_widget)
        self.stack.addWidget(self._preload_video)
        self.stack.addWidget(self.placeholder_label)
        self.stack.addWidget(self.frame_label)
        layout.addWidget(self.stack, 1)
        self.safe_overlay = _SafeAreaOverlay(self.stack)
        self.safe_overlay.hide()
        self.transform_overlay = _TransformOverlay(self.stack)
        self.transform_overlay.edited.connect(self.transform_edited)
        self.transform_overlay.hide()

        controls = QHBoxLayout()
        self.play_btn = QPushButton()
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_btn.setFixedWidth(36)
        controls.addWidget(self.play_btn)

        self.time_label = QLabel("00:00:00:00 / 00:00:00:00")
        self.time_label.setStyleSheet("color: #9aa1ad;")
        controls.addWidget(self.time_label)

        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setMinimum(0)
        self.seek_slider.setMaximum(1000)
        controls.addWidget(self.seek_slider, 1)

        self.accurate_checkbox = QCheckBox("Accurate frame")
        self.accurate_checkbox.setToolTip(
            "While paused or scrubbing, show the exact exported frame (every track, transitions, text, "
            "effects), rendered by the export engine at preview size."
        )
        self.accurate_checkbox.toggled.connect(self._on_accurate_toggled)
        controls.addWidget(self.accurate_checkbox)

        self.safe_checkbox = QCheckBox("Safe areas")
        self.safe_checkbox.setToolTip("Show action-safe and title-safe guides over the preview.")
        self.safe_checkbox.toggled.connect(self._on_safe_areas_toggled)
        controls.addWidget(self.safe_checkbox)

        self.transform_checkbox = QCheckBox("Transform")
        self.transform_checkbox.setToolTip(
            "Move and scale the selected video/image clip right on the preview: drag inside its box to move it, "
            "drag a corner to scale it (one undo step per drag)."
        )
        self.transform_checkbox.toggled.connect(self._update_transform_overlay)
        controls.addWidget(self.transform_checkbox)

        self.meters = _AudioLevelMeters()
        controls.addWidget(self.meters)

        self.loudness_label = QLabel()
        self.loudness_label.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.loudness_label.setStyleSheet(f"color: {theme.COLOR_TEXT_DIM};")
        self.loudness_label.setToolTip(
            "EBU R128 loudness of the clip under the playhead, with its volume: M = momentary (0.4 s), "
            "S = short-term (3 s), I = integrated since you pressed Play (gated like R128). Each source "
            "file is measured once in the background; the Export dialog measures the real mix."
        )
        self.loudness_label.setAccessibleName("Loudness meter")
        controls.addWidget(self.loudness_label)
        self._show_loudness(None, None)

        layout.addLayout(controls)

        self.note_label = QLabel(
            "Playback shows the top active video clip only. Tick 'Accurate frame' to see the exact "
            "export composite (transitions, text, effects) while paused."
        )
        self.note_label.setWordWrap(True)
        # COLOR_TEXT_DIM: the old #6b7280 measured 3.2:1 on the panel background.
        self.note_label.setStyleSheet(f"color: {theme.COLOR_TEXT_DIM}; font-size: 10px;")
        layout.addWidget(self.note_label)

    def _connect_signals(self) -> None:
        self._loudness_ready.connect(self._on_loudness_ready)
        self.play_btn.clicked.connect(self.toggle_play)
        self.seek_slider.sliderPressed.connect(self._pause_for_scrub)
        self.seek_slider.sliderMoved.connect(self._on_slider_moved)
        self.player.errorOccurred.connect(self._on_player_error)
        self._preload_player.errorOccurred.connect(self._on_player_error)

    # ------------------------------------------------------------------
    def set_project(self, project: Project) -> None:
        self.project = project
        self._renders = []
        self.render_ranges_changed.emit([])
        self._current_source = None
        self._preloaded_source = None
        self._active_clip = None
        self._waveform_cache.clear()
        self.seek_to(0.0)

    def media_path_for(self, clip: Clip) -> str:
        """The file the preview plays for ``clip``: its asset's proxy when one exists, else the source."""
        if self.project is not None and clip.nested:
            from core.nesting import render_path

            return render_path(clip.nested, self.project.settings)  # a compound clip's render (F20)
        if self.project is not None:
            asset = self.project.asset_for_clip(clip)
            if asset is not None and asset.path == clip.source_path:
                return asset.preview_path()
        return clip.source_path

    def _total_duration(self) -> float:
        return self.project.total_duration if self.project else 0.0

    # ------------------------------------------------------------------
    # Clip resolution
    # ------------------------------------------------------------------
    # -- preview renders (F10c) -------------------------------------------------------
    def add_rendered_range(self, render: RenderedRange) -> None:
        """Play ``render``'s file over its range from now on (while it stays valid);
        it replaces older renders it overlaps."""
        self._renders = [r for r in self._renders if r.end <= render.start or r.start >= render.end] + [render]
        self._update_render_validity(force_signal=True)
        self.seek_to(self._current_time)

    def render_ranges(self) -> list:
        return [(r.start, r.end, r.valid) for r in self._renders]

    def _update_render_validity(self, force_signal: bool = False) -> None:
        changed = force_signal
        for render in self._renders:
            valid = (self.project is not None and os.path.isfile(render.path)
                     and range_key(self.project, render.start, render.end) == render.key)
            if valid != render.valid:
                render.valid = valid
                changed = True
        if changed:
            self.render_ranges_changed.emit(self.render_ranges())

    def _rendered_clip_at(self, t: float) -> Optional[Clip]:
        """A stand-in clip playing the valid render that covers ``t``, if any: the rest
        of the preview (seeking, polling, cuts) then treats it like any video clip."""
        for render in reversed(self._renders):
            if render.valid and render.start <= t < render.end - 0.02:
                clip = Clip(
                    media_type=MediaType.VIDEO,
                    source_path=render.path,
                    source_in=0.0,
                    source_out=render.end - render.start,
                    timeline_start=render.start,
                    name="Rendered preview",
                )
                clip.id = f"render-{render.key[:12]}"
                return clip
        return None

    def _resolve_active_clip(self, t: float) -> Tuple[Optional[Clip], Optional[Track]]:
        rendered = self._rendered_clip_at(t)
        if rendered is not None:
            return rendered, None
        if self.project is None:
            return None, None
        video_tracks = [tr for tr in self.project.tracks if tr.track_type == TrackType.VIDEO]
        for track in reversed(video_tracks):  # topmost (highest index) first
            if track.hidden:
                continue
            clip = track.clip_at(t)
            if clip is not None and clip.media_type in (MediaType.VIDEO, MediaType.IMAGE, MediaType.TEXT):
                return clip, track
        audio_tracks = [tr for tr in self.project.tracks if tr.track_type == TrackType.AUDIO]
        for track in audio_tracks:
            clip = track.clip_at(t)
            if clip is not None:
                return clip, track
        return None, None

    # ------------------------------------------------------------------
    # Playback control
    # ------------------------------------------------------------------
    def toggle_play(self) -> None:
        if self._is_playing:
            self.pause()
        else:
            self._shuttle_rate = 1.0
            self.play()

    @property
    def shuttle_rate(self) -> float:
        return self._shuttle_rate if self._is_playing else 0.0

    def shuttle(self, direction: int) -> None:
        """J (direction < 0) / L (> 0): play that way; pressing again speeds up 1x -> 2x -> 4x."""
        current = self.shuttle_rate
        if current != 0 and (current > 0) == (direction > 0):
            index = SHUTTLE_RATES.index(abs(current)) if abs(current) in SHUTTLE_RATES else 0
            magnitude = SHUTTLE_RATES[min(index + 1, len(SHUTTLE_RATES) - 1)]
        else:
            magnitude = SHUTTLE_RATES[0]
        self._shuttle_rate = magnitude if direction > 0 else -magnitude
        if self._shuttle_rate < 0:
            self.player.pause()
            self._is_playing = True
            self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
            self._poll_timer.start()
            self.playing_changed.emit(True)
        else:
            self.play()

    def stop_shuttle(self) -> None:
        """K: pause and reset the shuttle speed."""
        self._shuttle_rate = 1.0
        if self._is_playing:
            self.pause()

    def play(self) -> None:
        if self.project is None:
            return
        if self._current_time >= self._total_duration() - 0.01:
            self._current_time = 0.0
        self._load_for_time(self._current_time, autoplay=True)
        self._is_playing = True
        self._integrated.reset()
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
        self._poll_timer.start()
        self.playing_changed.emit(True)

    def pause(self) -> None:
        self.player.pause()
        self._is_playing = False
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self._poll_timer.stop()
        self.playing_changed.emit(False)
        self.request_accurate_frame()

    def _pause_for_scrub(self) -> None:
        if self._is_playing:
            self.pause()

    def _on_slider_moved(self, value: int) -> None:
        total = self._total_duration()
        t = (value / 1000.0) * total
        self.seek_to(t)

    def _signature(self, t: float):
        """Everything about the clip under ``t`` that decides what the player shows/plays."""
        clip, track = self._resolve_active_clip(t)
        if clip is None:
            return None
        return (
            clip.id, clip.media_type, clip.source_path, clip.source_in, clip.source_out,
            clip.timeline_start, clip.speed, tuple(clip.speed_keyframes), clip.speed_ramp_smooth, clip.reverse, clip.freeze_frame_duration,
            clip.text_style.content if clip.text_style else None, self.media_path_for(clip),
        )

    def refresh_after_edit(self) -> None:
        """Called after any project edit: reseek only if the clip under the playhead changed.

        Every Properties-panel slider tick used to reseek QMediaPlayer (dozens
        of setPosition calls a second while dragging), although most
        properties (colour, opacity, key, transform) don't affect this
        preview at all. Volume/mute are applied without a seek.
        """
        self.request_accurate_frame()  # any edit can change the composite
        if self._renders:
            self._update_render_validity()
        if self._signature(self._current_time) != self._loaded_signature:
            self.seek_to(self._current_time)
            return
        clip, track = self._resolve_active_clip(self._current_time)
        if clip is not None:
            self._apply_output_level(clip, track)
        self._update_audio_meters()
        self._update_time_label()

    def seek_to(self, t: float) -> None:
        total = self._total_duration()
        t = max(0.0, min(t, total)) if total > 0 else max(0.0, t)
        self._current_time = t
        # If we're mid-playback, a seek (e.g. clicking elsewhere on the
        # timeline ruler) should keep playing from the new position rather
        # than silently pausing the underlying player.
        self._load_for_time(t, autoplay=self._is_playing)
        self._update_time_label()
        if not self._suppress_slider_signal:
            self._update_slider()
        self.time_changed.emit(t)
        self.request_accurate_frame()
        self._update_audio_meters()

    # ------------------------------------------------------------------
    # Accurate frame (F10b)
    # ------------------------------------------------------------------
    def _on_accurate_toggled(self, checked: bool) -> None:
        if checked:
            self.request_accurate_frame()
        else:
            self._frame_generation += 1  # drop anything still rendering
            self._load_for_time(self._current_time, autoplay=self._is_playing)

    def _on_safe_areas_toggled(self, checked: bool) -> None:
        self.safe_overlay.setVisible(bool(checked))
        self.safe_overlay.raise_()

    def request_accurate_frame(self) -> None:
        """Schedule a render of the exact frame at the playhead (debounced; paused only)."""
        if self.accurate_checkbox.isChecked() and not self._is_playing and self.project is not None:
            self._frame_generation += 1
            self._frame_timer.start()

    def _start_frame_render(self) -> None:
        if self.project is None or self.project.total_duration <= 0:
            return
        generation = self._frame_generation
        t = self._current_time
        project = self.project
        size = (max(160, self.stack.width()), max(90, self.stack.height()))

        def work() -> None:
            from core.frame_render import preview_size

            try:
                quick = render_frame(project, t, preview_size(project, max(160, size[0] // 2), max(90, size[1] // 2)))
                self._frame_ready.emit(generation, ("proxy", quick))
                if generation != self._frame_generation:
                    return
                data = render_frame(project, t, preview_size(project, *size))
            except FrameRenderError as exc:
                data = str(exc)
            except Exception as exc:  # noqa: BLE001 - never let the worker thread die silently
                data = f"Frame render failed: {exc}"
            self._frame_ready.emit(generation, ("final", data))

        threading.Thread(target=work, name="frame-render", daemon=True).start()

    def _on_frame_ready(self, generation: int, data) -> None:
        """Show a rendered frame -- unless a newer request superseded it, or playback started."""
        if generation != self._frame_generation or self._is_playing or not self.accurate_checkbox.isChecked():
            return
        stage, payload = data if isinstance(data, tuple) and len(data) == 2 else ("final", data)
        if isinstance(payload, (bytes, bytearray)):
            pixmap = QPixmap()
            pixmap.loadFromData(bytes(payload), "PNG")
            self.last_frame_error = None
            self.frame_label.setPixmap(
                pixmap.scaled(self.frame_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
            self.frame_label.setToolTip("Exact export frame" if stage == "final" else "Low-resolution background render")
        else:
            self.last_frame_error = str(payload)
            self.frame_label.setPixmap(QPixmap())
            self.frame_label.setText(f"Couldn't render this frame:\n{payload}")
        self.stack.setCurrentWidget(self.frame_label)

    # ------------------------------------------------------------------
    # Next-clip preloading (F10a)
    # ------------------------------------------------------------------
    def upcoming_clip(self) -> Optional[Clip]:
        """The clip the preview will switch to at the end of the active one (None if none / a gap)."""
        clip = self._active_clip
        if clip is None:
            return None
        nxt, _track = self._resolve_active_clip(clip.timeline_end + 0.001)
        if nxt is None or nxt.id == clip.id or nxt.media_type not in (MediaType.VIDEO, MediaType.AUDIO):
            return None
        return nxt

    def maybe_preload(self) -> Optional[str]:
        """Open the next clip's file in the spare player when the cut is near; returns the preloaded path."""
        clip = self.upcoming_clip()
        if clip is None or self._active_clip is None:
            return None
        if self._active_clip.timeline_end - self._current_time > PRELOAD_LEAD_S:
            return None
        path = self.media_path_for(clip)
        if path == self._current_source or path == self._preloaded_source:
            return self._preloaded_source
        self._preload_player.setSource(QUrl.fromLocalFile(path))
        self._preload_player.setPosition(int(max(0.0, clip.source_in) * 1000))
        self._preload_player.pause()
        self._preloaded_source = path
        return path

    def _swap_players(self) -> None:
        """Make the preloaded player the active one (it already has the next file open)."""
        self.player.pause()
        self.player, self._preload_player = self._preload_player, self.player
        self.audio_output, self._preload_audio = self._preload_audio, self.audio_output
        self.video_widget, self._preload_video = self._preload_video, self.video_widget
        self._preload_audio.setMuted(True)
        self._current_source, self._preloaded_source = self._preloaded_source, None

    def _load_for_time(self, t: float, autoplay: bool) -> None:
        clip, track = self._resolve_active_clip(t)
        self._active_clip = clip
        self._loaded_signature = self._signature(t)
        if self._shuttle_rate < 0:
            autoplay = False  # reverse shuttle steps by seeking; the player itself stays paused

        if clip is None:
            self.player.stop()
            self.stack.setCurrentWidget(self.placeholder_label)
            self.placeholder_label.setText("No clip at playhead")
            self._current_source = None
            return

        if clip.media_type == MediaType.TEXT:
            self.player.stop()
            self.stack.setCurrentWidget(self.placeholder_label)
            style = clip.text_style
            text = style.content if style else "Text"
            self.placeholder_label.setText(text)
            self._current_source = None
            return

        if clip.media_type == MediaType.IMAGE:
            self.player.stop()
            pixmap = QPixmap(clip.source_path)
            if not pixmap.isNull():
                self.stack.setCurrentWidget(self.placeholder_label)
                self.placeholder_label.setPixmap(
                    pixmap.scaled(self.placeholder_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
            self._current_source = None
            return

        # Video or audio clip: play through QMediaPlayer.
        self.stack.setCurrentWidget(self.video_widget if clip.media_type == MediaType.VIDEO else self.placeholder_label)
        if clip.media_type == MediaType.AUDIO:
            self.placeholder_label.setText(clip.name)

        # Timeline <-> source mapping through the clip's speed segments, so
        # speed-ramped clips seek to (and play at) the right place/speed;
        # reverse and the freeze frame are honoured too (Clip.source_time_at).
        offset_in_clip = t - clip.timeline_start
        source_position_s = clip.source_time_at(offset_in_clip)
        if self._is_stepped(clip, offset_in_clip):
            # QMediaPlayer can only play forwards: a reversed clip, and a
            # freeze frame, are shown by seeking frame by frame from the poll
            # timer (wall-clock time) with the player paused -- so they play
            # silently here; the export reverses the sound too.
            autoplay = False

        media_path = self.media_path_for(clip)
        if self._current_source != media_path:
            if media_path == self._preloaded_source:
                self._swap_players()
                self.stack.setCurrentWidget(
                    self.video_widget if clip.media_type == MediaType.VIDEO else self.placeholder_label
                )
            else:
                self.player.setSource(QUrl.fromLocalFile(media_path))
                self._current_source = media_path
        self.player.setPlaybackRate(clip.speed_at(offset_in_clip) * max(1.0, self._shuttle_rate))
        self.player.setPosition(int(max(0.0, source_position_s) * 1000))
        # A muted *track* silences its clips too (matching export, see
        # core/filter_graph.py); this used to honor only the clip's own flag.
        self._apply_output_level(clip, track)

        if autoplay:
            self.player.play()
        else:
            self.player.pause()
            # Not while playing a stepped (reversed / frozen) clip: the
            # exact-frame still would hide the frames being stepped through.
            if self.accurate_checkbox.isChecked() and not self._is_playing:
                self.stack.setCurrentWidget(self.frame_label)

    # ------------------------------------------------------------------
    def _on_poll(self) -> None:
        if not self._is_playing:
            return

        step = POLL_INTERVAL_MS / 1000.0 * abs(self._shuttle_rate)
        if self._shuttle_rate < 0:
            target = self._current_time - step
            if target <= 0.0:
                self._current_time = 0.0
                self._load_for_time(0.0, autoplay=False)
                self.pause()
                self._shuttle_rate = 1.0
            else:
                self._current_time = target
                self._load_for_time(target, autoplay=False)
            self._update_time_label()
            self._update_slider()
            self.time_changed.emit(self._current_time)
            return

        clip = self._active_clip
        if clip is None:
            # Playing through a gap with no active clip (e.g. between two
            # clips on the timeline): advance by wall-clock time and keep
            # checking whether a clip has started at the new position.
            self._advance_time_and_refresh(self._current_time + step)
            return

        if clip.media_type in (MediaType.VIDEO, MediaType.AUDIO) and self._is_stepped(
            clip, self._current_time - clip.timeline_start
        ):
            timeline_t = self._current_time + step
            if timeline_t >= clip.timeline_end - 0.02:
                self._advance_time_and_refresh(clip.timeline_end + 0.001)
                return
            self._load_for_time(timeline_t, autoplay=False)
            self.maybe_preload()
        elif clip.media_type in (MediaType.VIDEO, MediaType.AUDIO):
            position_s = self.player.position() / 1000.0
            local_t = clip.timeline_offset_at(position_s - clip.source_in)
            timeline_t = clip.timeline_start + local_t
            if position_s >= clip.source_out - 0.02:
                if clip.freeze_frame_duration > 0:
                    # Footage done: hold the last frame for the freeze (stepped from now on).
                    self._current_time = clip.timeline_start + clip.playable_duration
                    self._load_for_time(self._current_time, autoplay=False)
                    self._update_time_label()
                    self._update_slider()
                    self.time_changed.emit(self._current_time)
                    self._update_audio_meters()
                    return
                self._advance_time_and_refresh(clip.timeline_end + 0.001)
                return
            # Crossing a speed-ramp boundary: follow the new segment's speed.
            rate = clip.speed_at(local_t) * max(1.0, self._shuttle_rate)
            if abs(self.player.playbackRate() - rate) > 1e-6:
                self.player.setPlaybackRate(rate)
            self._current_time = timeline_t
            self.maybe_preload()
        else:
            # Text/image clips have no player position to poll; advance by
            # wall-clock time instead, and switch clips once we pass this
            # one's own end (mirrors the video/audio branch above).
            timeline_t = self._current_time + step
            if timeline_t >= clip.timeline_end - 0.02:
                self._advance_time_and_refresh(clip.timeline_end + 0.001)
                return

        self._current_time = timeline_t
        self._update_time_label()
        self._update_slider()
        self.time_changed.emit(timeline_t)
        self._update_audio_meters()

        if timeline_t >= self._total_duration():
            self.pause()

    def _advance_time_and_refresh(self, next_t: float) -> None:
        """Move the playhead to ``next_t`` and (re)resolve the active clip.

        Used both when a clip finishes playing and when we're advancing
        through a gap with nothing to show, so playback never gets stuck.
        """
        if next_t >= self._total_duration():
            self._current_time = self._total_duration()
            self.pause()
            self._update_time_label()
            self._update_slider()
            self.time_changed.emit(self._current_time)
            return
        self._current_time = next_t
        self._load_for_time(next_t, autoplay=True)
        self._update_time_label()
        self._update_slider()
        self.time_changed.emit(next_t)
        self._update_audio_meters()

    # ------------------------------------------------------------------
    @property
    def current_time(self) -> float:
        """The current preview playhead position, in seconds."""
        return self._current_time

    def _fps(self) -> float:
        return self.project.settings.fps if self.project is not None else 30.0

    def _update_time_label(self) -> None:
        fps = self._fps()
        self.time_label.setText(
            f"{format_timecode(self._current_time, fps)} / {format_timecode(self._total_duration(), fps)}"
        )

    def _update_slider(self) -> None:
        total = self._total_duration()
        self._suppress_slider_signal = True
        self.seek_slider.setValue(int((self._current_time / total) * 1000) if total > 0 else 0)
        self._suppress_slider_signal = False

    @staticmethod
    def format_time(seconds: float) -> str:
        seconds = max(0.0, seconds)
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _on_player_error(self, error, error_string: str) -> None:  # noqa: ARG002
        if error_string:
            self.placeholder_label.setText(f"Playback error: {error_string}")
            self.stack.setCurrentWidget(self.placeholder_label)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.safe_overlay.setGeometry(self.stack.rect())
        self.safe_overlay.raise_()
        self.transform_overlay.setGeometry(self.stack.rect())
        self.transform_overlay.raise_()

    # ------------------------------------------------------------------
    # On-canvas transform (F15)
    # ------------------------------------------------------------------
    def set_transform_target(self, target: Optional[dict]) -> None:
        """The clip the Transform overlay edits: {clip_id, x, y, scale, rotation, source_size,
        movable} (see MainWindow._transform_target), or None."""
        canvas = (self.project.settings.width, self.project.settings.height) if self.project else (1920, 1080)
        self.transform_overlay.set_target(target, canvas)
        self._update_transform_overlay()

    def _update_transform_overlay(self, *_args) -> None:
        show = self.transform_checkbox.isChecked() and self.transform_overlay.target is not None
        self.transform_overlay.setGeometry(self.stack.rect())
        self.transform_overlay.setVisible(show)
        if show:
            self.transform_overlay.raise_()

    @staticmethod
    def _is_stepped(clip: Clip, offset_in_clip: float) -> bool:
        """Whether the preview shows ``clip`` here by seeking per poll tick instead of playing it."""
        return clip.reverse or clip.in_freeze_frame(offset_in_clip)

    def _waveform_for_active_clip(self):
        """The active clip's cached waveform levels (loaded once per cache file; None = no audio data)."""
        clip = self._active_clip
        if self.project is None or clip is None:
            return None
        asset = self.project.asset_for_clip(clip)
        if asset is None or not asset.waveform_path:
            return None  # not cached: the waveform may still arrive from the import worker
        path = asset.waveform_path
        if path not in self._waveform_cache:
            self._waveform_cache[path] = load_waveform(path)
        return self._waveform_cache[path]

    def _update_audio_meters(self) -> None:
        """Peak/RMS meters for the sound under the playhead, read from the cached waveform.

        No decoding: a slice of about a dozen cached values per call (see
        core.waveform.window_levels), so it is cheap enough for every poll
        tick. It shows the active clip only (the one the preview plays), and
        drops to zero for silence: no clip, text/image, muted clip or track,
        a freeze frame, or media without audio.
        """
        clip = self._active_clip
        offset = self._current_time - clip.timeline_start if clip is not None else 0.0
        track = self.project.find_clip_track(clip.id) if (clip is not None and self.project is not None) else None
        if (
            clip is None
            or clip.media_type not in (MediaType.VIDEO, MediaType.AUDIO)
            or clip.is_silent
            or (track is not None and not track_audible(track, self.project.tracks))
            or clip.in_freeze_frame(offset)
        ):
            self.meters.set_levels(0.0, 0.0)
            self._update_loudness(None, 0.0, 0.0)
            return
        gain = self._clip_gain_at(clip, offset)
        self._update_loudness(clip, clip.source_time_at(offset), gain)
        waveform = self._waveform_for_active_clip()
        if waveform is None:
            self.meters.set_levels(0.0, 0.0)
            return
        peak, rms = window_levels(waveform, clip.source_time_at(offset))
        if track is not None:
            gain *= track_meter_gain(track)
        self.meters.set_levels(peak * gain, rms * gain)

    def _apply_output_level(self, clip: Clip, track) -> None:
        """Mute/volume of the preview's audio output: the clip's own, times its track's mixer fader
        (F31), silenced by mute or by another track's solo -- as the export mixes it."""
        audible = track is None or self.project is None or track_audible(track, self.project.tracks)
        self.audio_output.setMuted(clip.muted or not audible)
        fader = track_meter_gain(track) if track is not None else 1.0
        self.audio_output.setVolume(max(0.0, min(1.0, clip.volume * fader)))

    @staticmethod
    def _clip_gain_at(clip: Clip, offset: float) -> float:
        """The clip's gain at ``offset`` seconds into it: its volume automation
        envelope when it has one (what the export applies), else its volume."""
        if clip.has_volume_automation:
            from core.clip import interpolate_keyframes

            return max(0.0, interpolate_keyframes(clip.volume_keyframes, offset, clip.easing_for("volume"))[0])
        return max(0.0, clip.volume)

    # -- live loudness (F15) ---------------------------------------------------
    def _loudness_for_clip(self, clip: Clip):
        """The cached measurement of ``clip``'s source, or None; starts measuring it
        in the background the first time (the meter fills in when it's done)."""
        if self.project is None:
            return None
        asset = self.project.asset_for_clip(clip)
        if asset is None or not asset.path or not asset.has_audio or not os.path.isfile(asset.path):
            return None
        path = asset.path
        if path in self._loudness_cache:
            return self._loudness_cache[path]
        cache_file = loudness_cache_path(path, self.loudness_cache_dir)
        if os.path.exists(cache_file):
            self._loudness_cache[path] = load_loudness(cache_file)
            return self._loudness_cache[path]
        if path not in self._loudness_jobs:
            self._loudness_jobs.add(path)
            duration = getattr(asset, "duration", 0.0) or 0.0
            threading.Thread(
                target=self._measure_loudness, args=(path, duration), name="loudness", daemon=True
            ).start()
        return None

    def _measure_loudness(self, path: str, duration: float) -> None:
        try:
            result = generate_loudness(path, self.loudness_cache_dir, duration)
        except Exception:  # noqa: BLE001 -- no meter for this file, never a crash
            result = None
        self._loudness_ready.emit(path, result)

    def _on_loudness_ready(self, path: str, cache_file) -> None:
        self._loudness_jobs.discard(path)
        self._loudness_cache[path] = load_loudness(cache_file) if cache_file else None
        self._update_audio_meters()

    def _update_loudness(self, clip: Optional[Clip], source_time: float, gain: float) -> None:
        momentary = short_term = None
        data = self._loudness_for_clip(clip) if clip is not None else None
        if data is not None:
            momentary, short_term = loudness_at(data, source_time, gain)
        if self._is_playing:
            self._integrated.add(self._current_time, momentary)
        self._show_loudness(momentary, short_term)

    def _show_loudness(self, momentary: Optional[float], short_term: Optional[float]) -> None:
        integrated = self._integrated.value()
        self._last_loudness = (momentary, short_term, integrated)
        self.loudness_label.setText(
            f"M {format_lufs(momentary)}  S {format_lufs(short_term)}  I {format_lufs(integrated)} LUFS"
        )

    def loudness_reading(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """``(momentary, short_term, integrated)`` as last shown (tests, accessibility)."""
        return self._last_loudness


class _SafeAreaOverlay(QWidget):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)

    def paintEvent(self, event) -> None:  # noqa: ARG002
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        outer = self.rect().adjusted(6, 6, -6, -6)
        title = outer.adjusted(int(outer.width() * 0.1), int(outer.height() * 0.1), -int(outer.width() * 0.1), -int(outer.height() * 0.1))
        action = outer.adjusted(int(outer.width() * 0.05), int(outer.height() * 0.05), -int(outer.width() * 0.05), -int(outer.height() * 0.05))
        painter.setPen(QPen(QColor(255, 255, 255, 120), 1, Qt.DashLine))
        painter.drawRect(action)
        painter.setPen(QPen(QColor(255, 215, 0, 150), 1, Qt.DashLine))
        painter.drawRect(title)


class _TransformOverlay(QWidget):
    """Draws the target clip's box where the export places it and turns drags into new
    Transform values: inside the box moves it, a corner handle scales it (uniformly)."""

    edited = Signal(str, float, float, float)
    HANDLE = 7.0

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setMouseTracking(True)
        self.target: Optional[dict] = None
        self.canvas = (1920, 1080)
        self._drag: Optional[dict] = None

    def set_target(self, target: Optional[dict], canvas: Tuple[int, int]) -> None:
        if self._drag is not None:
            return  # a drag in progress keeps its own copy
        self.target = dict(target) if target else None
        self.canvas = canvas
        self.update()

    # -- mapping between widget and canvas pixels (the preview shows the canvas fitted, centred)
    def _canvas_rect(self) -> QRectF:
        w, h = self.canvas
        scale = min(self.width() / max(1, w), self.height() / max(1, h))
        return QRectF((self.width() - w * scale) / 2, (self.height() - h * scale) / 2, w * scale, h * scale)

    def _to_canvas(self, point) -> Tuple[float, float]:
        rect = self._canvas_rect()
        factor = self.canvas[0] / max(1e-6, rect.width())
        return (point.x() - rect.x()) * factor, (point.y() - rect.y()) * factor

    def _to_widget(self, x: float, y: float) -> QPointF:
        rect = self._canvas_rect()
        factor = rect.width() / max(1, self.canvas[0])
        return QPointF(rect.x() + x * factor, rect.y() + y * factor)

    def _box(self):
        t = self.target
        return clip_box(t["x"], t["y"], t["scale"], t["rotation"], t["source_size"], self.canvas)

    def _corner_at(self, pos) -> Optional[int]:
        for index, (x, y) in enumerate(self._box().corners()):
            corner = self._to_widget(x, y)
            if abs(corner.x() - pos.x()) <= self.HANDLE + 2 and abs(corner.y() - pos.y()) <= self.HANDLE + 2:
                return index
        return None

    def paintEvent(self, event) -> None:  # noqa: ARG002
        if self.target is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        corners = [self._to_widget(x, y) for x, y in self._box().corners()]
        painter.setPen(QPen(QColor(theme.COLOR_ACCENT), 2, Qt.SolidLine if self.target.get("movable", True) else Qt.DashLine))
        painter.drawPolygon(QPolygonF(corners))
        painter.setBrush(QColor(theme.COLOR_ACCENT))
        for corner in corners:
            painter.drawRect(QRectF(corner.x() - self.HANDLE / 2, corner.y() - self.HANDLE / 2, self.HANDLE, self.HANDLE))

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        if self._drag is None:
            if self.target is None:
                return
            if self._corner_at(pos) is not None:
                self.setCursor(Qt.SizeFDiagCursor)
            elif self.target.get("movable", True) and self._box().contains(*self._to_canvas(pos)):
                self.setCursor(Qt.SizeAllCursor)
            else:
                self.unsetCursor()
            return
        drag, start = self._drag, self._drag["start"]
        now = self._to_canvas(pos)
        if drag["mode"] == "move":
            self.target["x"], self.target["y"] = moved(drag["x"], drag["y"], now[0] - start[0], now[1] - start[1], self.canvas)
        else:
            self.target["scale"] = scaled(drag["box"], drag["scale"], start, now)
        self.update()

    def mousePressEvent(self, event) -> None:
        if self.target is None or event.button() != Qt.LeftButton:
            return
        pos = event.position()
        mode = "scale" if self._corner_at(pos) is not None else None
        if mode is None and self.target.get("movable", True) and self._box().contains(*self._to_canvas(pos)):
            mode = "move"
        if mode is None:
            return
        self._drag = {"mode": mode, "start": self._to_canvas(pos), "x": self.target["x"], "y": self.target["y"],
                      "scale": self.target["scale"], "box": self._box()}

    def mouseReleaseEvent(self, event) -> None:  # noqa: ARG002
        if self._drag is None:
            return
        drag, self._drag = self._drag, None
        t = self.target
        if (t["x"], t["y"], t["scale"]) != (drag["x"], drag["y"], drag["scale"]):
            self.edited.emit(t["clip_id"], t["x"], t["y"], t["scale"])


class _AudioLevelMeters(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._peak = 0.0
        self._rms = 0.0
        self.setFixedSize(70, 18)
        self.setToolTip("Audio level meters (peak / RMS)")

    def set_levels(self, peak: float, rms: float) -> None:
        peak, rms = max(0.0, min(1.0, peak)), max(0.0, min(1.0, rms))
        if (peak, rms) == (self._peak, self._rms):
            return  # nothing to repaint (silence stays silence)
        self._peak, self._rms = peak, rms
        self.update()

    def levels(self) -> Tuple[float, float]:
        """``(peak, rms)`` currently shown, 0..1."""
        return self._peak, self._rms

    def paintEvent(self, event) -> None:  # noqa: ARG002
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.COLOR_BG_DARK))
        peak_w = int((self.width() - 2) * self._peak)
        rms_w = int((self.width() - 2) * self._rms)
        painter.fillRect(1, 2, peak_w, 6, QColor("#ef4444"))
        painter.fillRect(1, 10, rms_w, 6, QColor("#22c55e"))
        painter.setPen(QColor(theme.COLOR_BORDER))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
