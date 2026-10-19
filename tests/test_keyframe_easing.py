"""Keyframe easing (F25): the curves, the FFmpeg expressions (evaluated by FFmpeg itself and
compared with core.clip.interpolate_keyframes), the project round trip, splits, and the panel."""

from __future__ import annotations

import os
import subprocess
import unittest
from array import array

from core.clip import EASINGS, Clip, MediaType, clean_easing, ease, interpolate_keyframes
from core.commands import SplitClipCommand
from core.filter_graph import _piecewise_linear_expr, build_filter_graph
from core.project import Project, ProjectSettings
from tests.integration import test_p2_real_ffmpeg as p2

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from core.history import History
    from ui.properties_panel import PropertiesPanel

    _PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover - PySide6 missing
    _PYSIDE6_AVAILABLE = False

KEYS = [(0.5, 0.0), (1.5, 1.0), (3.0, 0.25)]


class EasingCurveTests(unittest.TestCase):
    def test_curves_meet_the_keyframes_and_differ_in_between(self):
        for easing in EASINGS:
            self.assertEqual(ease(0.0, easing), 0.0, easing)
            if easing != "hold":
                self.assertAlmostEqual(ease(1.0, easing), 1.0, msg=easing)
        self.assertEqual(ease(0.5, "linear"), 0.5)
        self.assertEqual(ease(0.5, "ease_in"), 0.25)
        self.assertEqual(ease(0.5, "ease_out"), 0.75)
        self.assertEqual(ease(0.5, "ease_in_out"), 0.5)
        self.assertLess(ease(0.25, "ease_in_out"), 0.25)
        self.assertEqual(interpolate_keyframes(KEYS, 1.0, "hold"), (0.0,))
        self.assertEqual(interpolate_keyframes(KEYS, 1.0, "ease_in"), (0.25,))
        self.assertEqual(interpolate_keyframes(KEYS, 9.0, "ease_out"), (0.25,))  # held after the last key

    def test_clean_easing_and_round_trip(self):
        self.assertEqual(clean_easing({"opacity": "ease_in", "volume": "linear", "bogus": "hold", "position": "wobble"}),
                         {"opacity": "ease_in"})
        self.assertEqual(clean_easing("x"), {})
        clip = Clip(media_type=MediaType.VIDEO, source_in=0, source_out=4, keyframe_easing={"position": "ease_in_out"})
        restored = Clip.from_dict(clip.to_dict())
        self.assertEqual(restored.easing_for("position"), "ease_in_out")
        self.assertEqual(restored.easing_for("opacity"), "linear")
        old = clip.to_dict()
        old.pop("keyframe_easing")
        self.assertEqual(Clip.from_dict(old).keyframe_easing, {})


@unittest.skipUnless(p2.FFMPEG, "FFmpeg not available")
class ExpressionTests(unittest.TestCase):
    """FFmpeg evaluates the generated expression (aevalsrc, 100 samples a second)."""

    def _samples(self, expr: str):
        command = [p2.FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s=100:d=3.5", "-f", "f32le", "-"]
        return array("f", subprocess.run(command, check=True, capture_output=True).stdout)

    def test_every_easing_matches_the_python_curve(self):
        for easing in EASINGS:
            samples = self._samples(_piecewise_linear_expr(KEYS, "t", easing))
            for index in range(0, len(samples), 7):
                expected = interpolate_keyframes(KEYS, index / 100.0, easing)[0]
                self.assertAlmostEqual(samples[index], expected, places=4, msg=f"{easing} at {index / 100.0}s")


class GraphAndSplitTests(unittest.TestCase):
    def _video_project(self, easing):
        from core.media_asset import MediaAsset

        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        clip = Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=4, timeline_start=0,
                    position_keyframes=[(0.0, 0.2, 0.5), (4.0, 0.8, 0.5)], opacity_keyframes=[(0.0, 0.0), (2.0, 1.0)],
                    keyframe_easing=easing)
        project.tracks[0].clips.append(clip)
        return project, clip

    def test_the_export_graph_carries_the_easing(self):
        project, _clip = self._video_project({"position": "ease_in_out", "opacity": "hold"})
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("*(3-2*", graph.filter_complex)  # smoothstep in the overlay's x expression
        self.assertIn("alpha(X,Y)*(if(lt(T,0.000000),0.000000,if(lt(T,2.000000),0.000000,", graph.filter_complex)
        graph.cleanup()

    def test_a_split_keeps_the_value_at_the_cut(self):
        project, clip = self._video_project({"position": "ease_in"})
        before = interpolate_keyframes(clip.position_keyframes, 1.0, "ease_in")
        SplitClipCommand(project, clip.id, 1.0).do()
        first, second = sorted(project.tracks[0].clips, key=lambda c: c.timeline_start)
        self.assertEqual(second.easing_for("position"), "ease_in")
        self.assertAlmostEqual(first.position_keyframes[-1][1], before[0])
        self.assertAlmostEqual(second.position_keyframes[0][1], before[0])


@unittest.skipUnless(_PYSIDE6_AVAILABLE, "PySide6 is not installed in this interpreter")
class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_easing_combo_edits_the_clip_with_undo(self):
        project = Project()
        clip = Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=4, timeline_start=0)
        project.tracks[0].clips.append(clip)
        history = History()
        panel = PropertiesPanel()
        self.addCleanup(panel.deleteLater)
        panel.set_project(project, history)
        panel.set_selected_clip(clip.id)
        combo = panel.easing_combos["position"]
        combo.setCurrentIndex(combo.findData("ease_out"))
        self.assertEqual(clip.keyframe_easing, {"position": "ease_out"})
        panel.easing_combos["volume"].setCurrentIndex(panel.easing_combos["volume"].findData("hold"))
        self.assertEqual(clip.keyframe_easing, {"position": "ease_out", "volume": "hold"})
        history.undo()
        self.assertEqual(clip.keyframe_easing, {"position": "ease_out"})
        panel.set_selected_clip(clip.id)
        self.assertEqual(panel.easing_combos["position"].currentData(), "ease_out")


if __name__ == "__main__":
    unittest.main()
