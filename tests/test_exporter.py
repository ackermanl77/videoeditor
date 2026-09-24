"""Tests for core/exporter.py's pure argument/file-construction logic.

core/exporter.py otherwise has no unit tests, by design — actually running
FFmpeg end-to-end belongs to manual/real-execution verification (see
docs/PROJECT_STATUS.md and the workflow docs), not this suite. But some of
its logic — chapter-file construction, and how the FFmpeg command line for
each pass is assembled — is pure text/argument construction that doesn't
require executing FFmpeg to verify, the same category `core/filter_graph.py`
tests already cover. These tests exist because a real, previously-undetected
bug (two-pass export unconditionally crashing — see TwoPassArgsTests below)
was found here, and a test like these would have caught it immediately.
"""

import os
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType, ReframeMode
from core.exporter import ExportSettings, Exporter, NVENC_CODEC_MAP
from core.media_asset import MediaAsset
from core.project import Project


class ChaptersFileTests(unittest.TestCase):
    def test_no_markers_produces_no_chapters_args(self):
        project = Project()
        exporter = Exporter(project)
        from core.filter_graph import build_filter_graph
        from core.project import ProjectSettings

        graph = build_filter_graph(project, ProjectSettings())
        args, index, path = exporter._build_chapters_args(graph, total_duration=10.0)
        self.assertEqual(args, [])
        self.assertIsNone(index)
        self.assertIsNone(path)

    def test_markers_produce_a_valid_ffmetadata_file(self):
        project = Project()
        project.add_marker(0.0, "Intro")
        project.add_marker(2.5, "Main")
        exporter = Exporter(project)

        path = exporter._write_chapters_file(total_duration=6.0)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        finally:
            os.remove(path)

        self.assertTrue(content.startswith(";FFMETADATA1"))
        self.assertIn("START=0", content)
        self.assertIn("END=2500", content)  # Intro spans to the next marker
        self.assertIn("title=Intro", content)
        self.assertIn("START=2500", content)
        self.assertIn("END=6000", content)  # Main spans to the project's end
        self.assertIn("title=Main", content)

    def test_special_characters_in_marker_names_are_escaped(self):
        project = Project()
        project.add_marker(0.0, "Weird=Name;#test\\end")
        exporter = Exporter(project)

        path = exporter._write_chapters_file(total_duration=5.0)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        finally:
            os.remove(path)

        # Every FFMETADATA metacharacter must be backslash-escaped so the
        # rest of the name doesn't get parsed as a new key/comment.
        self.assertIn("title=Weird\\=Name\\;\\#test\\\\end", content)

    def test_trim_range_shifts_and_clips_chapters(self):
        # A Shorts export trimming the output to [3, 8) should re-anchor
        # chapter times to that window (0-based in the trimmed output), not
        # the untrimmed project's own timeline.
        project = Project()
        project.add_marker(0.0, "Intro")
        project.add_marker(4.0, "Middle")
        project.add_marker(9.0, "Outro")
        exporter = Exporter(project)

        path = exporter._write_chapters_file(total_duration=12.0, trim_start=3.0, trim_end=8.0)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        finally:
            os.remove(path)

        # "Intro" (0-4) overlaps [3, 8) as [3, 4) -> re-anchored to [0, 1000ms).
        self.assertIn("title=Intro", content)
        self.assertIn("START=0\nEND=1000", content)
        # "Middle" (4-9) overlaps [3, 8) as [4, 8) -> re-anchored to [1000, 5000ms).
        self.assertIn("title=Middle", content)
        self.assertIn("START=1000\nEND=5000", content)
        # "Outro" starts at 9, entirely after the trim window -> dropped.
        self.assertNotIn("title=Outro", content)

    def test_chapter_entirely_before_trim_range_is_dropped(self):
        project = Project()
        project.add_marker(0.0, "TooEarly")  # spans (0, 2) -- entirely before the trim start
        project.add_marker(2.0, "Kept")
        exporter = Exporter(project)

        path = exporter._write_chapters_file(total_duration=10.0, trim_start=4.0, trim_end=10.0)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        finally:
            os.remove(path)

        self.assertNotIn("TooEarly", content)
        self.assertIn("title=Kept", content)

    def test_build_chapters_args_uses_the_next_available_input_index(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        project.tracks[0].clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0)
        )
        project.add_marker(1.0, "Point")
        exporter = Exporter(project)

        from core.filter_graph import build_filter_graph
        from core.project import ProjectSettings

        graph = build_filter_graph(project, ProjectSettings())
        args, index, path = exporter._build_chapters_args(graph, total_duration=5.0)
        try:
            self.assertEqual(index, len(graph.input_specs))  # right after the real inputs
            self.assertEqual(args, ["-f", "ffmetadata", "-i", path])
        finally:
            os.remove(path)


class _FakeStdout:
    """A closeable, iterable stand-in for a Popen's stdout pipe."""

    def __iter__(self):
        return iter(())  # no progress lines

    def close(self):
        pass


class _FakeProcess:
    """Stands in for subprocess.Popen without spawning anything real."""

    def __init__(self):
        self.stdout = _FakeStdout()
        self.returncode = 0

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass


class TwoPassArgsTests(unittest.TestCase):
    """Regression test for a real bug: two-pass export's first pass never
    mapped the base filter graph's always-present audio output pad, which
    makes FFmpeg refuse to run at all ("... has output ... unconnected") —
    for *every* project, not just ones with markers. Caught while manually
    verifying that chapter embedding didn't interfere with two-pass export.
    """

    def test_pass_one_sinks_the_unused_audio_pad(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
        project.tracks[0].clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0)
        )

        captured_args = []

        def fake_popen(args, **kwargs):
            captured_args.append(list(args))
            return _FakeProcess()

        with patch("core.exporter.find_binary", return_value="ffmpeg"), patch("core.exporter.popen", fake_popen):
            exporter = Exporter(project)
            settings = ExportSettings(
                output_path="/tmp/out.mp4", width=320, height=240, fps=25.0,
                two_pass=True, target_size_mb=1.0,
            )
            exporter.export(settings)

        self.assertEqual(len(captured_args), 2)  # pass 1, then pass 2
        pass1_args = captured_args[0]
        filter_index = pass1_args.index("-filter_complex") + 1
        self.assertIn("anullsink", pass1_args[filter_index])
        self.assertIn("-an", pass1_args)


class ShortsExportSettingsTests(unittest.TestCase):
    """ExportSettings.reframe_override/trim_start/trim_end must actually
    reach build_filter_graph() and shape the real FFmpeg command line, not
    just exist as unused dataclass fields.
    """

    def test_reframe_override_and_trim_reach_the_filter_graph_and_command_line(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=10.0, has_video=True, has_audio=False))
        project.tracks[0].clips.append(
            Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=10, timeline_start=0)
        )

        captured_args = []

        def fake_popen(args, **kwargs):
            captured_args.append(list(args))
            return _FakeProcess()

        with patch("core.exporter.find_binary", return_value="ffmpeg"), patch("core.exporter.popen", fake_popen):
            exporter = Exporter(project)
            settings = ExportSettings(
                output_path="/tmp/out.mp4", width=1080, height=1920, fps=30.0,
                reframe_override=ReframeMode.FILL, trim_start=0.0, trim_end=6.0,
            )
            exporter.export(settings)

        self.assertEqual(len(captured_args), 1)  # single-pass export
        args = captured_args[0]
        filter_index = args.index("-filter_complex") + 1
        filter_complex = args[filter_index]
        self.assertIn("force_original_aspect_ratio=increase", filter_complex)
        self.assertIn("trim=start=0.000:end=6.000,setpts=PTS-STARTPTS", filter_complex)
        # The trimmed labels must be what actually gets mapped to the output.
        self.assertIn("-map", args)
        map_index = args.index("-map")
        self.assertIn(args[map_index + 1], ("[trimout_v]",))


class ResolveVideoCodecTests(unittest.TestCase):
    """Exporter._resolve_video_codec() is the pure decision logic behind GPU
    (NVENC) selection -- tested directly, independent of nvenc_available()'s
    own real probe, by patching it.
    """

    def test_prefers_nvenc_when_available_and_requested(self):
        with patch("core.exporter.nvenc_available", return_value=True):
            codec, is_gpu = Exporter._resolve_video_codec("libx264", prefer_gpu=True)
        self.assertEqual(codec, "h264_nvenc")
        self.assertTrue(is_gpu)

    def test_falls_back_to_cpu_when_nvenc_unavailable(self):
        with patch("core.exporter.nvenc_available", return_value=False):
            codec, is_gpu = Exporter._resolve_video_codec("libx264", prefer_gpu=True)
        self.assertEqual(codec, "libx264")
        self.assertFalse(is_gpu)

    def test_cpu_codec_used_when_gpu_not_requested(self):
        # nvenc_available() isn't even consulted when prefer_gpu is False.
        with patch("core.exporter.nvenc_available", return_value=True) as fake_probe:
            codec, is_gpu = Exporter._resolve_video_codec("libx264", prefer_gpu=False)
        self.assertEqual(codec, "libx264")
        self.assertFalse(is_gpu)
        fake_probe.assert_not_called()

    def test_codec_with_no_nvenc_equivalent_stays_on_cpu(self):
        # VP9 (.webm) has no entry in NVENC_CODEC_MAP.
        self.assertNotIn("libvpx-vp9", NVENC_CODEC_MAP)
        with patch("core.exporter.nvenc_available", return_value=True):
            codec, is_gpu = Exporter._resolve_video_codec("libvpx-vp9", prefer_gpu=True)
        self.assertEqual(codec, "libvpx-vp9")
        self.assertFalse(is_gpu)


class VideoQualityArgsTests(unittest.TestCase):
    """NVENC doesn't understand -crf; make sure the CRF preset value is
    routed through NVENC's own -cq/-rc/-preset conventions instead of being
    passed straight through to an encoder that would reject it.
    """

    def test_gpu_uses_cq_not_crf(self):
        args = Exporter._video_quality_args("h264_nvenc", is_gpu=True, crf="23")
        self.assertNotIn("-crf", args)
        self.assertIn("-cq:v", args)
        self.assertEqual(args[args.index("-cq:v") + 1], "23")
        self.assertIn("-rc:v", args)
        self.assertIn("vbr", args)
        self.assertIn("-preset", args)
        self.assertIn("-b:v", args)
        self.assertEqual(args[args.index("-b:v") + 1], "0")

    def test_cpu_libx264_uses_crf(self):
        args = Exporter._video_quality_args("libx264", is_gpu=False, crf="18")
        self.assertIn("-crf", args)
        self.assertEqual(args[args.index("-crf") + 1], "18")
        self.assertNotIn("-cq:v", args)

    def test_cpu_vp9_uses_crf(self):
        args = Exporter._video_quality_args("libvpx-vp9", is_gpu=False, crf="28")
        self.assertIn("-crf", args)
        self.assertNotIn("-cq:v", args)


def _build_simple_project():
    project = Project()
    project.add_media_asset(MediaAsset(path="/media/a.mp4", duration=5.0, has_video=True, has_audio=False))
    project.tracks[0].clips.append(
        Clip(media_type=MediaType.VIDEO, source_path="/media/a.mp4", source_in=0, source_out=5, timeline_start=0)
    )
    return project


class GpuExportIntegrationTests(unittest.TestCase):
    """End-to-end (mocked subprocess) coverage of the GPU export path
    through Exporter.export(), matching the mocked-Popen pattern the rest of
    this file already uses for two-pass/Shorts coverage.
    """

    def test_uses_nvenc_when_available_and_preferred(self):
        project = _build_simple_project()
        captured_args = []

        def fake_popen(args, **kwargs):
            captured_args.append(list(args))
            return _FakeProcess()

        with patch("core.exporter.find_binary", return_value="ffmpeg"), patch(
            "core.exporter.popen", fake_popen
        ), patch("core.exporter.nvenc_available", return_value=True):
            exporter = Exporter(project)
            settings = ExportSettings(output_path="/tmp/out.mp4", width=320, height=240, fps=25.0, use_gpu=True)
            exporter.export(settings)

        self.assertEqual(len(captured_args), 1)
        args = captured_args[0]
        self.assertIn("h264_nvenc", args)
        self.assertNotIn("libx264", args)
        self.assertIn("-cq:v", args)

    def test_uses_cpu_codec_when_nvenc_unavailable(self):
        project = _build_simple_project()
        captured_args = []

        def fake_popen(args, **kwargs):
            captured_args.append(list(args))
            return _FakeProcess()

        with patch("core.exporter.find_binary", return_value="ffmpeg"), patch(
            "core.exporter.popen", fake_popen
        ), patch("core.exporter.nvenc_available", return_value=False):
            exporter = Exporter(project)
            settings = ExportSettings(output_path="/tmp/out.mp4", width=320, height=240, fps=25.0, use_gpu=True)
            exporter.export(settings)

        self.assertEqual(len(captured_args), 1)
        args = captured_args[0]
        self.assertIn("libx264", args)
        self.assertNotIn("h264_nvenc", args)
        self.assertIn("-crf", args)

    def test_use_gpu_false_stays_on_cpu_even_if_nvenc_available(self):
        project = _build_simple_project()
        captured_args = []

        def fake_popen(args, **kwargs):
            captured_args.append(list(args))
            return _FakeProcess()

        with patch("core.exporter.find_binary", return_value="ffmpeg"), patch(
            "core.exporter.popen", fake_popen
        ), patch("core.exporter.nvenc_available", return_value=True):
            exporter = Exporter(project)
            settings = ExportSettings(output_path="/tmp/out.mp4", width=320, height=240, fps=25.0, use_gpu=False)
            exporter.export(settings)

        args = captured_args[0]
        self.assertIn("libx264", args)
        self.assertNotIn("h264_nvenc", args)

    def test_two_pass_ignores_gpu_preference(self):
        # NVENC's two-pass rate control doesn't use libx264's -pass 1/2
        # log-file scheme -- two-pass, target-file-size exports always use
        # the CPU codec, even with use_gpu=True and NVENC available.
        project = _build_simple_project()
        captured_args = []

        def fake_popen(args, **kwargs):
            captured_args.append(list(args))
            return _FakeProcess()

        with patch("core.exporter.find_binary", return_value="ffmpeg"), patch(
            "core.exporter.popen", fake_popen
        ), patch("core.exporter.nvenc_available", return_value=True):
            exporter = Exporter(project)
            settings = ExportSettings(
                output_path="/tmp/out.mp4", width=320, height=240, fps=25.0,
                two_pass=True, target_size_mb=1.0, use_gpu=True,
            )
            exporter.export(settings)

        self.assertEqual(len(captured_args), 2)  # pass 1, pass 2
        for pass_args in captured_args:
            self.assertIn("libx264", pass_args)
            self.assertNotIn("h264_nvenc", pass_args)

    def test_gpu_failure_falls_back_to_cpu_and_reports_it(self):
        # Simulate a real-world NVENC failure (driver issue, session limit,
        # etc.): the first (GPU) ffmpeg invocation exits non-zero, the retry
        # with the CPU codec succeeds. Exporter must not raise, must have
        # actually used the CPU codec on the second attempt, and must have
        # told the caller (via on_progress, the same status/log mechanism
        # the rest of the export flow already uses) that it fell back.
        project = _build_simple_project()
        captured_args = []
        progress_messages = []

        class _FailingThenSucceedingProcess:
            def __init__(self, args):
                self.stdout = _FakeStdout()
                self.returncode = 1 if "h264_nvenc" in args else 0

            def wait(self):
                return self.returncode

            def poll(self):
                return self.returncode

            def terminate(self):
                pass

        def fake_popen(args, **kwargs):
            captured_args.append(list(args))
            return _FailingThenSucceedingProcess(args)

        def on_progress(fraction, message):
            progress_messages.append(message)

        with patch("core.exporter.find_binary", return_value="ffmpeg"), patch(
            "core.exporter.popen", fake_popen
        ), patch("core.exporter.nvenc_available", return_value=True):
            exporter = Exporter(project)
            settings = ExportSettings(output_path="/tmp/out.mp4", width=320, height=240, fps=25.0, use_gpu=True)
            result = exporter.export(settings, on_progress=on_progress)

        self.assertEqual(result, "/tmp/out.mp4")
        self.assertEqual(len(captured_args), 2)
        self.assertIn("h264_nvenc", captured_args[0])
        self.assertIn("libx264", captured_args[1])
        self.assertNotIn("h264_nvenc", captured_args[1])
        self.assertTrue(any("fall" in m.lower() and "cpu" in m.lower() for m in progress_messages))

    def test_gpu_and_cpu_both_fail_raises(self):
        # If the CPU retry also fails, that's a real error (bad media,
        # invalid graph, ...) -- it must propagate, not be swallowed.
        from core.exporter import ExportError

        project = _build_simple_project()

        class _AlwaysFailingProcess:
            def __init__(self):
                self.stdout = _FakeStdout()
                self.returncode = 1

            def wait(self):
                return self.returncode

            def poll(self):
                return self.returncode

            def terminate(self):
                pass

        def fake_popen(args, **kwargs):
            return _AlwaysFailingProcess()

        with patch("core.exporter.find_binary", return_value="ffmpeg"), patch(
            "core.exporter.popen", fake_popen
        ), patch("core.exporter.nvenc_available", return_value=True):
            exporter = Exporter(project)
            settings = ExportSettings(output_path="/tmp/out.mp4", width=320, height=240, fps=25.0, use_gpu=True)
            with self.assertRaises(ExportError):
                exporter.export(settings)


if __name__ == "__main__":
    unittest.main()
