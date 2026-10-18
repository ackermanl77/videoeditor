"""Title styles (F23)."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType, TextStyle
from core.title_templates import (
    BUILTIN_STYLES,
    all_styles,
    apply_style,
    delete_user_style,
    load_user_styles,
    save_user_style,
    style_of,
)


class TitleStyleTests(unittest.TestCase):
    def test_applying_scales_to_the_project_and_keeps_the_text(self):
        clip_style = TextStyle(content="Jane Doe — Director")
        at_4k = apply_style(clip_style, BUILTIN_STYLES["Lower Third"], 2160)
        self.assertEqual(at_4k.content, "Jane Doe — Director")
        self.assertEqual(at_4k.font_size, 92)  # 46 at 1080p
        self.assertEqual(at_4k.box_padding, 36)
        self.assertTrue(at_4k.box_enabled and at_4k.alignment == "left")
        at_720 = apply_style(clip_style, BUILTIN_STYLES["Lower Third"], 720)
        self.assertEqual(at_720.font_size, 31)

    def test_a_saved_style_round_trips_at_any_resolution(self):
        original = apply_style(TextStyle(content="x"), BUILTIN_STYLES["Centered Title"], 2160)
        saved = style_of(original, 2160)
        self.assertEqual(saved["font_size"], BUILTIN_STYLES["Centered Title"]["font_size"])
        self.assertNotIn("content", saved)
        again = apply_style(TextStyle(content="y"), saved, 2160)
        self.assertEqual((again.font_size, again.outline_width, again.content), (original.font_size, original.outline_width, "y"))
        self.assertEqual(apply_style(TextStyle(), {"font_size": "big", "nonsense": 1}, 1080), TextStyle())

    def test_the_user_store(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "styles.json")
        save_user_style("My Look", {"font_size": 50, "color": "#FF0000", "content": "ignored"}, path)
        self.assertEqual(load_user_styles(path), {"My Look": {"font_size": 50, "color": "#FF0000"}})
        self.assertIn("Lower Third", all_styles(path)) and self.assertIn("My Look", all_styles(path))
        with self.assertRaises(ValueError):
            save_user_style("  ", {}, path)
        self.assertTrue(delete_user_style("My Look", path))
        self.assertFalse(delete_user_style("Lower Third", path))  # built-ins aren't in the file
        open(path, "w").write("{broken")
        self.assertEqual(load_user_styles(path), {})


class TitleStylePanelTests(unittest.TestCase):
    def test_apply_save_and_delete_from_the_properties_panel(self):
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PySide6 not installed")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), \
                patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
            window._autosave_timer.stop()
            try:
                clip = Clip(media_type=MediaType.TEXT, source_path="", name="T", text_style=TextStyle(content="Hello"),
                            source_in=0.0, source_out=3.0, timeline_start=0.0, source_duration=3.0)
                window.project.tracks[0].clips.append(clip)
                window.timeline.rebuild()
                window.timeline.select_clips([clip.id])
                panel = window.properties
                height = window.project.settings.height
                self.assertTrue(panel.apply_title_style("Caption"))
                styled = window.project.find_clip(clip.id).text_style
                self.assertEqual((styled.content, styled.box_enabled), ("Hello", True))
                self.assertEqual(styled.font_size, round(44 * height / 1080))
                window.history.undo()
                self.assertFalse(window.project.find_clip(clip.id).text_style.box_enabled)
                self.assertEqual(panel.save_title_style("House Style"), "House Style")
                self.assertIn("House Style", [panel.title_style_combo.itemText(i) for i in range(panel.title_style_combo.count())])
                self.assertFalse(panel.delete_title_style("Lower Third"))  # built-in
                self.assertTrue(panel.delete_title_style("House Style"))
            finally:
                window._clear_autosave()
                window.deleteLater()
                QApplication.clipboard().clear()
                app.processEvents()
        shutil.rmtree(home, ignore_errors=True)
