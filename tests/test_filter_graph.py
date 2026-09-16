"""Tests for the FFmpeg filter-graph construction logic (no FFmpeg required).

These tests only check the *structure* of the generated command/graph, not
that FFmpeg can actually execute it — that would require FFmpeg and real
media files. They protect against regressions in the graph-building logic
(labels, escaping, ordering) which is the trickiest part of the exporter.
"""

import math
import unittest

from core.clip import Clip, MediaType, ReframeMode, TextStyle, TransitionType
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

    def test_trim_range_wraps_video_and_audio_output(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=10.0, has_video=True, has_audio=True))
        track = project.tracks[0]
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=10, timeline_start=0)
        )
        graph = build_filter_graph(project, ProjectSettings(), trim_range=(2.0, 7.0))
        self.assertIn(
            f"[{'comp0'}]trim=start=2.000:end=7.000,setpts=PTS-STARTPTS[trimout_v]", graph.filter_complex
        )
        self.assertIn("atrim=start=2.000:end=7.000,asetpts=PTS-STARTPTS[trimout_a]", graph.filter_complex)
        self.assertEqual(graph.video_out_label, "trimout_v")
        self.assertEqual(graph.audio_out_label, "trimout_a")

    def test_trim_end_none_defaults_to_project_duration(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=10.0, has_video=True, has_audio=False))
        track = project.tracks[0]
        track.clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=10, timeline_start=0)
        )
        graph = build_filter_graph(project, ProjectSettings(), trim_range=(1.0, None))
        self.assertIn("trim=start=1.000:end=10.000,setpts=PTS-STARTPTS[trimout_v]", graph.filter_complex)


if __name__ == "__main__":
    unittest.main()
