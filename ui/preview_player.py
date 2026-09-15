"""Playback preview panel.

The preview plays the topmost active video clip (falling back to an
audio-only clip when no video is present at the current time) using
``QMediaPlayer``. Full multi-track alpha compositing, transitions and text
overlays are computed only at export time by the FFmpeg render engine — the
preview intentionally trades perfect What-You-See-Is-What-You-Get accuracy
for a simple, fast, maintainable implementation. A small on-screen note
reminds the user of this.
"""

from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
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
from core.project import Project
from core.track import Track, TrackType

POLL_INTERVAL_MS = 40


class PreviewPlayer(QWidget):
    """Video/audio preview synced to the project timeline."""

    time_changed = Signal(float)
    playing_changed = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project: Optional[Project] = None
        self._active_clip: Optional[Clip] = None
        self._current_source: Optional[str] = None
        self._is_playing = False
        self._current_time = 0.0
        self._suppress_slider_signal = False

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)

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
        self.placeholder_label = QLabel()
        self.placeholder_label.setAlignment(Qt.AlignCenter)
        self.placeholder_label.setStyleSheet("background-color: black; color: white; font-size: 24px;")
        self.stack.addWidget(self.video_widget)
        self.stack.addWidget(self.placeholder_label)
        layout.addWidget(self.stack, 1)

        controls = QHBoxLayout()
        self.play_btn = QPushButton()
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_btn.setFixedWidth(36)
        controls.addWidget(self.play_btn)

        self.time_label = QLabel("00:00:00 / 00:00:00")
        self.time_label.setStyleSheet("color: #9aa1ad;")
        controls.addWidget(self.time_label)

        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setMinimum(0)
        self.seek_slider.setMaximum(1000)
        controls.addWidget(self.seek_slider, 1)

        layout.addLayout(controls)

        self.note_label = QLabel(
            "Preview shows the top active video clip only. Transitions, text and full "
            "multi-track compositing appear in the exported video."
        )
        self.note_label.setWordWrap(True)
        self.note_label.setStyleSheet("color: #6b7280; font-size: 10px;")
        layout.addWidget(self.note_label)

    def _connect_signals(self) -> None:
        self.play_btn.clicked.connect(self.toggle_play)
        self.seek_slider.sliderPressed.connect(self._pause_for_scrub)
        self.seek_slider.sliderMoved.connect(self._on_slider_moved)
        self.player.errorOccurred.connect(self._on_player_error)

    # ------------------------------------------------------------------
    def set_project(self, project: Project) -> None:
        self.project = project
        self._current_source = None
        self._active_clip = None
        self.seek_to(0.0)

    def _total_duration(self) -> float:
        return self.project.total_duration if self.project else 0.0

    # ------------------------------------------------------------------
    # Clip resolution
    # ------------------------------------------------------------------
    def _resolve_active_clip(self, t: float) -> Tuple[Optional[Clip], Optional[Track]]:
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
            self.play()

    def play(self) -> None:
        if self.project is None:
            return
        if self._current_time >= self._total_duration() - 0.01:
            self._current_time = 0.0
        self._load_for_time(self._current_time, autoplay=True)
        self._is_playing = True
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
        self._poll_timer.start()
        self.playing_changed.emit(True)

    def pause(self) -> None:
        self.player.pause()
        self._is_playing = False
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self._poll_timer.stop()
        self.playing_changed.emit(False)

    def _pause_for_scrub(self) -> None:
        if self._is_playing:
            self.pause()

    def _on_slider_moved(self, value: int) -> None:
        total = self._total_duration()
        t = (value / 1000.0) * total
        self.seek_to(t)

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

    def _load_for_time(self, t: float, autoplay: bool) -> None:
        clip, _track = self._resolve_active_clip(t)
        self._active_clip = clip

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

        offset_in_clip = t - clip.timeline_start
        source_position_s = clip.source_in + offset_in_clip * (clip.speed if clip.speed > 0 else 1.0)

        if self._current_source != clip.source_path:
            self.player.setSource(QUrl.fromLocalFile(clip.source_path))
            self._current_source = clip.source_path
        self.player.setPlaybackRate(clip.speed if clip.speed > 0 else 1.0)
        self.player.setPosition(int(max(0.0, source_position_s) * 1000))
        self.audio_output.setMuted(clip.muted)
        self.audio_output.setVolume(max(0.0, min(1.0, clip.volume)))

        if autoplay:
            self.player.play()
        else:
            self.player.pause()

    # ------------------------------------------------------------------
    def _on_poll(self) -> None:
        if not self._is_playing:
            return

        clip = self._active_clip
        if clip is None:
            # Playing through a gap with no active clip (e.g. between two
            # clips on the timeline): advance by wall-clock time and keep
            # checking whether a clip has started at the new position.
            self._advance_time_and_refresh(self._current_time + POLL_INTERVAL_MS / 1000.0)
            return

        speed = clip.speed if clip.speed > 0 else 1.0

        if clip.media_type in (MediaType.VIDEO, MediaType.AUDIO):
            position_s = self.player.position() / 1000.0
            timeline_t = clip.timeline_start + (position_s - clip.source_in) / speed
            if position_s >= clip.source_out - 0.02:
                self._advance_time_and_refresh(clip.timeline_end + 0.001)
                return
        else:
            # Text/image clips have no player position to poll; advance by
            # wall-clock time instead, and switch clips once we pass this
            # one's own end (mirrors the video/audio branch above).
            timeline_t = self._current_time + POLL_INTERVAL_MS / 1000.0
            if timeline_t >= clip.timeline_end - 0.02:
                self._advance_time_and_refresh(clip.timeline_end + 0.001)
                return

        self._current_time = timeline_t
        self._update_time_label()
        self._update_slider()
        self.time_changed.emit(timeline_t)

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

    # ------------------------------------------------------------------
    @property
    def current_time(self) -> float:
        """The current preview playhead position, in seconds."""
        return self._current_time

    def _update_time_label(self) -> None:
        self.time_label.setText(f"{self.format_time(self._current_time)} / {self.format_time(self._total_duration())}")

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
