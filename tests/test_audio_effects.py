"""Per-clip audio effects (F26): noise gate, parametric EQ and compressor -- the filter chain, the
project round trip, the export graph, the Properties panel, and (with FFmpeg) what they do to sound."""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
import unittest
from array import array

from core import audio_effects
from core.clip import Clip, MediaType
from core.filter_graph import build_filter_graph
from core.project import Project, ProjectSettings
from core.track import TrackType
from tests.integration import test_p2_real_ffmpeg as p2

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from core.history import History
    from ui.properties_panel import PropertiesPanel

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover - PySide6 missing
    _PYSIDE6_AVAILABLE = False


def _audio_clip(**fields):
    return Clip(media_type=MediaType.AUDIO, source_path="/media/voice.wav", source_in=0, source_out=6, timeline_start=0, **fields)


class FilterChainTests(unittest.TestCase):
    def test_everything_is_off_by_default(self):
        clip = _audio_clip()
        self.assertEqual(audio_effects.audio_effect_filters(clip), "")
        self.assertFalse(clip.has_audio_effects)

    def test_chain_order_and_values(self):
        clip = _audio_clip(
            gate_enabled=True, gate_threshold_db=-40.0, gate_range_db=20.0,
            eq_enabled=True, eq_low_gain_db=3.0, eq_mid_gain_db=-4.5, eq_mid_freq=2500.0, eq_mid_q=2.0,
            comp_enabled=True, comp_threshold_db=-20.0, comp_ratio=3.0, comp_makeup_db=6.0,
        )
        chain = audio_effects.audio_effect_filters(clip)
        names = [part.split("=")[0] for part in chain.strip(",").split(",")]
        self.assertEqual(names, ["agate", "lowshelf", "equalizer", "acompressor"])  # 0 dB high shelf left out
        self.assertIn("agate=threshold=0.010000:range=0.100000", chain)
        self.assertIn("lowshelf=f=120.0:g=3.00", chain)
        self.assertIn("equalizer=f=2500.0:t=q:w=2.000:g=-4.50", chain)
        self.assertIn("acompressor=threshold=0.100000:ratio=3.00:attack=20.00:release=250.00:makeup=1.9953", chain)

    def test_neutral_settings_emit_nothing_even_when_enabled(self):
        clip = _audio_clip(gate_enabled=True, gate_range_db=0.0, eq_enabled=True, comp_enabled=True, comp_ratio=1.0)
        self.assertEqual(audio_effects.audio_effect_filters(clip), "")

    def test_round_trip_and_clamping(self):
        clip = _audio_clip(comp_enabled=True, comp_ratio=8.0, eq_enabled=True, eq_high_gain_db=-6.0)
        restored = Clip.from_dict(clip.to_dict())
        self.assertTrue(restored.comp_enabled and restored.eq_enabled)
        self.assertEqual((restored.comp_ratio, restored.eq_high_gain_db), (8.0, -6.0))
        data = clip.to_dict()
        data.update(comp_ratio=99, gate_enabled="yes", eq_mid_q=float("nan"))
        hostile = Clip.from_dict(data)
        self.assertEqual(hostile.comp_ratio, 20.0)
        self.assertIs(hostile.gate_enabled, False)
        self.assertEqual(hostile.eq_mid_q, 1.0)
        old = clip.to_dict()
        for name in audio_effects.FIELDS:
            old.pop(name)
        self.assertFalse(Clip.from_dict(old).has_audio_effects)  # an older project: all off

    def test_export_graph_applies_them_after_noise_reduction(self):
        project = Project()
        track = [t for t in project.tracks if t.track_type == TrackType.AUDIO][0]
        track.clips.append(_audio_clip(denoise_enabled=True, comp_enabled=True))
        graph = build_filter_graph(project, ProjectSettings())
        complex_ = graph.filter_complex
        self.assertIn("afftdn=nr=12.000,acompressor=", complex_)
        graph.cleanup()


@unittest.skipUnless(p2.FFMPEG, "FFmpeg not available")
class RealFfmpegTests(unittest.TestCase):
    """The chains are valid FFmpeg filters and do what they say on a generated tone."""

    def _rms(self, source_args, chain):
        command = [p2.FFMPEG, "-v", "error", *source_args]
        if chain:
            command += ["-af", chain.strip(",")]
        command += ["-ac", "1", "-ar", "8000", "-f", "f32le", "-"]
        samples = array("f", subprocess.run(command, check=True, capture_output=True).stdout)[800:]
        return math.sqrt(sum(value * value for value in samples) / len(samples))

    def test_gate_eq_and_compressor_change_the_level(self):
        quiet = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1,volume=0.005"]
        loud = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1,volume=0.9"]
        high = ["-f", "lavfi", "-i", "sine=frequency=3000:duration=1,volume=0.2"]
        gated = audio_effects.audio_effect_filters(_audio_clip(gate_enabled=True, gate_threshold_db=-30.0, gate_range_db=40.0))
        self.assertLess(self._rms(quiet, gated), self._rms(quiet, "") * 0.2)
        boosted = audio_effects.audio_effect_filters(_audio_clip(eq_enabled=True, eq_high_gain_db=12.0, eq_high_freq=1500.0))
        self.assertGreater(self._rms(high, boosted), self._rms(high, "") * 2.5)
        squeezed = audio_effects.audio_effect_filters(_audio_clip(comp_enabled=True, comp_threshold_db=-30.0, comp_ratio=10.0, comp_attack_ms=1.0))
        self.assertLess(self._rms(loud, squeezed), self._rms(loud, "") * 0.5)


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class PropertiesPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_audio_effects_group_edits_the_clip_with_undo(self):
        project = Project()
        track = [t for t in project.tracks if t.track_type == TrackType.AUDIO][0]
        clip = _audio_clip()
        track.clips.append(clip)
        history = History()
        panel = PropertiesPanel()
        self.addCleanup(panel.deleteLater)
        panel.set_project(project, history)
        panel.set_selected_clip(clip.id)
        self.assertFalse(panel.audio_fx_group.isHidden())
        panel.audio_fx_widgets["comp_enabled"].setChecked(True)
        self.assertTrue(clip.comp_enabled)
        spin = panel.audio_fx_widgets["comp_ratio"]
        spin.setValue(6.0)
        spin.editingFinished.emit()
        self.assertEqual(clip.comp_ratio, 6.0)
        history.undo()
        self.assertEqual(clip.comp_ratio, 4.0)
        history.undo()
        self.assertFalse(clip.comp_enabled)


if __name__ == "__main__":
    unittest.main()
