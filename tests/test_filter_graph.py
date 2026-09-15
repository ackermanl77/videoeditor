"""Tests for the FFmpeg filter-graph construction logic (no FFmpeg required).

These tests only check the *structure* of the generated command/graph, not
that FFmpeg can actually execute it — that would require FFmpeg and real
media files. They protect against regressions in the graph-building logic
(labels, escaping, ordering) which is the trickiest part of the exporter.
"""

import unittest

from core.clip import Clip, MediaType, TextStyle
from core.filter_graph import _escape_text, build_filter_graph
from core.media_asset import MediaAsset
from core.project import Project, ProjectSettings
from core.track import TrackType


class FilterGraphTests(unittest.TestCase):
    def test_single_video_clip_produces_one_input(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=True))
        track = project.tracks[0]
        track.clips.append(Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0))

        graph = build_filter_graph(project, ProjectSettings(width=1280, height=720, fps=30))
        # Video and audio streams of the same file share a single -i input.
        self.assertEqual(len(graph.input_specs), 1)
        self.assertIn("[0:v]", graph.filter_complex)
        self.assertIn("[0:a]", graph.filter_complex)
        self.assertIn("color=c=black:s=1280x720", graph.filter_complex)
        self.assertIn("overlay=0:0", graph.filter_complex)
        self.assertTrue(graph.has_audio)

    def test_muted_clip_has_no_audio_input(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=True))
        track = project.tracks[0]
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0, muted=True)
        )
        graph = build_filter_graph(project, ProjectSettings())
        # Only the video input should be present; audio mix falls back to anullsrc.
        self.assertEqual(len(graph.input_specs), 1)
        self.assertIn("anullsrc", graph.filter_complex)

    def test_two_clips_on_same_track_both_present(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        project.add_media_asset(MediaAsset(path="/media/b.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0))
        track.clips.append(Clip(media_type=MediaType.VIDEO, source_path="/media/b.mp4", source_in=0, source_out=5, timeline_start=5))

        graph = build_filter_graph(project, ProjectSettings())
        self.assertEqual(graph.filter_complex.count("trim=duration="), 2)
        self.assertEqual(graph.filter_complex.count("overlay=0:0"), 2)

    def test_text_clip_generates_drawtext(self):
        project = Project()
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.TEXT,
                source_in=0,
                source_out=3,
                timeline_start=0,
                text_style=TextStyle(content="Hello World"),
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("drawtext", graph.filter_complex)
        self.assertIn("Hello World", graph.filter_complex)

    def test_image_clip_uses_loop_input(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/pic.png", duration=0.0))
        track = project.tracks[0]
        track.clips.append(Clip(media_type=MediaType.IMAGE, source_path="/media/pic.png", source_in=0, source_out=4, timeline_start=0))
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("-loop", graph.input_specs[0].pre_args)

    def test_audio_only_clip_mixed(self):
        project = Project()
        audio_track = [t for t in project.tracks if t.track_type == TrackType.AUDIO][0]
        audio_track.clips.append(
            Clip(media_type=MediaType.AUDIO, source_path="/media/song.mp3", source_in=0, source_out=8, timeline_start=0)
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("amix=inputs=1", graph.filter_complex)

    def test_empty_project_still_produces_valid_graph(self):
        project = Project()
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("anullsrc", graph.filter_complex)
        self.assertTrue(graph.video_out_label)


class TextEscapingTests(unittest.TestCase):
    def test_escapes_special_characters(self):
        escaped = _escape_text("Hello: World's 100% 'quote'")
        self.assertNotIn("100%", escaped)  # % must be escaped
        self.assertIn("\\:", escaped)
        self.assertIn("\\'", escaped)


if __name__ == "__main__":
    unittest.main()
