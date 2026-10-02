"""Real-FFmpeg integration tests (T1): the export graph actually run, not just its text.

Fixtures are generated at test time with lavfi (no media in the repo), at
small sizes so the whole module takes a few seconds. Skipped when no
FFmpeg is found (repo ``bin/`` or ``PATH``); CI's ``integration`` job
installs the pinned build from ``ffmpeg.lock`` (see scripts/fetch_ffmpeg.py).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType, TextStyle, TransitionType
from core.exporter import ExportSettings, Exporter
from core.filter_graph import build_filter_graph
from core.media_asset import MediaAsset
from core.project import Project, ProjectSettings
from core.track import Track, TrackType

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _find(tool):
    bundled = os.path.join(REPO, "bin", tool + (".exe" if os.name == "nt" else ""))
    return bundled if os.path.isfile(bundled) else shutil.which(tool)


FFMPEG = _find("ffmpeg")
FFPROBE = _find("ffprobe")
_TMP = None
FIX = {}


def setUpModule():
    global _TMP
    if not FFMPEG:
        return
    _TMP = tempfile.mkdtemp(prefix="ve_integration_")
    for i in range(4):
        path = os.path.join(_TMP, f"clip{i}.mp4")
        _run([FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=4", "-f", "lavfi",
              "-i", f"sine=frequency={300 + 100 * i}:sample_rate=48000:d=4",
              "-vf", f"hue=h={i * 60}", "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path])
        FIX[f"clip{i}"] = path
    for name, freq, rate in (("tone_a", 220, 48000), ("tone_b", 330, 44100)):
        path = os.path.join(_TMP, f"{name}.wav")
        # 0.8 amplitude: two of these summed peak at about +3.7 dBFS without the limiter.
        _run([FFMPEG, "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate={rate}:d=4",
              "-af", "volume=0.8*2", "-ac", "1", path])
        FIX[name] = path


def tearDownModule():
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, **kw)


def _args(graph, tail):
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    for spec in graph.input_specs:
        args += list(spec.pre_args) + ["-i", spec.path]
    return args + ["-filter_complex", graph.filter_complex] + tail


def _video_hashes(graph, filter_complex=None):
    fc = (filter_complex or graph.filter_complex) + f";[{graph.audio_out_label}]anullsink"
    args = _args(graph, ["-map", f"[{graph.video_out_label}]", "-f", "framemd5", "-"])
    args[args.index("-filter_complex") + 1] = fc
    out = _run(args).stdout
    return [line.split(",")[-1].strip() for line in out.splitlines() if line and not line.startswith("#")]


def _asset(project, path, duration=4.0, video=True, audio=True):
    project.add_media_asset(MediaAsset(path=path, duration=duration, has_video=video, has_audio=audio))


def _multi_clip_project():
    """Sequential clips with a gap, a crossfade, a speed change and a PiP layer on V2."""
    p = Project()
    for i in range(4):
        _asset(p, FIX[f"clip{i}"])
    v1 = p.tracks[0]
    v1.clips += [
        Clip(media_type=MediaType.VIDEO, source_path=FIX["clip0"], source_in=0.5, source_out=3.0, timeline_start=0.0,
             fade_in=0.4, transition_out=TransitionType.CROSSFADE, transition_duration=0.6),
        Clip(media_type=MediaType.VIDEO, source_path=FIX["clip1"], source_in=0.0, source_out=2.5, timeline_start=2.5),
        Clip(media_type=MediaType.VIDEO, source_path=FIX["clip2"], source_in=0.0, source_out=3.0, timeline_start=5.5,
             speed=1.5, fade_out=0.5, opacity_keyframes=[(0.0, 0.3), (1.0, 1.0)]),
    ]
    v2 = Track(track_type=TrackType.VIDEO, name="V2")
    v2.clips.append(Clip(media_type=MediaType.VIDEO, source_path=FIX["clip3"], source_in=1.0, source_out=3.0,
                         timeline_start=1.3, transform_scale=0.4, transform_x=0.8, transform_y=0.25, muted=True))
    p.tracks.insert(1, v2)
    return p


@unittest.skipUnless(FFMPEG, "real ffmpeg not available")
class GraphEquivalenceTests(unittest.TestCase):
    settings = ProjectSettings(320, 180, 30.0)

    def test_setpts_shift_is_frame_identical_to_the_old_tpad_shift(self):
        # B1 regression guard: the same graph with every setpts timeline
        # shift swapped back to the old tpad form must hash identically.
        graph = build_filter_graph(_multi_clip_project(), self.settings)
        try:
            self.assertIn("setpts=PTS+", graph.filter_complex)
            legacy = re.sub(r"setpts=PTS\+([\d.]+)/TB", r"tpad=start_duration=\1:color=black@0.0", graph.filter_complex)
            new_hashes, old_hashes = _video_hashes(graph), _video_hashes(graph, legacy)
        finally:
            graph.cleanup()
        self.assertEqual(len(new_hashes), round(7.5 * 30))  # timeline ends at 7.5 s
        self.assertEqual(new_hashes, old_hashes)

    def test_trimmed_export_matches_the_same_frames_of_the_full_export(self):
        # B2 regression guard: building the graph for a window must render
        # exactly the frames the full timeline shows there.
        project = _multi_clip_project()
        full = build_filter_graph(project, self.settings)
        window = build_filter_graph(project, self.settings, trim_range=(1.9, 6.2))
        try:
            full_hashes, window_hashes = _video_hashes(full), _video_hashes(window)
        finally:
            full.cleanup()
            window.cleanup()
        first = round(1.9 * 30)
        self.assertEqual(len(window_hashes), round((6.2 - 1.9) * 30))
        self.assertEqual(window_hashes, full_hashes[first:first + len(window_hashes)])

    def test_export_cost_grows_linearly_with_clip_count(self):
        # tpad made cost quadratic (sum of start times); measured t16/t4 was 5-9x.
        def cost(n):
            p = Project()
            _asset(p, FIX["clip0"])
            for i in range(n):
                p.tracks[0].clips.append(Clip(media_type=MediaType.VIDEO, source_path=FIX["clip0"],
                                              source_in=0.0, source_out=2.0, timeline_start=2.0 * i, muted=True))
            graph = build_filter_graph(p, ProjectSettings(640, 360, 30.0))
            args = _args(graph, ["-map", f"[{graph.video_out_label}]", "-map", f"[{graph.audio_out_label}]",
                                 "-f", "null", "-"])
            start = time.perf_counter()
            _run(args)
            return time.perf_counter() - start

        t4 = min(cost(4) for _ in range(2))
        t16 = min(cost(16) for _ in range(2))
        self.assertLess(t16 / t4, 6.0, f"t4={t4:.2f}s t16={t16:.2f}s")


@unittest.skipUnless(FFMPEG and FFPROBE, "real ffmpeg/ffprobe not available")
class ExportOutputTests(unittest.TestCase):
    def setUp(self):
        self.out_dir = tempfile.mkdtemp(prefix="ve_integration_out_")
        self.addCleanup(shutil.rmtree, self.out_dir, True)

    def _export(self, project, name, **kw):
        out = os.path.join(self.out_dir, name)
        with patch("core.exporter.find_binary", return_value=FFMPEG):
            Exporter(project).export(ExportSettings(output_path=out, width=320, height=180, fps=30.0,
                                                    use_gpu=False, **kw))
        return out

    def _probe(self, path, entries):
        return _run([FFPROBE, "-v", "error", "-show_entries", entries, "-of", "default=nw=1", path]).stdout

    def _audio_stats(self, path):
        err = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", path, "-af", "astats=metadata=0,ebur128",
                              "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace").stderr
        peak = max(float(v) for v in re.findall(r"Peak level dB: (-?[\d.]+|-inf)", err) if v != "-inf")
        loudness = float(re.findall(r"^\s*I:\s+(-?[\d.]+) LUFS", err, re.MULTILINE)[-1])
        return peak, loudness

    def _two_tones(self):
        p = Project()
        a2 = Track(track_type=TrackType.AUDIO, name="A2")
        p.tracks.append(a2)
        for track, name in ((p.tracks[1], "tone_a"), (a2, "tone_b")):
            _asset(p, FIX[name], video=False)
            track.clips.append(Clip(media_type=MediaType.AUDIO, source_path=FIX[name], source_in=0, source_out=4))
        return p

    def test_master_limiter_keeps_the_mix_below_minus_1_dbfs(self):
        out = self._export(self._two_tones(), "mix.mp4")
        peak, _ = self._audio_stats(out)
        self.assertLessEqual(peak, -0.9)
        self.assertIn("sample_rate=48000", self._probe(out, "stream=sample_rate"))

    def test_loudness_normalization_hits_the_target(self):
        out = self._export(self._two_tones(), "loud.mp4", loudness_target=-23.0, audio_bitrate_kbps=128)
        _, loudness = self._audio_stats(out)
        self.assertAlmostEqual(loudness, -23.0, delta=1.0)

    def test_trimmed_export_has_the_window_duration(self):
        out = self._export(_multi_clip_project(), "trim.mp4", trim_start=2.0, trim_end=5.0)
        duration = float(self._probe(out, "format=duration").split("=")[1])
        self.assertAlmostEqual(duration, 3.0, delta=0.1)

    def test_styled_text_uses_a_real_font_file(self):
        from core.fonts import resolve_font_file

        if resolve_font_file("Arial") is None:
            self.skipTest("no system fonts found")
        p = Project()
        p.tracks[0].clips.append(Clip(media_type=MediaType.TEXT, source_in=0, source_out=1, text_style=TextStyle(
            content="Árbol\nsecond line", bold=True, italic=True, alignment="right",
            shadow_enabled=True, box_enabled=True)))
        graph = build_filter_graph(p, ProjectSettings(320, 180, 30.0))
        try:
            self.assertIn("fontfile=", graph.filter_complex)
            args = _args(graph, ["-map", f"[{graph.video_out_label}]", "-map", f"[{graph.audio_out_label}]",
                                 "-f", "null", "-"])
            args[args.index("-loglevel") + 1] = "warning"
            result = _run(args)
        finally:
            graph.cleanup()
        self.assertNotIn("Fontconfig", result.stderr)


if __name__ == "__main__":
    unittest.main()
