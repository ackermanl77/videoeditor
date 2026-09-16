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

from core.clip import Clip, MediaType
from core.exporter import ExportSettings, Exporter
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


if __name__ == "__main__":
    unittest.main()
