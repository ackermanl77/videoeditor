"""Tests for the two timeline-UI affordances added for glanceability:

* ``ClipItem`` per-clip effect-indicator badges (Color Correction, Chroma
  Key, Denoise, Transition) -- see ``ui/widgets/timeline_items.py``'s
  ``EFFECT_INDICATORS``/``_active_effect_badges``.
* ``TrackHeaderItem``'s ducking toggle button, which mirrors the existing
  Mute/Lock/Hide ``ToggleButtonItem``s and reuses the same "Duck Audio
  Under..." picker the right-click context menu already opens.

Unlike the rest of ``tests/`` (which covers ``core/`` only and has zero
third-party imports, matching this project's zero-`pip install` CI), these
exercise ``ui/widgets`` and therefore need PySide6. They're skipped
entirely -- not failed -- when it isn't installed, so they never break the
dependency-free CI run described in docs/PROJECT_STATUS.md; run them
locally with a PySide6-equipped interpreter, e.g.
``py -3 -m unittest tests.test_timeline_items -v`` on this machine.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from core.clip import Clip, MediaType, TransitionType
    from core.history import History
    from core.project import Project
    from ui.widgets.timeline_items import ClipItem, TimelineSignals, TrackHeaderItem
    from ui.widgets.timeline_widget import TimelineWidget

    _PYSIDE6_AVAILABLE = True
except ImportError:
    _PYSIDE6_AVAILABLE = False


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class ClipItemEffectBadgeTests(unittest.TestCase):
    """``ClipItem.active_effect_labels()`` mirrors filter_graph's own
    "is this effect active" checks (via the shared ``Clip.has_*``
    properties), independent of any actual painting/display.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.signals = TimelineSignals()

    def _item(self, clip: Clip) -> ClipItem:
        return ClipItem(clip, row=0, pixels_per_second=40.0, signals=self.signals)

    def test_plain_clip_has_no_badges(self):
        clip = Clip(media_type=MediaType.VIDEO, source_in=0.0, source_out=5.0)
        self.assertEqual(self._item(clip).active_effect_labels(), [])

    def test_color_correction_badge(self):
        clip = Clip(media_type=MediaType.VIDEO, source_in=0.0, source_out=5.0, brightness=0.2)
        self.assertEqual(self._item(clip).active_effect_labels(), ["C"])

    def test_chroma_key_badge(self):
        clip = Clip(media_type=MediaType.VIDEO, source_in=0.0, source_out=5.0, chroma_key_enabled=True)
        self.assertEqual(self._item(clip).active_effect_labels(), ["K"])

    def test_denoise_badge_applies_to_audio_clips_too(self):
        clip = Clip(media_type=MediaType.AUDIO, source_in=0.0, source_out=5.0, denoise_enabled=True)
        self.assertEqual(self._item(clip).active_effect_labels(), ["N"])

    def test_transition_badge(self):
        clip = Clip(
            media_type=MediaType.VIDEO,
            source_in=0.0,
            source_out=5.0,
            transition_out=TransitionType.CROSSFADE,
        )
        self.assertEqual(self._item(clip).active_effect_labels(), ["T"])

    def test_all_four_effects_produce_badges_in_a_stable_order(self):
        clip = Clip(
            media_type=MediaType.VIDEO,
            source_in=0.0,
            source_out=5.0,
            brightness=0.3,
            chroma_key_enabled=True,
            denoise_enabled=True,
            transition_out=TransitionType.WIPE_LEFT,
        )
        self.assertEqual(self._item(clip).active_effect_labels(), ["C", "K", "N", "T"])

    def test_a_default_field_left_at_its_neutral_value_is_not_flagged(self):
        # Non-default gamma alone (contrast/saturation/brightness untouched)
        # should still count as "color correction active" -- exercising the
        # same tuple-equality check filter_graph.py uses, not a per-field one.
        clip = Clip(media_type=MediaType.VIDEO, source_in=0.0, source_out=5.0, gamma=1.4)
        self.assertEqual(self._item(clip).active_effect_labels(), ["C"])

    def test_set_source_clip_recomputes_badges(self):
        clip = Clip(media_type=MediaType.VIDEO, source_in=0.0, source_out=5.0)
        item = self._item(clip)
        self.assertEqual(item.active_effect_labels(), [])

        edited = Clip(media_type=MediaType.VIDEO, source_in=0.0, source_out=5.0, chroma_key_enabled=True)
        item.set_source_clip(edited)
        self.assertEqual(item.active_effect_labels(), ["K"])


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class TrackHeaderDuckButtonTests(unittest.TestCase):
    """Unit-level: the duck toggle button in isolation, no full timeline."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_duck_button_defaults_inactive(self):
        signals = TimelineSignals()
        header = TrackHeaderItem("t1", 0, "Music", "audio", signals)
        self.assertFalse(header.duck_btn.active)

    def test_clicking_duck_button_emits_duck_config_requested_for_its_own_row(self):
        signals = TimelineSignals()
        header = TrackHeaderItem("t1", 3, "Music", "audio", signals)
        received = []
        signals.track_duck_config_requested.connect(received.append)
        header.duck_btn.clicked.emit()
        self.assertEqual(received, [3])


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class DuckToggleIntegrationTests(unittest.TestCase):
    """Full ``TimelineWidget`` integration: the button reflects
    ``Track.duck_trigger_track_id`` and reuses the exact same
    ``QInputDialog`` picker code path as the right-click context menu
    (``TimelineWidget._on_track_duck_config_requested``).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _widget_with_project(self):
        project = Project(name="Ducking UI Test")
        video_track = project.tracks[0]
        audio_track = project.tracks[1]
        history = History()
        widget = TimelineWidget()
        widget.set_project(project, history)
        return widget, project, video_track, audio_track

    def test_header_duck_indicator_reflects_duck_trigger_track_id(self):
        widget, project, video_track, audio_track = self._widget_with_project()
        row = widget._row_tracks.index(audio_track)
        self.assertFalse(widget._header_items[row].duck_btn.active)

        audio_track.duck_trigger_track_id = video_track.id
        widget.rebuild()

        self.assertTrue(widget._header_items[row].duck_btn.active)

    @staticmethod
    def _fake_dialog(trigger_id, accepted=True, **params):
        """Stand-in for the modal DuckingDialog: answers with the real dialog's values plus overrides."""
        from PySide6.QtWidgets import QDialog

        from ui.widgets.timeline_widget import DuckingDialog

        calls = []

        def factory(track, others, parent):
            real = DuckingDialog(track, others, parent)
            calls.append(real)
            index = real.trigger_combo.findData(trigger_id)
            real.trigger_combo.setCurrentIndex(index)
            for name, value in params.items():
                getattr(real, f"{name}_spin").setValue(value)
            real.exec = lambda: QDialog.Accepted if accepted else QDialog.Rejected
            return real

        return factory, calls

    def test_clicking_duck_button_reuses_the_context_menus_picker(self):
        widget, project, video_track, audio_track = self._widget_with_project()
        row = widget._row_tracks.index(audio_track)
        header = widget._header_items[row]
        self.assertFalse(header.duck_btn.active)

        widget.ducking_dialog_factory, calls = self._fake_dialog(video_track.id, ratio=4.0, release=500.0)
        header.duck_btn.clicked.emit()

        self.assertEqual(len(calls), 1)
        self.assertEqual(audio_track.duck_trigger_track_id, video_track.id)
        # B5: the compressor settings are editable too, in the same undo step.
        self.assertEqual((audio_track.duck_ratio, audio_track.duck_release), (4.0, 500.0))
        widget.history.undo()
        self.assertIsNone(audio_track.duck_trigger_track_id)
        self.assertEqual(audio_track.duck_ratio, 8.0)
        widget.history.redo()
        widget.rebuild()
        # rebuild() (run by the same handler the context menu uses) replaces
        # header items, so re-fetch this row's header for the fresh state.
        self.assertTrue(widget._header_items[row].duck_btn.active)

    def test_choosing_none_clears_ducking_and_deactivates_the_indicator(self):
        widget, project, video_track, audio_track = self._widget_with_project()
        audio_track.duck_trigger_track_id = video_track.id
        widget.rebuild()
        row = widget._row_tracks.index(audio_track)
        header = widget._header_items[row]
        self.assertTrue(header.duck_btn.active)

        widget.ducking_dialog_factory, _calls = self._fake_dialog(None)
        header.duck_btn.clicked.emit()

        self.assertIsNone(audio_track.duck_trigger_track_id)
        self.assertFalse(widget._header_items[row].duck_btn.active)

    def test_cancelled_dialog_changes_nothing(self):
        widget, project, video_track, audio_track = self._widget_with_project()
        widget.ducking_dialog_factory, _calls = self._fake_dialog(video_track.id, accepted=False)
        widget._on_track_duck_config_requested(widget._row_tracks.index(audio_track))
        self.assertIsNone(audio_track.duck_trigger_track_id)
        self.assertFalse(widget.history.can_undo)


if __name__ == "__main__":
    unittest.main()
