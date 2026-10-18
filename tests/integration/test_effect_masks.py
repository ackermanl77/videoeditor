"""Masks on effects (F16): geometry, the filter graph with the real FFmpeg, projects, the panel."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType
from core.effect_masks import clean_mask, mask_value, masked_filter
from core.effects import EffectInstance, effect_filters
from core.exporter import ExportSettings, Exporter
from core.frame_render import render_frame
from core.media_asset import MediaAsset
from core.project import Project, ProjectSettings
from tests.integration import test_p2_real_ffmpeg as p2

FFMPEG, FFPROBE = p2.FFMPEG, p2.FFPROBE
W, H = 160, 90
_TMP = None
GREY = None
ELLIPSE = {"shape": "ellipse", "cx": 0.5, "cy": 0.5, "w": 0.4, "h": 0.5, "feather": 0.02}


def setUpModule():
    global _TMP, GREY
    if not FFMPEG:
        return
    _TMP = tempfile.mkdtemp(prefix="ve_masks_")
    GREY = os.path.join(_TMP, "grey.mp4")
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=0x606060:s={W}x{H}:r=25:d=2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", GREY], check=True, capture_output=True)


def tearDownModule():
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)


class MaskGeometryTests(unittest.TestCase):
    def test_shapes_cover_inside_and_not_outside(self):
        rect = clean_mask({"shape": "rectangle", "cx": 0.5, "cy": 0.5, "w": 0.5, "h": 0.5, "feather": 0.01})
        self.assertEqual(mask_value(rect, 0.5, 0.5, 1.0), 1.0)
        self.assertEqual(mask_value(rect, 0.05, 0.05, 1.0), 0.0)
        self.assertEqual(mask_value({**rect, "invert": True}, 0.05, 0.05, 1.0), 1.0)
        ellipse = clean_mask(ELLIPSE)
        self.assertEqual(mask_value(ellipse, 0.5, 0.5, 16 / 9), 1.0)
        self.assertEqual(mask_value(ellipse, 0.5, 0.05, 16 / 9), 0.0)
        linear = clean_mask({"shape": "linear", "cx": 0.5, "cy": 0.5, "angle": 0, "feather": 0.1})
        self.assertGreater(mask_value(linear, 0.9, 0.5, 1.0), 0.99)  # the side the gradient points to
        self.assertLess(mask_value(linear, 0.1, 0.5, 1.0), 0.01)
        self.assertAlmostEqual(mask_value(linear, 0.5, 0.5, 1.0), 0.5)
        turned = clean_mask({"shape": "rectangle", "cx": 0.5, "cy": 0.5, "w": 0.8, "h": 0.1, "angle": 90, "feather": 0.01})
        self.assertEqual(mask_value(turned, 0.5, 0.15, 1.0), 1.0)  # rotated: now tall
        self.assertEqual(mask_value(turned, 0.15, 0.5, 1.0), 0.0)

    def test_bad_masks_are_cleaned_and_saved_with_the_effect(self):
        self.assertIsNone(clean_mask({"shape": "none"}))
        self.assertIsNone(clean_mask("ellipse"))
        self.assertEqual(clean_mask({"shape": "ellipse", "w": 99, "feather": "x"})["w"], 2.0)
        effect = EffectInstance.create("color_correction", brightness=0.4)
        effect.mask = clean_mask(ELLIPSE)
        again = EffectInstance.from_dict(effect.to_dict())
        self.assertEqual(again.mask, effect.mask)
        self.assertIsNone(EffectInstance.from_dict({"kind": "blur", "mask": {"shape": "triangle"}}).mask)

    def test_each_masked_effect_gets_its_own_labels(self):
        effect = EffectInstance.create("blur")
        effect.mask = clean_mask(ELLIPSE)
        a, b = effect_filters([effect], "post"), effect_filters([effect], "post")
        self.assertIn("maskedmerge", a)
        labels = lambda text: {part.split("]")[0] for part in text.split("[")[1:]}  # noqa: E731
        self.assertFalse(labels(a) & labels(b))  # a speed ramp emits one copy per segment
        self.assertNotIn("maskedmerge", effect_filters([EffectInstance.create("blur")], "post"))


def _luma(path, x, y):
    out = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-vf", f"crop=4:4:{x - 2}:{y - 2},scale=1:1,format=gray",
                          "-frames:v", "1", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return out[0]


def _project(effect, **clip_kw):
    asset = MediaAsset(path=GREY, duration=2.0, width=W, height=H, has_video=True, has_audio=False)
    clip = Clip(media_type=MediaType.VIDEO, source_path=GREY, source_in=0.0, source_out=2.0, timeline_start=0.0,
                source_duration=2.0, asset_id=asset.id, **clip_kw)
    clip.effects = [effect]
    project = Project()
    project.settings = ProjectSettings(width=W, height=H, fps=25.0)
    project.add_media_asset(asset)
    project.tracks[0].clips.append(clip)
    return project


def _export(project, name):
    out = os.path.join(_TMP, name)
    with patch("core.exporter.find_binary", return_value=FFMPEG):
        Exporter(project).export(ExportSettings(output_path=out, width=W, height=H, fps=25.0, use_gpu=False))
    return out


@unittest.skipUnless(FFMPEG, "FFmpeg not found")
class MaskedEffectExportTests(unittest.TestCase):
    def _brighten(self, mask):
        effect = EffectInstance.create("color_correction", brightness=0.4)
        effect.mask = clean_mask(mask) if mask else None
        return effect

    def test_a_masked_effect_changes_only_inside_its_mask(self):
        plain = _export(_project(self._brighten(None)), "plain.mp4")
        untouched = _export(_project(EffectInstance.create("color_correction")), "neutral.mp4")
        masked = _export(_project(self._brighten(ELLIPSE)), "masked.mp4")
        self.assertAlmostEqual(_luma(masked, W // 2, H // 2), _luma(plain, W // 2, H // 2), delta=3)  # inside: brightened
        self.assertAlmostEqual(_luma(masked, 8, 8), _luma(untouched, 8, 8), delta=3)  # outside: as it was
        self.assertGreater(_luma(masked, W // 2, H // 2), _luma(masked, 8, 8) + 40)
        inverted = _export(_project(self._brighten({**ELLIPSE, "invert": True})), "inverted.mp4")
        self.assertGreater(_luma(inverted, 8, 8), _luma(inverted, W // 2, H // 2) + 40)

    def test_pre_stage_effects_and_speed_ramps_and_the_preview(self):
        denoise = EffectInstance.create("denoise")
        denoise.mask = clean_mask(ELLIPSE)
        self.assertTrue(os.path.getsize(_export(_project(denoise), "pre.mp4")) > 0)  # a frame without alpha yet
        ramp = _project(self._brighten(ELLIPSE), speed_keyframes=[(0.0, 1.0), (1.0, 2.0)])
        ramped = _export(ramp, "ramp.mp4")
        self.assertGreater(_luma(ramped, W // 2, H // 2), _luma(ramped, 8, 8) + 40)
        with patch("core.frame_render.find_binary", return_value=FFMPEG):
            frame = render_frame(_project(self._brighten(ELLIPSE)), 0.5, (W, H))
        self.assertTrue(frame)  # the preview path builds the same graph


@unittest.skipUnless(FFMPEG, "FFmpeg not found")
class MaskPanelTests(unittest.TestCase):
    def test_the_panel_sets_and_clears_a_mask_as_undoable_edits(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), \
                patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
            window._autosave_timer.stop()
            try:
                project = _project(EffectInstance.create("blur"))
                clip = project.tracks[0].clips[0]
                window.project.add_media_asset(project.asset_for_clip(clip))
                window.project.tracks[0].clips.append(clip)
                window.timeline.rebuild()
                window.timeline.select_clips([clip.id])
                panel = window.properties
                panel.effects_list.setCurrentRow(0)
                self.assertTrue(panel.set_effect_mask(0, ELLIPSE))
                self.assertEqual(window.project.find_clip(clip.id).effects[0].mask["shape"], "ellipse")
                self.assertFalse(panel.set_effect_mask(0, ELLIPSE))  # unchanged: no history entry
                self.assertEqual(panel.effect_mask_editor.mask_controls["shape"].currentData(), "ellipse")
                window.history.undo()
                self.assertIsNone(window.project.find_clip(clip.id).effects[0].mask)
            finally:
                window._clear_autosave()
                window.deleteLater()
                QApplication.clipboard().clear()
                app.processEvents()
        shutil.rmtree(home, ignore_errors=True)
