"""Core tests for the fourth round: reverse / freeze frame, subtitles, level meters, GPU decoding.

No FFmpeg needed (tests/integration/test_p4_real_ffmpeg.py renders the
same features for real). Covers the clip time mapping on reversed/frozen
clips, every edit that changes a clip's range (split, trims, roll, ripple,
undo), the filter graph they produce, project files, the SRT/WebVTT
parser, waveform RMS levels and the hardware-decode fallback plumbing.
"""

from __future__ import annotations

import json
import os
import struct
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType
from core.commands import (
    SplitClipCommand,
    TrimClipCommand,
    UpdateClipPropertyCommand,
    build_ripple_trim_command,
    property_change_overlaps,
)
from core.edit_ops import build_roll_command, build_slide_command
from core.ffmpeg_utils import (
    DECODE_HWACCEL_ENV,
    apply_decode_hwaccel,
    decode_hwaccel_modes,
    externalize_filter_graph,
    is_hw_decode_failure,
)
from core.filter_graph import build_filter_graph
from core.history import History
from core.media_asset import MediaAsset
from core.project import Project, ProjectSettings
from core.subtitles import (
    SubtitleParseError,
    cues_to_text_clips,
    is_subtitle_path,
    parse_subtitle_file,
    parse_subtitle_text,
)
from core.track import TrackType
from core.waveform import _pcm_bytes_to_levels, load_waveform, window_levels


def _video(start=0.0, src_in=0.0, src_out=4.0, **kw):
    return Clip(media_type=MediaType.VIDEO, source_path="/m/a.mp4", source_in=src_in, source_out=src_out,
                timeline_start=start, source_duration=20.0, **kw)


def _project(*clips, has_audio=True):
    project = Project()
    project.settings = ProjectSettings(width=320, height=180, fps=30.0)
    project.add_media_asset(MediaAsset(path="/m/a.mp4", duration=20.0, width=320, height=180,
                                       has_video=True, has_audio=has_audio))
    project.tracks[0].clips.extend(clips)
    return project


def _graph(project, **kw):
    return build_filter_graph(project, project.settings, **kw).filter_complex


# ---------------------------------------------------------------------------
# Time mapping
# ---------------------------------------------------------------------------
class ReverseFreezeMappingTests(unittest.TestCase):
    def test_duration_includes_freeze_and_playable_excludes_it(self):
        clip = _video(src_in=1.0, src_out=5.0, speed=2.0, freeze_frame_duration=1.5)
        self.assertAlmostEqual(clip.duration, 3.5)
        self.assertAlmostEqual(clip.playable_duration, 2.0)

    def test_source_time_at_forward_reverse_and_freeze(self):
        fwd = _video(src_in=1.0, src_out=5.0, freeze_frame_duration=1.0)
        self.assertAlmostEqual(fwd.source_time_at(0.0), 1.0)
        self.assertAlmostEqual(fwd.source_time_at(3.0), 4.0)
        self.assertAlmostEqual(fwd.source_time_at(4.5), 5.0)  # freeze holds the last frame
        rev = _video(src_in=1.0, src_out=5.0, reverse=True, freeze_frame_duration=1.0)
        self.assertAlmostEqual(rev.source_time_at(0.0), 5.0)
        self.assertAlmostEqual(rev.source_time_at(3.0), 2.0)
        self.assertAlmostEqual(rev.source_time_at(4.5), 1.0)  # reversed: holds source_in
        self.assertTrue(rev.in_freeze_frame(4.0))
        self.assertFalse(rev.in_freeze_frame(3.9))

    def test_reverse_walks_speed_segments_back_to_front(self):
        # Source 0..2 at 2x (1 s on screen), 2..4 at 1x (2 s). Reversed, the
        # 1x half plays first: after 1 s, 1 source second has been consumed.
        clip = _video(src_out=4.0, reverse=True, speed_keyframes=[(0.0, 2.0), (2.0, 1.0)])
        self.assertAlmostEqual(clip.duration, 3.0)
        self.assertAlmostEqual(clip.source_time_at(1.0), 3.0)
        self.assertAlmostEqual(clip.source_time_at(2.5), 1.0)
        self.assertAlmostEqual(clip.playback_timeline_offset(clip.playback_source_consumed(2.5)), 2.5)

    def test_source_offset_at_still_extrapolates_without_freeze(self):  # the user's fix, kept
        clip = _video(src_out=4.0)
        self.assertAlmostEqual(clip.source_offset_at(6.0), 6.0)
        frozen = _video(src_out=4.0, freeze_frame_duration=1.0)
        self.assertAlmostEqual(frozen.source_offset_at(4.5), 4.0)


# ---------------------------------------------------------------------------
# Edits
# ---------------------------------------------------------------------------
class ReverseFreezeEditTests(unittest.TestCase):
    def test_split_of_a_reversed_clip_keeps_the_right_source_ranges(self):
        clip = _video(start=10.0, src_in=2.0, src_out=8.0, reverse=True)
        project = _project(clip)
        history = History(validator=project.validate)
        history.execute(SplitClipCommand(project, clip.id, 12.0))
        first, second = sorted(project.tracks[0].clips, key=lambda c: c.timeline_start)
        # Plays 8 -> 2: the first 2 s on screen are source 6..8, the rest 2..6.
        self.assertEqual((first.source_in, first.source_out), (6.0, 8.0))
        self.assertEqual((second.source_in, second.source_out), (2.0, 6.0))
        self.assertTrue(first.reverse and second.reverse)
        self.assertAlmostEqual(first.timeline_end, 12.0)
        self.assertAlmostEqual(second.timeline_end, 16.0)
        history.undo()
        self.assertEqual(len(project.tracks[0].clips), 1)
        self.assertEqual((project.tracks[0].clips[0].source_in, project.tracks[0].clips[0].source_out), (2.0, 8.0))

    def test_split_of_a_reversed_speed_ramp_moves_keyframes_to_the_right_half(self):
        clip = _video(src_out=4.0, reverse=True, speed_keyframes=[(0.0, 2.0), (2.0, 1.0)])
        project = _project(clip)
        History().execute(SplitClipCommand(project, clip.id, 1.0))  # cut 1 s in: source 3.0
        first, second = sorted(project.tracks[0].clips, key=lambda c: c.timeline_start)
        self.assertEqual((first.source_in, first.source_out), (3.0, 4.0))
        self.assertEqual((second.source_in, second.source_out), (0.0, 3.0))
        self.assertAlmostEqual(first.speed, 1.0)  # source 3..4 was the 1x part
        self.assertEqual(second.speed_keyframes, [(0.0, 2.0), (2.0, 1.0)])
        self.assertAlmostEqual(first.duration + second.duration, 3.0)

    def test_split_moves_the_freeze_to_the_second_half_and_refuses_inside_it(self):
        clip = _video(src_out=4.0, freeze_frame_duration=2.0)
        project = _project(clip)
        History().execute(SplitClipCommand(project, clip.id, 1.0))
        first, second = sorted(project.tracks[0].clips, key=lambda c: c.timeline_start)
        self.assertEqual(first.freeze_frame_duration, 0.0)
        self.assertEqual(second.freeze_frame_duration, 2.0)
        self.assertAlmostEqual(first.timeline_end, 1.0)  # used to keep the freeze: overlapped the second half
        self.assertAlmostEqual(second.timeline_end, 6.0)
        History().execute(SplitClipCommand(project, second.id, 5.0))  # inside the freeze: no-op
        self.assertEqual(len(project.tracks[0].clips), 2)

    def test_trim_command_carries_the_freeze_and_undoes_it(self):
        clip = _video(src_out=4.0, freeze_frame_duration=2.0)
        project = _project(clip)
        history = History(validator=project.validate)
        history.execute(TrimClipCommand(project, clip.id, 0.0, 4.0, 0.0, freeze_frame_duration=0.5))
        self.assertAlmostEqual(clip.duration, 4.5)
        history.undo()
        self.assertAlmostEqual(clip.freeze_frame_duration, 2.0)

    def test_right_trim_eats_the_freeze_first_then_footage(self):
        clip = _video(src_out=4.0, freeze_frame_duration=2.0)
        self.assertEqual(clip.trim_right_range(5.0), (0.0, 4.0, 1.0))
        self.assertEqual(clip.trim_right_range(3.0), (0.0, 3.0, 0.0))
        rev = _video(src_in=2.0, src_out=8.0, reverse=True)
        self.assertEqual(rev.trim_right_range(4.0), (4.0, 8.0, 0.0))  # right edge = source_in end
        self.assertEqual(rev.trim_left_range(1.0), (2.0, 7.0))  # left edge = source_out end

    def test_roll_between_reversed_clips_trims_the_correct_ends(self):
        a = _video(start=0.0, src_in=2.0, src_out=6.0, reverse=True)
        b = _video(start=4.0, src_in=10.0, src_out=14.0, reverse=True)
        project = _project(a, b)
        history = History(validator=project.validate)
        history.execute(build_roll_command(project, a.id, 3.0))
        self.assertEqual((a.source_in, a.source_out), (3.0, 6.0))  # a's tail (source_in end) cut
        self.assertEqual((b.source_in, b.source_out), (10.0, 15.0))  # b extended at its source_out end
        self.assertAlmostEqual(a.timeline_end, 3.0)
        self.assertAlmostEqual(b.timeline_start, 3.0)

    def test_roll_into_a_freeze_frame_changes_only_the_freeze(self):
        a = _video(start=0.0, src_out=4.0, freeze_frame_duration=2.0)
        b = _video(start=6.0, src_in=5.0, src_out=9.0)
        project = _project(a, b)
        History(validator=project.validate).execute(build_roll_command(project, a.id, 5.0))
        self.assertEqual((a.source_in, a.source_out, a.freeze_frame_duration), (0.0, 4.0, 1.0))
        self.assertAlmostEqual(b.timeline_start, 5.0)
        self.assertAlmostEqual(b.source_in, 4.0)

    def test_slide_past_a_frozen_neighbour(self):
        a = _video(start=0.0, src_out=4.0, freeze_frame_duration=1.0)
        b = _video(start=5.0, src_in=5.0, src_out=7.0)
        c = _video(start=7.0, src_in=1.0, src_out=5.0)
        project = _project(a, b, c)
        History(validator=project.validate).execute(build_slide_command(project, b.id, -0.5))
        self.assertAlmostEqual(a.freeze_frame_duration, 0.5)
        self.assertAlmostEqual(b.timeline_start, 4.5)

    def test_ripple_trim_counts_the_freeze(self):
        a = _video(start=0.0, src_out=4.0, freeze_frame_duration=2.0)
        b = _video(start=6.0)
        project = _project(a, b)
        History(validator=project.validate).execute(
            build_ripple_trim_command(project, a.id, 0.0, 4.0, 0.0, freeze_frame_duration=0.0))
        self.assertAlmostEqual(b.timeline_start, 4.0)

    def test_longer_freeze_into_the_next_clip_is_refused(self):
        a = _video(start=0.0, src_out=4.0)
        project = _project(a, _video(start=5.0))
        self.assertTrue(property_change_overlaps(project, a.id, "freeze_frame_duration", 2.0))
        self.assertFalse(property_change_overlaps(project, a.id, "freeze_frame_duration", 1.0))

    def test_left_trim_of_a_reversed_clip_rebases_timeline_keyframes(self):
        clip = _video(src_in=0.0, src_out=4.0, reverse=True, opacity_keyframes=[(0.0, 0.0), (4.0, 1.0)])
        trimmed = clip.with_source_range(0.0, 3.0)  # 1 s cut from the playback start
        self.assertEqual(trimmed.opacity_keyframes[0], (0.0, 0.25))


# ---------------------------------------------------------------------------
# Filter graph
# ---------------------------------------------------------------------------
class ReverseFreezeGraphTests(unittest.TestCase):
    def test_reverse_reverses_picture_and_sound(self):
        graph = _graph(_project(_video(reverse=True)))
        self.assertIn("setpts=PTS-STARTPTS,reverse", graph)
        self.assertIn(",areverse", graph)

    def test_freeze_pads_video_before_fades_and_leaves_audio_silent(self):
        graph = _graph(_project(_video(src_out=2.0, freeze_frame_duration=1.0, fade_out=0.5)))
        self.assertIn("tpad=stop_mode=clone:stop_duration=1.000,fade=t=out:st=2.500", graph)
        self.assertNotIn("apad", graph)
        self.assertIn("afade=t=out:st=1.500", graph)  # fades with the sound, which ends at 2 s

    def test_speed_ramp_reverse_orders_segments_back_to_front(self):
        clip = _video(src_out=4.0, reverse=True, speed_keyframes=[(0.0, 2.0), (2.0, 1.0)])
        result = build_filter_graph(_project(clip), ProjectSettings(width=320, height=180, fps=30.0))
        first_seg = next(line for line in result.filter_complex.split(";") if line.endswith("[sr0v0]"))
        self.assertTrue(first_seg.startswith("[1:v]"), first_seg)  # input 1 = source 2..4
        self.assertIn(",reverse", first_seg)
        self.assertIn(",areverse", result.filter_complex)

    def test_windowed_reverse_seeks_the_mirrored_source_range(self):
        clip = _video(src_in=2.0, src_out=8.0, reverse=True)
        result = build_filter_graph(_project(clip), ProjectSettings(width=320, height=180, fps=30.0),
                                    trim_range=(1.0, 3.0))
        # Clip-local 1..3 plays source 7 -> 5, so the input reads 5..7.
        self.assertEqual(result.input_specs[0].pre_args[:2], ["-ss", "5.000"])
        self.assertIn("trim=duration=2.000,setpts=PTS-STARTPTS,reverse", result.filter_complex)

    def test_window_inside_the_freeze_reads_the_last_frame_and_trims_after(self):
        clip = _video(src_in=0.0, src_out=2.0, freeze_frame_duration=2.0)
        result = build_filter_graph(_project(clip), ProjectSettings(width=320, height=180, fps=30.0),
                                    trim_range=(3.0, 3.1))
        self.assertEqual(result.input_specs[0].pre_args[:2], ["-ss", "1.967"])
        self.assertIn("trim=start=3.000000,setpts=PTS-STARTPTS", result.filter_complex)
        self.assertIn("atrim=start=3.000000", result.filter_complex)

    def test_reverse_on_an_audio_track_clip(self):
        project = _project()
        audio = Clip(media_type=MediaType.AUDIO, source_path="/m/a.mp4", source_out=3.0, reverse=True)
        next(t for t in project.tracks if t.track_type == TrackType.AUDIO).clips.append(audio)
        self.assertIn(",areverse", _graph(project))


# ---------------------------------------------------------------------------
# Project files
# ---------------------------------------------------------------------------
class ReverseFreezeSerializationTests(unittest.TestCase):
    def test_round_trip_and_old_files_default_to_off(self):
        clip = _video(reverse=True, freeze_frame_duration=1.25)
        project = _project(clip)
        path = os.path.join(tempfile.mkdtemp(prefix="ve_p4_"), "p.vproj")
        project.save(path)
        loaded = Project.load(path).find_clip(clip.id)
        self.assertTrue(loaded.reverse)
        self.assertAlmostEqual(loaded.freeze_frame_duration, 1.25)
        data = clip.to_dict()
        del data["reverse"], data["freeze_frame_duration"]  # a file from before these fields
        old = Clip.from_dict(data)
        self.assertFalse(old.reverse)
        self.assertEqual(old.freeze_frame_duration, 0.0)
        self.assertEqual(Clip.from_dict({**data, "freeze_frame_duration": -3}).freeze_frame_duration, 0.0)

    def test_reverse_toggle_is_undoable(self):
        clip = _video()
        project = _project(clip)
        history = History()
        history.execute(UpdateClipPropertyCommand(project, clip.id, "reverse", True))
        self.assertTrue(clip.reverse)
        history.undo()
        self.assertFalse(clip.reverse)


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------
SRT = """1
00:00:01,000 --> 00:00:02,500
Hello <i>world</i>

2
00:00:03,000 --> 00:00:04,000 X1:10 X2:20
Line one
Line two
"""

VTT = """WEBVTT - Episode 1
Kind: captions

NOTE this is a comment

intro
00:01.000 --> 00:02.000 align:start
{\\an8}Caf&eacute; <v Ann>time</v>

00:00:03.500 --> 00:00:05.000
Second
"""


class SubtitleTests(unittest.TestCase):
    def test_srt(self):
        cues = parse_subtitle_text(SRT)
        self.assertEqual([(c.start, c.end, c.text) for c in cues],
                         [(1.0, 2.5, "Hello world"), (3.0, 4.0, "Line one\nLine two")])

    def test_webvtt_variants(self):
        cues = parse_subtitle_text(VTT)
        self.assertEqual([(c.start, c.end, c.text) for c in cues], [(1.0, 2.0, "Café time"), (3.5, 5.0, "Second")])

    def test_blank_line_inside_a_cue_is_skipped_but_garbage_is_an_error(self):
        cues = parse_subtitle_text("1\n00:00:01,000 --> 00:00:02,000\nA\n\nstray text\n\n2\n00:00:03,000 --> 00:00:04,000\nB\n")
        self.assertEqual([c.text for c in cues], ["A", "B"])
        with self.assertRaises(SubtitleParseError):
            parse_subtitle_text("this is not a subtitle file")
        with self.assertRaises(SubtitleParseError):
            parse_subtitle_text("1\n00:00:01 --> 00:00:02,000\nbad timestamp\n")

    def test_non_utf8_and_utf16_files(self):
        tmp = tempfile.mkdtemp(prefix="ve_srt_")
        path = os.path.join(tmp, "cp1252.srt")
        with open(path, "wb") as f:
            f.write("1\r\n00:00:01,000 --> 00:00:02,000\r\nNiño\r\n".encode("cp1252"))
        self.assertEqual(parse_subtitle_file(path)[0].text, "Niño")
        path16 = os.path.join(tmp, "utf16.vtt")
        with open(path16, "wb") as f:
            f.write("WEBVTT\n\n00:01.000 --> 00:02.000\nÜber\n".encode("utf-16"))
        self.assertEqual(parse_subtitle_file(path16)[0].text, "Über")
        self.assertTrue(is_subtitle_path("A.SRT") and is_subtitle_path("b.vtt") and not is_subtitle_path("c.ass"))

    def test_cues_become_non_overlapping_text_clips(self):
        cues = parse_subtitle_text(
            "1\n00:00:01,000 --> 00:00:03,000\nA\n\n2\n00:00:02,000 --> 00:00:04,000\nB\n\n"
            "3\n00:00:02,000 --> 00:00:02,500\nC\n"
        )
        clips = cues_to_text_clips(cues)
        self.assertEqual([(c.timeline_start, c.timeline_end, c.text_style.content) for c in clips],
                         [(1.0, 2.0, "A"), (2.0, 4.0, "B\nC")])
        self.assertTrue(all(c.media_type == MediaType.TEXT for c in clips))

    def test_subtitle_clips_export_as_drawtext_and_survive_a_save(self):
        project = _project()
        track = project.add_track(TrackType.VIDEO, name="Subtitles") if hasattr(project, "add_track") else None
        if track is None:
            from core.track import Track

            track = Track(track_type=TrackType.VIDEO, name="Subtitles")
            project.tracks.append(track)
        track.clips.extend(cues_to_text_clips(parse_subtitle_text(SRT)))
        result = build_filter_graph(project, project.settings)
        try:
            self.assertEqual(result.filter_complex.count("drawtext=textfile="), 2)
            self.assertIn("enable='between(t,1.000,2.500)'", result.filter_complex)
        finally:
            result.cleanup()
        path = os.path.join(tempfile.mkdtemp(prefix="ve_p4_"), "subs.vproj")
        project.save(path)
        loaded = Project.load(path)
        texts = [c.text_style.content for t in loaded.tracks for c in t.clips if c.media_type == MediaType.TEXT]
        self.assertEqual(texts, ["Hello world", "Line one\nLine two"])


# ---------------------------------------------------------------------------
# Waveform levels (meters)
# ---------------------------------------------------------------------------
class WaveformLevelTests(unittest.TestCase):
    def test_rms_of_a_square_wave_equals_its_peak_and_silence_is_zero(self):
        samples = [16384, -16384] * 40 + [0] * 80  # one bucket loud, one silent (40 samples/bucket)
        peaks, rms = _pcm_bytes_to_levels(struct.pack(f"<{len(samples)}h", *samples))
        self.assertEqual(peaks, [0.5, 0.5, 0.0, 0.0])
        self.assertEqual(rms, [0.5, 0.5, 0.0, 0.0])

    def test_window_levels(self):
        data = ([0.0] * 100 + [0.8] * 100, [0.0] * 100 + [0.4] * 100, 100.0)
        self.assertEqual(window_levels(data, 0.3), (0.0, 0.0))
        peak, rms = window_levels(data, 1.5)
        self.assertAlmostEqual(peak, 0.8)
        self.assertAlmostEqual(rms, 0.4)
        self.assertEqual(window_levels(([], [], 100.0), 1.0), (0.0, 0.0))
        self.assertEqual(window_levels(data, 99.0), (0.0, 0.0))  # past the end

    def test_old_cache_without_rms_still_loads(self):
        path = os.path.join(tempfile.mkdtemp(prefix="ve_wf_"), "old.waveform.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"peaks_per_second": 25, "peaks": [0.1, 0.9]}, f)
        self.assertEqual(load_waveform(path), ([0.1, 0.9], [0.1, 0.9], 25.0))

    def test_waveform_decode_never_tries_gpu_decoding(self):
        from core import waveform

        calls = []

        def fake_run(args, **kw):
            calls.append(args)

            class R:
                returncode, stdout = 0, struct.pack("<4h", 1, 2, 3, 4)
            return R()

        tmp = tempfile.mkdtemp(prefix="ve_wf_")
        with patch.dict(os.environ, {DECODE_HWACCEL_ENV: "auto"}), \
                patch("core.waveform.find_binary", return_value="ffmpeg"), \
                patch("core.waveform.run_binary", fake_run):
            self.assertIsNotNone(waveform.generate_waveform(os.path.join(tmp, "a.wav"), tmp))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("-hwaccel", calls[0])


# ---------------------------------------------------------------------------
# GPU decoding plumbing
# ---------------------------------------------------------------------------
class HwDecodeTests(unittest.TestCase):
    def test_modes_always_end_in_software(self):
        for value, expected_first in (("", None), ("off", None), ("bogus", None), ("cuda", "cuda"),
                                      ("d3d11va", "d3d11va"), ("auto", None)):
            with patch.dict(os.environ, {DECODE_HWACCEL_ENV: value}):
                modes = decode_hwaccel_modes()
            self.assertIsNone(modes[-1], value)
            if value != "auto":
                self.assertEqual(modes[0], expected_first, value)
        with patch.dict(os.environ, {DECODE_HWACCEL_ENV: "auto"}):
            self.assertGreaterEqual(len(decode_hwaccel_modes()), 2)

    def test_hwaccel_goes_before_every_real_input(self):
        args = ["ffmpeg", "-y", "-ss", "1", "-i", "a.mp4", "-f", "lavfi", "-i", "color=c=black", "-i", "b.png", "out.mp4"]
        out = apply_decode_hwaccel(args, "cuda")
        self.assertEqual(out, ["ffmpeg", "-y", "-ss", "1", "-hwaccel", "cuda", "-i", "a.mp4", "-f", "lavfi", "-i",
                               "color=c=black", "-hwaccel", "cuda", "-i", "b.png", "out.mp4"])
        self.assertEqual(apply_decode_hwaccel(args, None), args)

    def test_decoder_vs_encoder_failures(self):
        self.assertTrue(is_hw_decode_failure(["Device creation failed: -5.", "No device available for decoder"]))
        self.assertFalse(is_hw_decode_failure(["[h264_nvenc] OpenEncodeSession failed: out of memory"]))
        self.assertFalse(is_hw_decode_failure(["No space left on device"]))

    def test_long_filter_graphs_go_through_a_file(self):
        short = ["ffmpeg", "-filter_complex", "[0:v]null[v]", "out.mp4"]
        self.assertEqual(externalize_filter_graph(short), (short, None))
        long_args = ["ffmpeg", "-filter_complex", "x" * 9000, "-map", "[v]"]
        out, path = externalize_filter_graph(long_args)
        try:
            self.assertEqual(out[:2], ["ffmpeg", "-/filter_complex"])
            self.assertEqual(out[3:], ["-map", "[v]"])
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "x" * 9000)
        finally:
            os.remove(path)


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


class _FakeProcess:
    def __init__(self, returncode, lines):
        self.returncode = returncode
        self.stdout = _FakeStdout(lines)

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass


class ExporterHwDecodeFallbackTests(unittest.TestCase):
    def _exporter(self):
        from core.exporter import Exporter

        return Exporter(Project())

    def _run(self, outcomes, env="d3d11va"):
        """Run Exporter._run_ffmpeg with fake processes; returns (calls, progress, error)."""
        calls, progress = [], []

        def fake_popen(args, **kw):
            calls.append(args)
            return outcomes[len(calls) - 1]

        error = None
        with patch.dict(os.environ, {DECODE_HWACCEL_ENV: env}), patch("core.exporter.popen", fake_popen):
            try:
                self._exporter()._run_ffmpeg(["ffmpeg", "-i", "a.mp4", "out.mp4"], 2.0,
                                             lambda f, m: progress.append((f, m)), (0.0, 1.0), "Rendering")
            except Exception as exc:  # noqa: BLE001
                error = exc
        return calls, progress, error

    def test_early_hw_failure_retries_in_software(self):
        calls, progress, error = self._run([
            _FakeProcess(1, ["Device creation failed: -5."]),
            _FakeProcess(0, ["out_time_ms=2000000"]),
        ])
        self.assertIsNone(error)
        self.assertIn("-hwaccel", calls[0])
        self.assertNotIn("-hwaccel", calls[1])
        self.assertTrue(any("retrying with software decoding" in m for _f, m in progress))
        self.assertEqual(progress[-1][0], 1.0)

    def test_media_failure_after_progress_is_not_retried(self):
        calls, _progress, error = self._run([_FakeProcess(1, ["out_time_ms=1000000", "No space left on device"])])
        self.assertEqual(len(calls), 1)
        self.assertIn("No space left on device", error.log_tail)

    def test_nvenc_failure_is_left_to_the_encoder_fallback(self):
        calls, _progress, error = self._run([_FakeProcess(1, ["out_time_ms=500000", "[h264_nvenc] nvenc error"])])
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(error)

    def test_software_only_by_default(self):
        calls, _progress, error = self._run([_FakeProcess(0, [])], env="off")
        self.assertIsNone(error)
        self.assertEqual(calls, [["ffmpeg", "-i", "a.mp4", "out.mp4"]])

    def test_nvenc_to_cpu_fallback_still_works_with_gpu_decoding_on(self):
        from core.exporter import ExportSettings, Exporter

        clip = _video(src_out=1.0)
        project = _project(clip, has_audio=False)
        out = os.path.join(tempfile.mkdtemp(prefix="ve_p4_"), "o.mp4")
        calls = []

        def fake_popen(args, **kw):
            calls.append(args)
            if "h264_nvenc" in args:
                return _FakeProcess(1, ["[h264_nvenc] OpenEncodeSession failed"])
            open(args[-1], "wb").close()
            return _FakeProcess(0, ["out_time_ms=1000000"])

        with patch.dict(os.environ, {DECODE_HWACCEL_ENV: "cuda"}), \
                patch("core.exporter.find_binary", return_value="ffmpeg"), \
                patch("core.exporter.popen", fake_popen), \
                patch("core.exporter.nvenc_available", return_value=True), \
                patch("core.exporter.nvenc_encoder_available", return_value=True), \
                patch("os.path.isfile", return_value=True):
            Exporter(project).export(ExportSettings(output_path=out, use_gpu=True))
        encoders = ["h264_nvenc" if "h264_nvenc" in c else "cpu" for c in calls]
        self.assertEqual(encoders, ["h264_nvenc", "h264_nvenc", "cpu"])  # cuda, software; then CPU codec
        self.assertIn("-hwaccel", calls[-1])  # the CPU encode still decodes on the GPU first


if __name__ == "__main__":
    unittest.main()
