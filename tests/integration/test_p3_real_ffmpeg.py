"""Real-FFmpeg tests for the third round: 10-bit/HDR export, stabilization, animated effects.

Fixtures are generated with lavfi at tiny sizes; skipped without FFmpeg
(repo ``bin/`` or ``PATH``). The T2 graph matrix from test_p2_real_ffmpeg is
re-run in every colour mode.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType, TextStyle
from core.effects import EffectInstance
from core.exporter import ExportSettings, Exporter
from core.filter_graph import build_filter_graph
from core.media_asset import MediaAsset
from core.output_color import OutputColor
from core.project import Project, ProjectSettings
from core.track import TrackType
from tests.integration import test_p2_real_ffmpeg as p2

FFMPEG, FFPROBE = p2.FFMPEG, p2.FFPROBE
FIX = {}
_TMP = None
W, H = 128, 72


def _run(args):
    return subprocess.run(args, capture_output=True, check=True)


def setUpModule():
    global _TMP
    if not (FFMPEG and FFPROBE):
        return
    p2.setUpModule()
    _TMP = tempfile.mkdtemp(prefix="ve_p3_integration_")
    FIX["grad10"] = os.path.join(_TMP, "grad10.mkv")
    # A horizontal ramp of odd 10-bit levels (65, 71, ... 827): none survives a trip through
    # 8 bits, so any value that isn't a multiple of 4 after an export proves 10-bit precision.
    # (The lavfi "gradients" source used before is random by default and stepped in multiples
    # of 4, so the check only passed when a lossy encoder happened to add odd values.)
    _run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
          f"nullsrc=s={W}x{H}:d=1:r=30,format=yuv420p10le,geq=lum='65+X*6':cb=512:cr=512",
          "-c:v", "ffv1", FIX["grad10"]])
    for kind, trc in (("pq", "smpte2084"), ("hlg", "arib-std-b67")):
        FIX[kind] = os.path.join(_TMP, f"{kind}.mp4")
        params = "log-level=error:repeat-headers=1"
        if kind == "pq":
            params += (":hdr10=1:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)"
                       "L(10000000,1):max-cll=1000,400")
        _run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r=30:d=1",
              "-vf", f"setparams=color_primaries=bt2020:color_trc={trc}:colorspace=bt2020nc",
              "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", "-x265-params", params, FIX[kind]])
    FIX["white"] = os.path.join(_TMP, "white.mp4")
    _run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=white:s={W}x{H}:r=30:d=1",
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", FIX["white"]])
    FIX["gray"] = os.path.join(_TMP, "gray.mp4")
    _run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=gray:s={W}x{H}:r=30:d=1",
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", FIX["gray"]])
    FIX["shaky"] = os.path.join(_TMP, "shaky.mp4")
    _run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
          "testsrc2=s=320x240:r=30:d=2,crop=256:192:'32+24*sin(n*1.7)':'24+18*cos(n*1.3)'",
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", FIX["shaky"]])


def tearDownModule():
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)
        p2.tearDownModule()


def _probe_asset(key):
    with patch("core.media_probe.find_binary", return_value=FFPROBE):
        from core.media_probe import probe_file

        return probe_file(FIX[key])


def _project(*assets, size=(W, H)):
    project = Project()
    project.settings = ProjectSettings(width=size[0], height=size[1], fps=30.0)
    for asset in assets:
        project.add_media_asset(asset)
    return project


def _clip(asset, start=0.0, length=1.0, **kw):
    return Clip(media_type=MediaType.VIDEO, source_path=asset.path, source_out=length, timeline_start=start,
                source_duration=asset.duration or None, asset_id=asset.id, **kw)


def _export(project, name, size=(W, H), **settings):
    out = os.path.join(_TMP, name)
    with patch("core.exporter.find_binary", return_value=FFMPEG):
        Exporter(project).export(ExportSettings(output_path=out, width=size[0], height=size[1], fps=30.0, **settings))
    return out


def _stream(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v", "-print_format", "json", "-show_streams",
                          path], capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"][0]


def _first_frame_side_data(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v", "-read_intervals", "%+#1", "-show_frames",
                          "-show_entries", "frame=side_data_list", "-print_format", "json", path],
                         capture_output=True, text=True, check=True).stdout
    frames = json.loads(out).get("frames") or [{}]
    return [d.get("side_data_type") for d in frames[0].get("side_data_list") or []]


def _luma(path, frame=0, size=(W, H)):
    """10-bit luma samples of one frame."""
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-vf", f"select=eq(n\\,{frame})", "-frames:v", "1",
                          "-pix_fmt", "yuv420p10le", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    n = size[0] * size[1]
    return struct.unpack(f"<{n}H", raw[:2 * n])


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/FFprobe not found")
class ColorModeMatrixTests(unittest.TestCase):  # T2 for B6/PG1
    def test_every_combination_runs_in_every_colour_mode(self):
        for mode in ("sdr10", "pq"):  # HLG differs from PQ only in the transfer name
            for name, build, graph_kw in p2._matrix():
                with self.subTest(mode=mode, case=name):
                    project = build()
                    graph = build_filter_graph(project, project.settings, output_color=OutputColor.from_mode(mode),
                                               **graph_kw)
                    try:
                        args = [FFMPEG, "-v", "error", "-nostdin"]
                        for spec in graph.input_specs:
                            args += spec.pre_args + ["-i", spec.path]
                        args += ["-filter_complex", graph.filter_complex, "-map", f"[{graph.video_out_label}]",
                                 "-map", f"[{graph.audio_out_label}]", "-t", "0.2", "-f", "null", "-"]
                        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
                    finally:
                        graph.cleanup()
                    self.assertEqual(result.returncode, 0, f"{mode}/{name}: {result.stderr[-1500:]}")


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/FFprobe not found")
class TenBitAndHdrExportTests(unittest.TestCase):  # B6 / PG1
    def test_ten_bit_sdr_keeps_ten_bit_precision(self):
        asset = _probe_asset("grad10")
        for codec, ext in (("hevc", ".mkv"), ("av1", ".mkv"), ("prores", ".mov")):
            with self.subTest(codec):
                project = _project(asset)
                project.tracks[0].clips.append(_clip(asset))
                out = _export(project, f"ten_{codec}{ext}", use_gpu=False, video_codec=codec, color_mode="sdr10")
                stream = _stream(out)
                self.assertIn("10le", stream["pix_fmt"])
                self.assertEqual(stream.get("color_transfer"), "bt709")
                # Values between 8-bit steps exist only if nothing truncated to 8 bits.
                self.assertTrue(any(v % 4 for v in _luma(out)))
        project = _project(asset)
        project.tracks[0].clips.append(_clip(asset))
        eight = _export(project, "eight.mkv", use_gpu=False, video_codec="hevc")
        self.assertEqual(_stream(eight)["pix_fmt"], "yuv420p")
        self.assertFalse(any(v % 4 for v in _luma(eight)))

    def test_ten_bit_nvenc_when_available(self):
        from core.ffmpeg_utils import encoder_supports_10bit, nvenc_available

        with patch("core.ffmpeg_utils.find_binary", return_value=FFMPEG):
            if not (nvenc_available() and encoder_supports_10bit("hevc_nvenc")):
                self.skipTest("no 10-bit NVENC on this machine")
        asset = _probe_asset("grad10")
        project = _project(asset)
        project.tracks[0].clips.append(_clip(asset))
        with patch("core.exporter.nvenc_available", return_value=True):
            out = _export(project, "ten_nvenc.mkv", use_gpu=True, video_codec="hevc", color_mode="sdr10")
        stream = _stream(out)
        self.assertEqual((stream["pix_fmt"], stream.get("profile")), ("yuv420p10le", "Main 10"))

    def test_pq_passthrough_with_mastering_metadata_and_sdr_at_reference_white(self):
        pq, white = _probe_asset("pq"), _probe_asset("white")
        self.assertEqual(pq.color_transfer, "smpte2084")
        self.assertTrue(pq.mastering_display.startswith("G(13250,34500)"))
        self.assertEqual(pq.max_cll, "1000,400")
        project = _project(pq, white)
        project.tracks[0].clips += [_clip(pq), _clip(white, 1.0)]
        top = project.add_track(TrackType.VIDEO)
        top.clips.append(Clip(media_type=MediaType.TEXT, source_out=2.0, text_style=TextStyle(content="HDR")))
        out = _export(project, "pq.mkv", use_gpu=False, video_codec="hevc", color_mode="pq")
        stream = _stream(out)
        self.assertEqual((stream["pix_fmt"], stream.get("color_transfer"), stream.get("color_primaries"),
                          stream.get("color_space")), ("yuv420p10le", "smpte2084", "bt2020", "bt2020nc"))
        self.assertIn("Mastering display metadata", _first_frame_side_data(out))
        self.assertIn("Content light level metadata", _first_frame_side_data(out))
        # The SDR white clip (frame 45, past the text) sits at ~203 cd/m2: PQ code ~573 of 1023.
        corner = _luma(out, frame=45)[0]
        self.assertAlmostEqual(corner, 573, delta=12)

    def test_hlg_passthrough_tags(self):
        hlg, gray = _probe_asset("hlg"), _probe_asset("gray")
        project = _project(hlg, gray)
        project.tracks[0].clips += [_clip(hlg), _clip(gray, 1.0)]
        out = _export(project, "hlg.mov", use_gpu=False, video_codec="prores", color_mode="hlg")
        stream = _stream(out)
        self.assertEqual((stream["pix_fmt"], stream.get("color_transfer")), ("yuv422p10le", "arib-std-b67"))


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/FFprobe not found")
class StabilizeAndAnimationTests(unittest.TestCase):  # B7 vidstab, F12
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="stab_", dir=_TMP)
        patcher = patch.dict(os.environ, {"VIDEO_EDITOR_STABILIZE_DIR": self.cache})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _motion(path):
        """Mean absolute difference between consecutive frames (lower = steadier)."""
        result = subprocess.run(
            [FFMPEG, "-v", "error", "-i", path, "-vf",
             "tblend=all_mode=difference,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
             "-f", "null", "-"], capture_output=True, text=True, check=True).stdout
        values = [float(line.split("=")[1]) for line in result.splitlines() if "YAVG" in line]
        return sum(values) / len(values)

    def test_stabilization_reduces_motion_and_caches_its_analysis(self):
        asset = _probe_asset("shaky")
        size = (256, 192)

        def render(name, stabilize, **kw):
            project = _project(asset, size=size)
            effects = [EffectInstance.create("stabilize", smoothing=30)] if stabilize else []
            project.tracks[0].clips.append(_clip(asset, length=1.6, effects=effects))
            return _export(project, name, size=size, use_gpu=False, **kw)

        shaky = self._motion(render("shaky.mp4", False))
        steady = self._motion(render("steady.mp4", True))
        self.assertLess(steady, shaky * 0.8)
        analyses = [f for f in os.listdir(self.cache) if f.endswith(".trf")]
        self.assertEqual(len(analyses), 1)
        stamp = os.path.getmtime(os.path.join(self.cache, analyses[0]))
        render("steady_trimmed.mp4", True, trim_start=0.5, trim_end=1.2)  # seeks into the clip: still works
        self.assertEqual(os.path.getmtime(os.path.join(self.cache, analyses[0])), stamp)  # reused, not redone
        self.assertEqual(os.listdir(self.cache), analyses)

    def test_animated_brightness_changes_over_the_clip(self):
        gray = _probe_asset("gray")
        effect = EffectInstance.create("color_correction")
        effect.keyframes["brightness"] = [(0.0, -0.3), (1.0, 0.3)]
        project = _project(gray)
        project.tracks[0].clips.append(_clip(gray, effects=[effect]))
        out = _export(project, "animated.mp4", use_gpu=False)
        start, end = _luma(out, frame=0)[0], _luma(out, frame=29)[0]
        self.assertGreater(end - start, 300)  # -0.3 -> +0.3 brightness on a 10-bit scale


if __name__ == "__main__":
    unittest.main()
