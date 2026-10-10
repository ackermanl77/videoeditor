"""Styled ASS/SSA subtitles (core.subtitles): parsing the script, styled clips over tracks,
writing the script back at the clips' timeline times, and saving it with the project."""

from __future__ import annotations

import unittest

from core.clip import AssEvent, Clip, MediaType
from core.project import Project
from core.subtitles import (
    build_ass_script,
    format_ass_timestamp,
    split_ass_script,
    styled_script_for_export,
    styled_subtitle_tracks,
)
from core.track import Track, TrackType

SCRIPT = """﻿[Script Info]
Title: Test
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,60,&H0000FFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,0,2,10,10,10,1
Style: Sign,Georgia,40,&H00FF00FF,&H000000FF,&H00000000,&H00000000,0,-1,0,0,100,100,0,0,1,2,0,8,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Comment: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,not shown
Dialogue: 0,0:00:01.00,0:00:03.00,Default,Ann,0,0,0,,Hello, {\\b0}world\\Nsecond line
Dialogue: 1,0:00:02.00,0:00:04.50,Sign,,5,6,7,Banner;10,{\\an7\\pos(10,10)}SIGN
Dialogue: 0,0:00:03.50,0:00:05.00,Default,,0,0,0,,Later
"""


class ParseTests(unittest.TestCase):
    def test_header_events_and_styles(self):
        script = split_ass_script(SCRIPT)
        self.assertTrue(script.header.startswith("[Script Info]"))
        self.assertIn("Style: Sign,Georgia", script.header)
        self.assertNotIn("[Events]", script.header)
        self.assertNotIn("Dialogue", script.header)
        self.assertEqual(script.play_res_y, 1080)
        self.assertEqual(sorted(script.styles), ["Default", "Sign"])
        first, sign, later = script.dialogues
        self.assertEqual((first.start, first.end, first.name), (1.0, 3.0, "Ann"))
        self.assertEqual(first.text, "Hello, {\\b0}world\\Nsecond line")  # commas inside the text kept
        self.assertEqual((sign.layer, sign.style, sign.margin_l, sign.margin_r, sign.margin_v, sign.effect),
                         (1, "Sign", 5, 6, 7, "Banner;10"))

    def test_overlapping_lines_go_on_extra_tracks(self):
        tracks = styled_subtitle_tracks(split_ass_script(SCRIPT), "s1")
        self.assertEqual([[c.text_style.content for c in t] for t in tracks],
                         [["Hello, world\nsecond line", "Later"], ["SIGN"]])
        sign = tracks[1][0]
        self.assertEqual((sign.timeline_start, sign.duration), (2.0, 2.5))
        self.assertEqual(sign.ass_event.text, "{\\an7\\pos(10,10)}SIGN")
        # The preview approximation follows the style and the \\an override.
        self.assertEqual(sign.text_style.font_family, "Georgia")
        self.assertEqual(sign.text_style.color, "#FF00FF")
        self.assertTrue(sign.text_style.italic)
        self.assertEqual((sign.text_style.position_y, sign.text_style.alignment), (0.12, "left"))
        hello = tracks[0][0]
        self.assertEqual((hello.text_style.color, hello.text_style.font_size, hello.text_style.bold), ("#FFFF00", 60, True))


class BuildTests(unittest.TestCase):
    def _clips(self):
        return [c for t in styled_subtitle_tracks(split_ass_script(SCRIPT), "s1") for c in t]

    def test_round_trip_keeps_every_field_and_the_raw_text(self):
        script = split_ass_script(SCRIPT)
        text = build_ass_script(script.header, self._clips())
        self.assertIn("Dialogue: 0,0:00:01.00,0:00:03.00,Default,Ann,0,0,0,,Hello, {\\b0}world\\Nsecond line", text)
        self.assertIn("Dialogue: 1,0:00:02.00,0:00:04.50,Sign,,5,6,7,Banner;10,{\\an7\\pos(10,10)}SIGN", text)
        self.assertEqual(split_ass_script(text).dialogues, script.dialogues)

    def test_moved_trimmed_and_edited_lines(self):
        clips = self._clips()
        hello = clips[0]
        hello.timeline_start = 10.0
        hello.text_style.content = "New {text}\nhere"
        text = build_ass_script("[Script Info]\n", clips, offset=2.5, end=11.0)
        lines = [line for line in text.splitlines() if line.startswith("Dialogue")]
        self.assertEqual(lines, [
            "Dialogue: 1,0:00:00.00,0:00:02.00,Sign,,5,6,7,Banner;10,{\\an7\\pos(10,10)}SIGN",
            "Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,Later",
            "Dialogue: 0,0:00:07.50,0:00:08.50,Default,Ann,0,0,0,,New (text)\\Nhere",
        ])

    def test_timestamps(self):
        self.assertEqual(format_ass_timestamp(3725.456), "1:02:05.46")
        self.assertEqual(format_ass_timestamp(-1), "0:00:00.00")


class ProjectTests(unittest.TestCase):
    def _project(self):
        project = Project()
        script = split_ass_script(SCRIPT)
        project.subtitle_scripts["s1"] = script.header
        project.subtitle_scripts["orphan"] = "[Script Info]\n"
        for n, clips in enumerate(styled_subtitle_tracks(script, "s1"), start=1):
            track = Track(track_type=TrackType.VIDEO, name="Subtitles" if n == 1 else f"Subtitles {n}")
            track.clips.extend(clips)
            project.tracks.append(track)
        return project

    def test_saved_and_loaded_with_the_project_without_unused_headers(self):
        project = self._project()
        data = project.to_dict()
        self.assertEqual(list(data["subtitle_scripts"]), ["s1"])
        loaded = Project.from_dict(data)
        clip = loaded.tracks[2].clips[0]
        self.assertIsInstance(clip.ass_event, AssEvent)
        self.assertEqual(clip.ass_event.script_id, "s1")
        self.assertEqual(Clip.from_dict(clip.to_dict()).ass_event, clip.ass_event)
        plain = Clip(media_type=MediaType.TEXT)
        self.assertNotIn("ass_event", plain.to_dict())
        self.assertNotIn("subtitle_scripts", Project().to_dict())

    def test_export_script_needs_one_script_and_only_styled_lines(self):
        project = self._project()
        script = styled_script_for_export(project, 1.0, 5.0)
        self.assertIn("Style: Sign", script)
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:02.00,Default,Ann", script)
        project.tracks[2].clips.append(Clip(media_type=MediaType.TEXT, timeline_start=20.0, source_out=1.0))
        self.assertIsNone(styled_script_for_export(project))  # a plain clip among them: SRT instead


class GraphTests(unittest.TestCase):
    def _project(self):
        project = Project()
        script = split_ass_script(SCRIPT)
        project.subtitle_scripts["s1"] = script.header
        track = Track(track_type=TrackType.VIDEO, name="Subtitles")
        track.clips.extend(styled_subtitle_tracks(script, "s1")[0])
        project.tracks.append(track)
        return project

    def test_styled_lines_are_drawn_by_libass_not_drawtext(self):
        from core.filter_graph import build_filter_graph

        project = self._project()
        graph = build_filter_graph(project, project.settings, video_only=True)
        try:
            self.assertEqual(graph.filter_complex.count("ass=filename="), 1)
            self.assertNotIn("drawtext", graph.filter_complex)
            self.assertEqual(graph.warnings, [])
        finally:
            graph.cleanup()
        del project.subtitle_scripts["s1"]  # the script is gone: plain drawtext captions instead
        graph = build_filter_graph(project, project.settings, video_only=True)
        try:
            self.assertNotIn("ass=filename=", graph.filter_complex)
            self.assertEqual(graph.filter_complex.count("drawtext"), 2)
        finally:
            graph.cleanup()

    def test_an_hdr_export_warns_about_styled_subtitles(self):
        from core.filter_graph import build_filter_graph
        from core.output_color import OutputColor

        project = self._project()
        graph = build_filter_graph(project, project.settings, video_only=True, output_color=OutputColor.from_mode("pq"))
        try:
            self.assertTrue(any("HDR" in w for w in graph.warnings))
        finally:
            graph.cleanup()


if __name__ == "__main__":
    unittest.main()
