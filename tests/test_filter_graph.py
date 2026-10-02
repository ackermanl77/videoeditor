"""Tests for the FFmpeg filter-graph construction logic (no FFmpeg required).

These tests only check the *structure* of the generated command/graph, not
that FFmpeg can actually execute it — that would require FFmpeg and real
media files. They protect against regressions in the graph-building logic
(labels, escaping, ordering) which is the trickiest part of the exporter.
"""

import math
import os
import unittest

from core.clip import Clip, MediaType, ReframeMode, TextStyle, TransitionType
from core.filter_graph import _escape_filter_value, build_filter_graph
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
        self.assertIn("overlay=x=", graph.filter_complex)
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
        self.assertEqual(graph.filter_complex.count("overlay=x="), 2)

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
        self.addCleanup(graph.cleanup)
        self.assertIn("drawtext=textfile=", graph.filter_complex)
        self.assertIn(":expansion=none", graph.filter_complex)
        # The text itself travels through a temp UTF-8 file, never inline.
        self.assertNotIn("Hello World", graph.filter_complex)
        self.assertEqual(len(graph.temp_files), 1)
        with open(graph.temp_files[0], "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "Hello World")

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


class TextOverlayFileTests(unittest.TestCase):
    """Regression tests: text containing ' or % used to break export.

    The old inline ``drawtext=text='...'`` form backslash-escaped ``'`` and
    ``%`` inside a single-quoted filter value, where a backslash is literal
    and ``'`` always ends the quote -- "Don't stop" made FFmpeg reject the
    whole graph. Text now goes through ``textfile=`` + ``expansion=none``.
    """

    def _graph_with_text(self, content):
        project = Project()
        project.tracks[0].clips.append(
            Clip(media_type=MediaType.TEXT, source_in=0, source_out=3, timeline_start=0, text_style=TextStyle(content=content))
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.addCleanup(graph.cleanup)
        return graph

    def test_special_characters_round_trip_through_the_text_file(self):
        for content in ("Don't stop", "50% at 10:30", "a [b], c; d \\ e", "Árbol ñandú 日本", "%{pts}"):
            with self.subTest(content=content):
                graph = self._graph_with_text(content)
                with open(graph.temp_files[0], "r", encoding="utf-8", newline="") as f:
                    self.assertEqual(f.read(), content)
                self.assertNotIn(content, graph.filter_complex)

    def test_multiline_text_is_written_with_plain_newlines(self):
        graph = self._graph_with_text("line one\nline two")
        with open(graph.temp_files[0], "rb") as f:
            self.assertEqual(f.read(), b"line one\nline two")

    def test_cleanup_removes_the_temp_files(self):
        graph = self._graph_with_text("bye")
        path = graph.temp_files[0]
        self.assertTrue(os.path.exists(path))
        graph.cleanup()
        self.assertFalse(os.path.exists(path))
        graph.cleanup()  # idempotent

    def test_escape_filter_value_escapes_both_parsing_levels(self):
        # ':' is option-level special (one backslash, then that backslash is
        # itself escaped at graph level); ',' is graph-level only.
        self.assertEqual(_escape_filter_value("C:/a,b"), r"C\\:/a\,b")
        self.assertEqual(_escape_filter_value("it's"), r"it\\\'s")
        self.assertEqual(_escape_filter_value("plain/path.txt"), "plain/path.txt")


class CrossfadeTransitionTests(unittest.TestCase):
    def test_crossfade_between_adjacent_clips_produces_one_merged_layer(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        project.add_media_asset(MediaAsset(path="/media/b.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5,
                timeline_start=0, transition_out=TransitionType.CROSSFADE, transition_duration=1.0,
            )
        )
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/b.mp4", source_in=0, source_out=5, timeline_start=5)
        )

        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("xfade=transition=fade:duration=1.000:offset=4.000", graph.filter_complex)
        # The pair becomes a single composited layer, not two.
        self.assertEqual(graph.filter_complex.count("overlay=x="), 1)
        # Each clip's own fade (superseded by the crossfade) is skipped.
        self.assertNotIn(":alpha=1", graph.filter_complex)
        # xfade hard-fails ("inputs needs to be a constant frame rate")
        # without this — confirmed against real FFmpeg, see PROJECT_STATUS.md.
        self.assertEqual(graph.filter_complex.count(f",fps={ProjectSettings().fps}"), 2)

    def test_no_gap_means_no_crossfade(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        project.add_media_asset(MediaAsset(path="/media/b.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5,
                timeline_start=0, transition_out=TransitionType.CROSSFADE, transition_duration=1.0,
            )
        )
        # A gap before the next clip means there's nothing to overlap.
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/b.mp4", source_in=0, source_out=5, timeline_start=6)
        )

        graph = build_filter_graph(project, ProjectSettings())
        self.assertNotIn("xfade", graph.filter_complex)
        self.assertEqual(graph.filter_complex.count("overlay=x="), 2)

    def test_crossfade_duration_clamped_to_the_shorter_clip(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        project.add_media_asset(MediaAsset(path="/media/b.mp4", duration=2.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5,
                timeline_start=0, transition_out=TransitionType.CROSSFADE, transition_duration=10.0,
            )
        )
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/b.mp4", source_in=0, source_out=2, timeline_start=5)
        )

        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("duration=2.000", graph.filter_complex)

    def test_crossfade_not_applied_into_a_text_clip(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5,
                timeline_start=0, transition_out=TransitionType.CROSSFADE, transition_duration=1.0,
            )
        )
        track.clips.append(
            Clip(media_type=MediaType.TEXT, source_in=0, source_out=3, timeline_start=5, text_style=TextStyle(content="Hi"))
        )

        graph = build_filter_graph(project, ProjectSettings())
        self.addCleanup(graph.cleanup)
        self.assertNotIn("xfade", graph.filter_complex)
        self.assertIn("drawtext", graph.filter_complex)


class ColorCorrectionTests(unittest.TestCase):
    def test_eq_filter_added_when_color_correction_is_non_neutral(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5,
                timeline_start=0, brightness=0.2, contrast=1.5, saturation=0.5, gamma=1.1,
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("eq=brightness=0.200:contrast=1.500:saturation=0.500:gamma=1.100", graph.filter_complex)

    def test_eq_filter_omitted_for_an_unedited_clip(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0)
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertNotIn("eq=", graph.filter_complex)


class ChromaKeyTests(unittest.TestCase):
    def test_colorkey_filter_added_when_enabled(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0,
                chroma_key_enabled=True, chroma_key_color="#00FF00", chroma_key_similarity=0.3, chroma_key_blend=0.15,
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("colorkey=0x00FF00:0.300:0.150", graph.filter_complex)

    def test_colorkey_filter_omitted_when_disabled(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0)
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertNotIn("colorkey", graph.filter_complex)


class TransformTests(unittest.TestCase):
    def test_default_transform_reproduces_the_old_centered_overlay(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0)
        )
        graph = build_filter_graph(project, ProjectSettings())
        # An unedited clip's overlay position expression must evaluate to
        # exactly 0:0 (same as the old hardcoded overlay=0:0) — (w/2 canceling
        # against main_w*0.5 when the layer is full-canvas-sized).
        self.assertIn("overlay=x='(main_w*(0.5000))-(w/2)':y='(main_h*(0.5000))-(h/2)'", graph.filter_complex)
        self.assertNotIn("scale=iw*", graph.filter_complex)
        self.assertNotIn("rotate=", graph.filter_complex)
        # No pad step either: Transform's overlay-based positioning replaces it.
        self.assertNotIn("pad=", graph.filter_complex)

    def test_scale_and_position_are_reflected_in_the_chain(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0,
                transform_x=0.25, transform_y=0.75, transform_scale=0.5,
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("scale=iw*0.5000:ih*0.5000", graph.filter_complex)
        self.assertIn("overlay=x='(main_w*(0.2500))-(w/2)':y='(main_h*(0.7500))-(h/2)'", graph.filter_complex)

    def test_rotation_uses_radians_and_expands_the_output_box(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0,
                transform_rotation=90.0,
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        expected_radians = f"{math.radians(90.0):.6f}"
        self.assertIn(f"rotate={expected_radians}:c=none:ow=rotw({expected_radians}):oh=roth({expected_radians})", graph.filter_complex)

    def test_transform_is_ignored_for_a_crossfading_clip(self):
        # xfade requires matching frame sizes between its two inputs, so a
        # clip's Transform is a documented no-op while it's crossfading —
        # it should fall back to the old full-canvas pad instead.
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        project.add_media_asset(MediaAsset(path="/media/b.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0,
                transition_out=TransitionType.CROSSFADE, transition_duration=1.0,
                transform_scale=0.5, transform_x=0.1, transform_y=0.1,
            )
        )
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/b.mp4", source_in=0, source_out=5, timeline_start=5)
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("pad=", graph.filter_complex)
        self.assertNotIn("scale=iw*", graph.filter_complex)
        # The merged crossfade layer is centered (0.5, 0.5), ignoring the
        # transform_x/y set on the clip above.
        self.assertIn("overlay=x='(main_w*(0.5))-(w/2)':y='(main_h*(0.5))-(h/2)'", graph.filter_complex)


class KeyframeTests(unittest.TestCase):
    def test_single_opacity_keyframe_behaves_like_no_keyframes(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0,
                opacity_keyframes=[(1.0, 0.5)],
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertNotIn("geq=", graph.filter_complex)

    def test_two_or_more_opacity_keyframes_produce_a_geq_alpha_ramp(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0,
                opacity_keyframes=[(0.0, 0.2), (2.0, 1.0)],
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("geq=lum='lum(X,Y)':cb='cb(X,Y)':cr='cr(X,Y)':a='alpha(X,Y)*(", graph.filter_complex)
        # Piecewise expression: before t=0 hold 0.2, interpolate to 1.0 by t=2, hold 1.0 after.
        self.assertIn("if(lt(T,0.000000),0.200000,if(lt(T,2.000000)", graph.filter_complex)

    def test_position_keyframes_produce_a_time_varying_overlay_expression(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=3.0,
                position_keyframes=[(0.0, 0.1, 0.2), (1.0, 0.9, 0.8)],
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        # The overlay step runs on the timeline-shifted (global-time) stream,
        # so the keyframe expression must subtract the clip's timeline_start
        # to convert overlay's `t` back to clip-local time.
        self.assertIn("(t-3.000000)", graph.filter_complex)
        self.assertIn("if(lt((t-3.000000),0.000000),0.100000,if(lt((t-3.000000),1.000000)", graph.filter_complex)

    def test_position_keyframes_ignored_for_a_crossfading_clip(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        project.add_media_asset(MediaAsset(path="/media/b.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0,
                transition_out=TransitionType.CROSSFADE, transition_duration=1.0,
                position_keyframes=[(0.0, 0.1, 0.1), (1.0, 0.9, 0.9)],
            )
        )
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/b.mp4", source_in=0, source_out=5, timeline_start=5)
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("overlay=x='(main_w*(0.5))-(w/2)':y='(main_h*(0.5))-(h/2)'", graph.filter_complex)


class SpeedRampTests(unittest.TestCase):
    def test_single_speed_keyframe_behaves_like_a_normal_clip(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0,
                speed_keyframes=[(1.0, 3.0)],
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertNotIn("concat=", graph.filter_complex)
        self.assertEqual(len(graph.input_specs), 1)

    def test_two_speed_keyframes_produce_segments_and_concat(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=10.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=10, timeline_start=0,
                speed_keyframes=[(0.0, 1.0), (4.0, 2.0)],
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        # Two segments -> two -i inputs, one for each speed zone.
        self.assertEqual(len(graph.input_specs), 2)
        self.assertIn("concat=n=2:v=1:a=0", graph.filter_complex)
        self.assertIn("setpts=PTS/2.000000", graph.filter_complex)  # the 2x segment
        # Only one overlay layer — the segments merge into a single clip layer.
        self.assertEqual(graph.filter_complex.count("overlay=x="), 1)

    def test_speed_ramped_clips_audio_is_segmented_and_concatenated_too(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=10.0, has_video=True, has_audio=True))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=10, timeline_start=0,
                speed_keyframes=[(0.0, 1.0), (4.0, 2.0)],
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("concat=n=2:v=0:a=1", graph.filter_complex)
        self.assertTrue(graph.has_audio)

    def test_speed_ramped_audio_with_fade_has_no_stray_leading_comma(self):
        # Regression: a bracket reference immediately followed by a comma
        # (e.g. "[label],afade=...") is invalid FFmpeg syntax ("No such
        # filter: ''") — confirmed against real FFmpeg. _video_fade_and_
        # opacity_suffix's leading comma is correctly stripped when it
        # starts a fresh filter_lines entry rather than continuing one.
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=10.0, has_video=True, has_audio=True))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=10, timeline_start=0,
                speed_keyframes=[(0.0, 1.0), (4.0, 2.0)], fade_in=0.5, fade_out=0.5,
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertNotIn("],afade", graph.filter_complex)
        self.assertNotIn("],fade", graph.filter_complex)
        self.assertIn("afade=t=in", graph.filter_complex)

    def test_speed_ramped_clip_cannot_be_a_crossfade_partner(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=10.0, has_video=True, has_audio=False))
        project.add_media_asset(MediaAsset(path="/media/b.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=10, timeline_start=0,
                transition_out=TransitionType.CROSSFADE, transition_duration=1.0,
                speed_keyframes=[(0.0, 1.0), (4.0, 2.0)],
            )
        )
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/b.mp4", source_in=0, source_out=5, timeline_start=track.clips[0].timeline_end)
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertNotIn("xfade", graph.filter_complex)


class AudioFadeTests(unittest.TestCase):
    def test_fade_in_and_out_applied_to_an_audio_only_clip(self):
        project = Project()
        audio_track = [t for t in project.tracks if t.track_type == TrackType.AUDIO][0]
        audio_track.clips.append(
            Clip(
                media_type=MediaType.AUDIO, source_path="/media/song.mp3", source_in=0, source_out=8,
                timeline_start=0, fade_in=1.0, fade_out=2.0,
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("afade=t=in:st=0:d=1.000", graph.filter_complex)
        self.assertIn("afade=t=out:st=6.000:d=2.000", graph.filter_complex)

    def test_fade_also_applies_to_a_video_clips_own_audio(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=True))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5,
                timeline_start=0, fade_in=1.0,
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("afade=t=in:st=0:d=1.000", graph.filter_complex)
        self.assertIn("fade=t=in:st=0:d=1.000:alpha=1", graph.filter_complex)


class ReframeTests(unittest.TestCase):
    def test_fit_is_the_default_and_unchanged(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0)
        )
        graph = build_filter_graph(project, ProjectSettings(width=1080, height=1920))
        self.assertIn("force_original_aspect_ratio=decrease", graph.filter_complex)
        self.assertNotIn("force_original_aspect_ratio=increase", graph.filter_complex)
        self.assertNotIn("crop=", graph.filter_complex)

    def test_fill_mode_crops_instead_of_padding(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0,
                reframe_mode=ReframeMode.FILL, crop_focus_x=0.25, crop_focus_y=0.75,
            )
        )
        graph = build_filter_graph(project, ProjectSettings(width=1080, height=1920))
        self.assertIn("force_original_aspect_ratio=increase", graph.filter_complex)
        self.assertNotIn("force_original_aspect_ratio=decrease", graph.filter_complex)
        self.assertNotIn("pad=1080:1920", graph.filter_complex)
        self.assertIn("crop=1080:1920:'(iw-1080)*0.2500':'(ih-1920)*0.7500'", graph.filter_complex)

    def test_transform_scale_and_rotation_are_skipped_in_fill_mode(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0,
                reframe_mode=ReframeMode.FILL, transform_scale=0.5, transform_rotation=45.0,
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertNotIn("scale=iw*", graph.filter_complex)
        self.assertNotIn("rotate=", graph.filter_complex)

    def test_transform_position_is_centered_in_fill_mode(self):
        # The cropped frame already exactly fills the canvas, so Transform's
        # position (transform_x/y) is a documented no-op in FILL mode.
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0,
                reframe_mode=ReframeMode.FILL, transform_x=0.1, transform_y=0.9,
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("overlay=x='(main_w*(0.5))-(w/2)':y='(main_h*(0.5))-(h/2)'", graph.filter_complex)

    def test_reframe_override_forces_fill_without_mutating_the_clip(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        clip = Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0)
        track.clips.append(clip)

        graph = build_filter_graph(project, ProjectSettings(width=1080, height=1920), reframe_override=ReframeMode.FILL)
        self.assertIn("force_original_aspect_ratio=increase", graph.filter_complex)
        # The override only affects this export's graph — the original
        # project's clip must still report its own stored FIT setting.
        self.assertEqual(clip.reframe_mode, ReframeMode.FIT)

    def test_reframe_override_does_not_affect_audio_only_clips(self):
        project = Project()
        audio_track = [t for t in project.tracks if t.track_type == TrackType.AUDIO][0]
        audio_track.clips.append(
            Clip(media_type=MediaType.AUDIO, source_path="/media/song.mp3", source_in=0, source_out=8, timeline_start=0)
        )
        # Should not raise even though AUDIO clips have no reframe_mode use.
        graph = build_filter_graph(project, ProjectSettings(), reframe_override=ReframeMode.FILL)
        self.assertIn("amix=inputs=1", graph.filter_complex)


class TrimRangeTests(unittest.TestCase):
    def test_no_trim_range_leaves_output_labels_unwrapped(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0)
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertNotIn("trim=start=", graph.filter_complex)
        self.assertNotIn("atrim=start=", graph.filter_complex)

    def test_trim_range_builds_only_the_window(self):
        # B2: the graph is built for the window itself -- a 5 s canvas, the
        # input seeked to the window's start and bounded to its length --
        # instead of rendering the whole timeline and trimming the result.
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=10.0, has_video=True, has_audio=True))
        track = project.tracks[0]
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=10, timeline_start=0)
        )
        graph = build_filter_graph(project, ProjectSettings(), trim_range=(2.0, 7.0))
        fc = graph.filter_complex
        self.assertIn("color=c=black:s=1920x1080:d=5.000", fc)
        self.assertEqual(graph.input_specs[0].pre_args, ["-ss", "2.000", "-t", "5.500"])
        self.assertIn("[0:v]trim=duration=5.000,setpts=PTS-STARTPTS,setpts=PTS+2.000000/TB", fc)
        self.assertIn("atrim=duration=5.000[aout]", fc)
        self.assertNotIn("trim=start=2.000:end=7.000", fc)
        self.assertEqual(graph.video_out_label, "comp0")
        self.assertEqual(graph.audio_out_label, "aout")

    def test_trim_end_none_defaults_to_project_duration(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=10.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=10, timeline_start=0)
        )
        graph = build_filter_graph(project, ProjectSettings(), trim_range=(1.0, None))
        self.assertIn("color=c=black:s=1920x1080:d=9.000", graph.filter_complex)
        self.assertEqual(graph.input_specs[0].pre_args[:2], ["-ss", "1.000"])

    def _sequence(self, n=4, length=5.0):
        project = Project()
        for i in range(n):
            path = f"/media/c{i}.mp4"
            project.add_media_asset(MediaAsset(path=path, duration=length, has_video=True, has_audio=True))
            project.tracks[0].clips.append(
                Clip(media_type=MediaType.VIDEO, source_path=path, source_in=0, source_out=length,
                     timeline_start=i * length)
            )
        return project

    def test_clips_outside_the_window_are_left_out(self):
        graph = build_filter_graph(self._sequence(), ProjectSettings(), trim_range=(12.0, 14.0))
        self.assertEqual([os.path.basename(s.path) for s in graph.input_specs], ["c2.mp4"])
        self.assertEqual(graph.input_specs[0].pre_args, ["-ss", "2.000", "-t", "2.500"])
        self.assertEqual(graph.filter_complex.count("overlay="), 1)

    def test_clip_starting_inside_the_window_is_shifted_by_the_window_start(self):
        graph = build_filter_graph(self._sequence(), ProjectSettings(), trim_range=(12.0, 17.0))
        fc = graph.filter_complex
        # c3 starts at 15 s = 3 s into the window; its audio is delayed the same.
        self.assertIn("setpts=PTS+3.000/TB[v3]", fc)
        self.assertIn("adelay=delays=3000:all=1[a3]", fc)

    def test_windowed_text_and_position_keyframes_use_window_time(self):
        project = self._sequence(2)
        project.tracks[0].clips[1].position_keyframes = [(0.0, 0.2, 0.2), (2.0, 0.8, 0.8)]
        project.tracks[0].clips.append(
            Clip(media_type=MediaType.TEXT, source_in=0, source_out=4, timeline_start=6.0)
        )
        graph = build_filter_graph(project, ProjectSettings(), trim_range=(7.0, 10.0))
        self.assertIn("between(t,-1.000,3.000)", graph.filter_complex)
        # clip 2 starts at 5 s, 2 s before the window: clip-local = t + 2.
        self.assertIn("(t+2.000000)", graph.filter_complex)
        graph.cleanup()

    def test_full_export_shifts_with_setpts_not_tpad(self):
        # B1: tpad generated start*fps transparent full-canvas frames per clip.
        graph = build_filter_graph(self._sequence(), ProjectSettings())
        self.assertNotIn("tpad", graph.filter_complex)
        self.assertIn("setpts=PTS+15.000/TB[v3]", graph.filter_complex)
        for spec in graph.input_specs:
            self.assertEqual(spec.pre_args, ["-ss", "0.000", "-t", "5.500"])


class DenoiseTests(unittest.TestCase):
    def test_afftdn_added_when_denoise_enabled_on_audio_only_clip(self):
        project = Project()
        audio_track = [t for t in project.tracks if t.track_type == TrackType.AUDIO][0]
        audio_track.clips.append(
            Clip(
                media_type=MediaType.AUDIO, source_path="/media/song.mp3", source_in=0, source_out=8,
                timeline_start=0, denoise_enabled=True, denoise_strength=20.0,
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("afftdn=nr=20.000", graph.filter_complex)

    def test_afftdn_omitted_when_denoise_disabled(self):
        project = Project()
        audio_track = [t for t in project.tracks if t.track_type == TrackType.AUDIO][0]
        audio_track.clips.append(
            Clip(media_type=MediaType.AUDIO, source_path="/media/song.mp3", source_in=0, source_out=8, timeline_start=0)
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertNotIn("afftdn", graph.filter_complex)

    def test_afftdn_applied_to_a_video_clips_own_audio(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=True))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5,
                timeline_start=0, denoise_enabled=True, denoise_strength=12.0,
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("afftdn=nr=12.000", graph.filter_complex)

    def test_afftdn_applied_to_speed_ramped_clips_audio(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=10.0, has_video=True, has_audio=True))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=10, timeline_start=0,
                speed_keyframes=[(0.0, 1.0), (4.0, 2.0)], denoise_enabled=True, denoise_strength=15.0,
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("afftdn=nr=15.000", graph.filter_complex)


class TransitionVarietyTests(unittest.TestCase):
    def _pair_project(self, transition: TransitionType):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        project.add_media_asset(MediaAsset(path="/media/b.mp4", duration=5.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5,
                timeline_start=0, transition_out=transition, transition_duration=1.0,
            )
        )
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/b.mp4", source_in=0, source_out=5, timeline_start=5)
        )
        return project

    def test_each_xfade_transition_name_is_emitted(self):
        cases = [
            (TransitionType.WIPE_LEFT, "wipeleft"),
            (TransitionType.WIPE_RIGHT, "wiperight"),
            (TransitionType.WIPE_UP, "wipeup"),
            (TransitionType.WIPE_DOWN, "wipedown"),
            (TransitionType.SLIDE_LEFT, "slideleft"),
            (TransitionType.SLIDE_RIGHT, "slideright"),
            (TransitionType.SLIDE_UP, "slideup"),
            (TransitionType.SLIDE_DOWN, "slidedown"),
            (TransitionType.CIRCLE_CROP, "circlecrop"),
            (TransitionType.DISSOLVE, "dissolve"),
        ]
        for transition_type, xfade_name in cases:
            with self.subTest(transition_type=transition_type):
                project = self._pair_project(transition_type)
                graph = build_filter_graph(project, ProjectSettings())
                self.assertIn(f"xfade=transition={xfade_name}:duration=1.000", graph.filter_complex)

    def test_crossfade_still_maps_to_fade_for_backward_compatibility(self):
        project = self._pair_project(TransitionType.CROSSFADE)
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("xfade=transition=fade:duration=1.000", graph.filter_complex)

    def test_none_transition_never_forms_a_pair(self):
        project = self._pair_project(TransitionType.NONE)
        graph = build_filter_graph(project, ProjectSettings())
        self.assertNotIn("xfade", graph.filter_complex)


class AudioDuckingTests(unittest.TestCase):
    def test_sidechaincompress_ducks_music_under_narration(self):
        project = Project()
        # tracks[0] = default Video 1, tracks[1] = default Audio 1 (narration)
        narration_track = project.tracks[1]
        music_track = project.add_track(TrackType.AUDIO, name="Music")
        music_track.duck_trigger_track_id = narration_track.id

        narration_track.clips.append(
            Clip(media_type=MediaType.AUDIO, source_path="/media/narration.mp3", source_in=0, source_out=10, timeline_start=0)
        )
        music_track.clips.append(
            Clip(media_type=MediaType.AUDIO, source_path="/media/music.mp3", source_in=0, source_out=10, timeline_start=0)
        )

        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("sidechaincompress=threshold=", graph.filter_complex)
        # Both the narration's own audio and the ducked-music output must
        # still reach the final mix (narration keeps playing at full volume;
        # only music gets compressed).
        self.assertEqual(graph.filter_complex.count("amix=inputs=2"), 1)

    def test_no_ducking_field_produces_identical_graph_to_before(self):
        project = Project()
        audio_track = project.tracks[1]
        audio_track.clips.append(
            Clip(media_type=MediaType.AUDIO, source_path="/media/song.mp3", source_in=0, source_out=8, timeline_start=0)
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertNotIn("sidechaincompress", graph.filter_complex)

    def test_ducking_ignored_when_trigger_track_has_no_audio(self):
        project = Project()
        narration_track = project.tracks[1]
        music_track = project.add_track(TrackType.AUDIO, name="Music")
        music_track.duck_trigger_track_id = narration_track.id
        # Narration track has no clips at all -- nothing to duck against.
        music_track.clips.append(
            Clip(media_type=MediaType.AUDIO, source_path="/media/music.mp3", source_in=0, source_out=10, timeline_start=0)
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertNotIn("sidechaincompress", graph.filter_complex)
        self.assertIn("amix=inputs=1", graph.filter_complex)

    def test_self_referencing_trigger_is_ignored(self):
        project = Project()
        audio_track = project.tracks[1]
        audio_track.duck_trigger_track_id = audio_track.id  # nonsensical, must be a no-op
        audio_track.clips.append(
            Clip(media_type=MediaType.AUDIO, source_path="/media/song.mp3", source_in=0, source_out=8, timeline_start=0)
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertNotIn("sidechaincompress", graph.filter_complex)

    def test_ducking_a_video_tracks_embedded_audio_under_an_audio_track(self):
        project = Project()
        video_track = project.tracks[0]
        narration_track = project.tracks[1]
        video_track.duck_trigger_track_id = narration_track.id

        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=True))
        video_track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0)
        )
        narration_track.clips.append(
            Clip(media_type=MediaType.AUDIO, source_path="/media/narration.mp3", source_in=0, source_out=5, timeline_start=0)
        )

        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("sidechaincompress", graph.filter_complex)


class AdelayAllChannelsTests(unittest.TestCase):
    """adelay=<ms>|<ms> only delayed the first two channels -- a 5.1 source's
    other channels played early. all=1 applies one delay to every channel."""

    def test_video_clips_own_audio_uses_all_channels_delay(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=True))
        project.tracks[0].clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=2.5)
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("adelay=delays=2500:all=1", graph.filter_complex)
        self.assertNotIn("|", graph.filter_complex)

    def test_speed_ramped_audio_uses_all_channels_delay(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=10.0, has_video=True, has_audio=True))
        project.tracks[0].clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=4, timeline_start=1.0,
                speed_keyframes=[(0.0, 1.0), (2.0, 2.0)],
            )
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("adelay=delays=1000:all=1", graph.filter_complex)


def _video_audio_project(track_hidden=False, track_muted=False):
    project = Project()
    project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=True))
    video_track = project.tracks[0]
    video_track.hidden = track_hidden
    video_track.muted = track_muted
    video_track.clips.append(
        Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0)
    )
    return project, video_track


class TrackHiddenMutedTests(unittest.TestCase):
    """Hidden = no picture but audio keeps playing; muted = no audio.

    Hidden used to skip the whole track (dropping its audio too), and a
    video track's `muted` flag was ignored for its clips' embedded audio.
    """

    def test_visible_unmuted_track_has_video_and_audio(self):
        project, _ = _video_audio_project()
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("[0:v]", graph.filter_complex)
        self.assertIn("[0:a]", graph.filter_complex)

    def test_hidden_track_keeps_its_audio_but_drops_its_picture(self):
        project, _ = _video_audio_project(track_hidden=True)
        graph = build_filter_graph(project, ProjectSettings())
        self.assertEqual(len(graph.input_specs), 1)
        self.assertNotIn("[0:v]", graph.filter_complex)
        self.assertNotIn("overlay=", graph.filter_complex)
        self.assertIn("[0:a]", graph.filter_complex)
        self.assertNotIn("anullsrc", graph.filter_complex)

    def test_muted_video_track_drops_embedded_audio_but_keeps_picture(self):
        project, _ = _video_audio_project(track_muted=True)
        graph = build_filter_graph(project, ProjectSettings())
        self.assertIn("[0:v]", graph.filter_complex)
        self.assertNotIn("[0:a]", graph.filter_complex)
        self.assertIn("anullsrc", graph.filter_complex)

    def test_hidden_and_muted_track_contributes_nothing(self):
        project, _ = _video_audio_project(track_hidden=True, track_muted=True)
        graph = build_filter_graph(project, ProjectSettings())
        self.assertEqual(graph.input_specs, [])

    def test_hidden_track_drops_its_text_overlays(self):
        project, track = _video_audio_project(track_hidden=True)
        track.clips.append(
            Clip(media_type=MediaType.TEXT, source_in=0, source_out=2, timeline_start=5, text_style=TextStyle(content="x"))
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.addCleanup(graph.cleanup)
        self.assertNotIn("drawtext", graph.filter_complex)

    def test_hidden_track_without_audio_registers_no_input(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        project.tracks[0].hidden = True
        project.tracks[0].clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0)
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertEqual(graph.input_specs, [])

    def test_hidden_speed_ramped_clip_keeps_segmented_audio(self):
        project, track = _video_audio_project(track_hidden=True)
        track.clips[0].speed_keyframes = [(0.0, 1.0), (2.0, 2.0)]
        graph = build_filter_graph(project, ProjectSettings())
        self.assertEqual(len(graph.input_specs), 2)  # one per speed segment
        self.assertNotIn(":v]", graph.filter_complex)
        self.assertIn("concat=n=2:v=0:a=1", graph.filter_complex)

    def test_hidden_crossfade_pair_keeps_both_sides_audio(self):
        project, track = _video_audio_project(track_hidden=True)
        track.clips[0].transition_out = TransitionType.CROSSFADE
        track.clips[0].transition_duration = 1.0
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=5)
        )
        graph = build_filter_graph(project, ProjectSettings())
        self.assertNotIn("xfade", graph.filter_complex)
        self.assertIn("[0:a]", graph.filter_complex)
        self.assertIn("[1:a]", graph.filter_complex)
        self.assertIn("amix=inputs=2", graph.filter_complex)


class CrossfadeAudioSyncTests(unittest.TestCase):
    """xfade starts clip B's picture at A.end - d, but B's audio stayed at
    B.timeline_start -- lagging its own video by d for the whole clip."""

    def _pair(self, a_start=0.0, d=1.0):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=True))
        project.add_media_asset(MediaAsset(path="/media/b.mp4", duration=5.0, has_video=True, has_audio=True))
        track = project.tracks[0]
        track.clips.append(
            Clip(
                media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5,
                timeline_start=a_start, transition_out=TransitionType.CROSSFADE, transition_duration=d,
            )
        )
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/b.mp4", source_in=0, source_out=5, timeline_start=a_start + 5)
        )
        return project

    def _audio_chain(self, graph, input_index):
        return next(line for line in graph.filter_complex.split(";") if line.startswith(f"[{input_index}:a]"))

    def test_partner_audio_starts_where_its_video_does(self):
        graph = build_filter_graph(self._pair(a_start=2.0, d=1.0), ProjectSettings())
        self.assertIn("xfade=transition=fade:duration=1.000:offset=4.000", graph.filter_complex)
        # Merged layer is tpad-shifted to A's start (2.0); B's first frame is
        # at offset 4.0 into it -> 6.0 on the timeline, not B's own 7.0.
        self.assertIn("adelay=delays=6000:all=1", self._audio_chain(graph, 1))
        self.assertNotIn("delays=7000", graph.filter_complex)
        self.assertIn("adelay=delays=2000:all=1", self._audio_chain(graph, 0))

    def test_audio_crossfades_over_the_same_overlap(self):
        graph = build_filter_graph(self._pair(a_start=0.0, d=1.0), ProjectSettings())
        self.assertIn("afade=t=out:st=4.000:d=1.000", self._audio_chain(graph, 0))
        self.assertIn("afade=t=in:st=0:d=1.000", self._audio_chain(graph, 1))

    def test_model_clips_are_not_mutated(self):
        project = self._pair(a_start=0.0, d=1.0)
        build_filter_graph(project, ProjectSettings())
        a, b = project.tracks[0].sorted_clips()
        self.assertEqual(b.timeline_start, 5.0)
        self.assertEqual((a.fade_out, b.fade_in), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
