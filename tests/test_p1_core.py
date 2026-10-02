"""Core (no Qt, no FFmpeg) regression tests for the P1 improvement batch:
mergeable undo (F1), overlap-refused property edits (F2), ramp-aware ripple
trim (R1), overlap-safe adds through History (F8), timeline navigation
(F9), text styling in the graph (B3) and the audio master bus (B4).
"""

import os
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType, TextStyle
from core.commands import (
    AddTrackCommand,
    UpdateClipPropertyCommand,
    build_add_clip_command,
    build_ripple_trim_command,
    property_change_overlaps,
)
from core.exporter import Exporter
from core.filter_graph import MASTER_LIMITER, _drawtext_filter, _Window, build_filter_graph
from core.history import History
from core.media_asset import MediaAsset
from core.project import Project, ProjectSettings
from core.timeline_nav import adjacent_point, edit_points, format_timecode, step_frames
from core.track import TrackType


def _video(start, length=4.0, **kw):
    return Clip(media_type=MediaType.VIDEO, source_path="/m/a.mp4", source_in=0.0, source_out=length,
                timeline_start=start, **kw)


class MergeableHistoryTests(unittest.TestCase):
    def setUp(self):
        self.project = Project()
        self.clip = _video(0.0)
        self.project.tracks[0].clips.append(self.clip)
        self.history = History()

    def _edit(self, value, mergeable=True, attribute="opacity"):
        self.history.execute(UpdateClipPropertyCommand(
            self.project, self.clip.id, attribute, value, mergeable=mergeable))

    def test_consecutive_edits_merge_into_one_step_that_undoes_to_the_start(self):
        for value in (0.9, 0.8, 0.7):
            self._edit(value)
        self.assertEqual(len(self.history._undo_stack), 1)
        self.history.undo()
        self.assertEqual(self.clip.opacity, 1.0)
        self.history.redo()
        self.assertEqual(self.clip.opacity, 0.7)

    def test_end_merge_starts_a_new_step(self):
        self._edit(0.9)
        self.history.end_merge()
        self._edit(0.5)
        self.assertEqual(len(self.history._undo_stack), 2)
        self.history.undo()
        self.assertEqual(self.clip.opacity, 0.9)

    def test_different_attribute_or_expired_window_does_not_merge(self):
        self._edit(0.9)
        self._edit(0.2, attribute="brightness")
        self.assertEqual(len(self.history._undo_stack), 2)
        with patch("core.history.time.monotonic", return_value=1e12):
            self._edit(0.3, attribute="brightness")
        self.assertEqual(len(self.history._undo_stack), 3)

    def test_non_mergeable_commands_never_merge(self):
        self._edit(0.9, mergeable=False)
        self._edit(0.8, mergeable=False)
        self.assertEqual(len(self.history._undo_stack), 2)

    def test_undo_closes_the_merge(self):
        self._edit(0.9)
        self.history.undo()
        self.history.redo()
        self._edit(0.5)
        self.assertEqual(len(self.history._undo_stack), 2)


class OverlapRefusalTests(unittest.TestCase):
    def test_slowing_a_clip_into_its_neighbour_is_detected(self):
        project = Project()
        a, b = _video(0.0), _video(4.0)
        project.tracks[0].clips += [a, b]
        self.assertTrue(property_change_overlaps(project, a.id, "speed", 0.5))
        self.assertFalse(property_change_overlaps(project, a.id, "speed", 2.0))
        self.assertTrue(property_change_overlaps(project, a.id, "speed_keyframes", [(0.0, 1.0), (2.0, 0.5)]))
        self.assertFalse(property_change_overlaps(project, a.id, "opacity", 0.1))


class RampAwareRippleTrimTests(unittest.TestCase):
    def test_ripple_trim_of_a_speed_ramped_clip_shifts_by_the_real_duration_change(self):
        # Reproduction from the report: 6 s ramped clip, next clip at 6.0.
        project = Project()
        a = _video(0.0, speed_keyframes=[(0.0, 1.0), (2.0, 0.5)])
        b = Clip(media_type=MediaType.VIDEO, source_path="/m/b.mp4", source_in=0, source_out=2, timeline_start=6.0)
        project.tracks[0].clips += [a, b]
        self.assertAlmostEqual(a.duration, 6.0)
        build_ripple_trim_command(project, a.id, 0.0, 3.0, 0.0).do()
        self.assertAlmostEqual(a.timeline_end, 4.0)
        self.assertAlmostEqual(b.timeline_start, 4.0)  # was 3.0 (overlapping)
        self.assertFalse(project.tracks[0].has_overlap(b))

    def test_source_timeline_mapping_round_trips_on_a_ramp(self):
        clip = _video(0.0, speed_keyframes=[(0.0, 1.0), (2.0, 0.5)])
        for local in (0.0, 1.0, 2.0, 3.5, 6.0, 7.0):
            self.assertAlmostEqual(clip.timeline_offset_at(clip.source_offset_at(local)), local)
        self.assertEqual(clip.speed_at(1.0), 1.0)
        self.assertEqual(clip.speed_at(3.0), 0.5)


class OverlapSafeAddTests(unittest.TestCase):
    def test_add_track_redo_reuses_the_same_track(self):
        project = Project()
        cmd = AddTrackCommand(project, TrackType.VIDEO)
        cmd.do()
        track_id = cmd.track_id
        cmd.undo()
        cmd.redo()
        self.assertIsNotNone(project.find_track(track_id))

    def test_free_spot_uses_the_preferred_track(self):
        project = Project()
        v1 = project.tracks[0]
        cmd = build_add_clip_command(project, _video(10.0), TrackType.VIDEO, v1.id)
        cmd.do()
        self.assertEqual(len(v1.clips), 1)

    def test_occupied_spot_creates_a_track_in_the_same_undo_step(self):
        project = Project()
        v1 = project.tracks[0]
        v1.clips.append(_video(0.0))
        history = History()
        clip = _video(1.0)
        history.execute(build_add_clip_command(project, clip, TrackType.VIDEO, v1.id))
        self.assertEqual(len(v1.clips), 1)
        new_track = project.find_clip_track(clip.id)
        self.assertIsNot(new_track, v1)
        self.assertFalse(new_track.has_overlap(clip))
        tracks_before = len(project.tracks)
        history.undo()
        self.assertEqual(len(project.tracks), tracks_before - 1)  # no orphan track
        history.redo()
        self.assertIs(project.find_clip_track(clip.id), project.find_track(new_track.id))

    def test_second_existing_track_is_used_before_creating_one(self):
        project = Project()
        v1 = project.tracks[0]
        v1.clips.append(_video(0.0))
        AddTrackCommand(project, TrackType.VIDEO).do()
        count = len(project.tracks)
        clip = _video(1.0)
        build_add_clip_command(project, clip, TrackType.VIDEO, v1.id).do()
        self.assertEqual(len(project.tracks), count)
        self.assertIsNot(project.find_clip_track(clip.id), v1)


class TimelineNavTests(unittest.TestCase):
    def test_timecode_and_frame_steps(self):
        self.assertEqual(format_timecode(3661.5, 30), "01:01:01:15")
        self.assertEqual(format_timecode(0.0, 25), "00:00:00:00")
        self.assertAlmostEqual(step_frames(1.0, 30, 1), 1.0 + 1 / 30)
        self.assertAlmostEqual(step_frames(1.0, 30, -1), 1.0 - 1 / 30)
        self.assertEqual(step_frames(0.0, 30, -5), 0.0)

    def test_edit_point_navigation(self):
        project = Project()
        project.tracks[0].clips += [_video(0.0), _video(6.0)]
        points = edit_points(project)
        self.assertEqual(points, [0.0, 4.0, 6.0, 10.0])
        self.assertEqual(adjacent_point(points, 4.0, 1), 6.0)
        self.assertEqual(adjacent_point(points, 4.0, -1), 0.0)
        self.assertIsNone(adjacent_point(points, 10.0, 1))


class TextStyleGraphTests(unittest.TestCase):
    def _draw(self, style, height=1080):
        clip = Clip(media_type=MediaType.TEXT, source_in=0, source_out=2, text_style=style)
        return _drawtext_filter(style, "C:/tmp/t.txt", clip, _Window(), height)

    def test_alignment_size_scaling_shadow_and_box(self):
        style = TextStyle(font_size=48, outline_width=2, alignment="left", shadow_enabled=True,
                          box_enabled=True, box_opacity=0.25)
        f = self._draw(style, height=2160)
        self.assertIn("fontsize=96", f)
        self.assertIn("borderw=4", f)
        self.assertIn("text_align=L", f)
        self.assertIn("shadowx=6:shadowy=6", f)
        self.assertIn("box=1:boxcolor=#000000@0.25", f)
        self.assertIn("fontsize=48", self._draw(TextStyle(font_size=48)))
        self.assertNotIn("box=1", self._draw(TextStyle()))

    def test_bold_italic_family_resolve_to_a_font_file(self):
        with patch("core.fonts.resolve_font_file", return_value=r"C:\Windows\Fonts\georgiab.ttf") as resolve:
            f = self._draw(TextStyle(font_family="Georgia", bold=True))
        resolve.assert_called_once_with("Georgia", True, False)
        self.assertIn("fontfile=C\\\\:/Windows/Fonts/georgiab.ttf", f)

    def test_old_projects_without_new_fields_load_with_defaults(self):
        style = TextStyle.from_dict({"content": "x", "bold": True})
        self.assertFalse(style.shadow_enabled)
        self.assertFalse(style.box_enabled)
        self.assertEqual(TextStyle.from_dict(style.to_dict()), style)


class AudioMasterBusTests(unittest.TestCase):
    def _project(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/m/song.wav", duration=8.0, has_audio=True))
        project.tracks[1].clips.append(
            Clip(media_type=MediaType.AUDIO, source_path="/m/song.wav", source_in=0, source_out=8))
        return project

    def test_mix_is_limited_and_clips_are_format_normalized(self):
        graph = build_filter_graph(self._project(), ProjectSettings(sample_rate=44100))
        self.assertIn(f"normalize=0,{MASTER_LIMITER}[aout]", graph.filter_complex)
        self.assertIn("aresample=44100,aformat=sample_rates=44100:channel_layouts=stereo", graph.filter_complex)
        self.assertTrue(graph.has_audible_clips)

    def test_audio_only_graph_has_no_picture_layers(self):
        project = self._project()
        project.add_media_asset(MediaAsset(path="/m/v.mp4", duration=4.0, has_video=True, has_audio=True))
        project.tracks[0].clips.append(_video(0.0))
        project.tracks[0].clips[0].source_path = "/m/v.mp4"
        graph = build_filter_graph(project, ProjectSettings(), audio_only=True)
        self.assertNotIn("overlay=", graph.filter_complex)
        self.assertIn("[0:a]", graph.filter_complex)

    def test_loudnorm_json_parsing(self):
        log = ["[Parsed_loudnorm_3 @ 0x1] ", "{", '"input_i" : "-27.61",', '"input_tp" : "-4.47",',
               '"input_lra" : "0.00",', '"input_thresh" : "-37.61",', '"target_offset" : "0.10"', "}",
               "[out#0/null @ 0x2] video:0KiB"]
        measured = Exporter._parse_loudnorm_json(log)
        self.assertEqual(measured["input_i"], "-27.61")
        self.assertIsNone(Exporter._parse_loudnorm_json(["no json here"]))


if __name__ == "__main__":
    unittest.main()
