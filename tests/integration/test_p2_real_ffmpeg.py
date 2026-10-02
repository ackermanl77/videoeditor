"""Real-FFmpeg tests for the P2 batch (T2 combination matrix, frame render, codecs, HDR, proxies, self-test).

Fixtures are generated with lavfi at tiny sizes; skipped without FFmpeg
(repo ``bin/`` or ``PATH``), like tests/integration/test_real_ffmpeg.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType, ReframeMode, TextStyle, TransitionType
from core.effects import EffectInstance
from core.exporter import ExportSettings, Exporter
from core.filter_graph import build_filter_graph
from core.media_asset import MediaAsset
from core.project import Project, ProjectSettings
from core.track import TrackType

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _find(tool):
    bundled = os.path.join(REPO, "bin", tool + (".exe" if os.name == "nt" else ""))
    return bundled if os.path.isfile(bundled) else shutil.which(tool)


FFMPEG = _find("ffmpeg")
FFPROBE = _find("ffprobe")
FIX = {}
_TMP = None


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, check=True, **kw)


def setUpModule():
    global _TMP
    if not FFMPEG:
        return
    _TMP = tempfile.mkdtemp(prefix="ve_p2_integration_")
    for name, color, freq in (("red", "red", 300), ("blue", "blue", 500)):
        path = os.path.join(_TMP, f"{name}.mp4")
        _run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=160x120:r=30:d=2",
              "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:d=2",
              "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path])
        FIX[name] = path
    FIX["tone"] = os.path.join(_TMP, "tone.wav")
    _run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100:d=2", "-ac", "1", FIX["tone"]])
    FIX["image"] = os.path.join(_TMP, "still.png")
    _run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x120:d=1", "-frames:v", "1", FIX["image"]])
    FIX["hdr"] = os.path.join(_TMP, "hdr.mp4")
    _run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=30:d=1", "-c:v", "libx265",
          "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020", "-color_trc", "arib-std-b67",
          "-colorspace", "bt2020nc", "-x265-params", "log-level=error", FIX["hdr"]])
    FIX["lut"] = os.path.join(_TMP, "look's.cube")  # apostrophe: exercises filtergraph escaping
    with open(FIX["lut"], "w", encoding="ascii") as f:
        f.write("LUT_3D_SIZE 2\n" + "\n".join(f"{b} {g} {r}" for r in (0, 1) for g in (0, 1) for b in (0, 1)) + "\n")


def tearDownModule():
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)


def _asset(key, **kw):
    defaults = dict(duration=2.0, width=160, height=120, has_video=True, has_audio=True)
    if key == "tone":
        defaults = dict(duration=2.0, has_audio=True)
    if key == "image":
        defaults = dict(width=160, height=120)
    defaults.update(kw)
    return MediaAsset(path=FIX[key], **defaults)


def _project(*assets):
    project = Project()
    project.settings = ProjectSettings(width=64, height=36, fps=30.0)
    for asset in assets:
        project.add_media_asset(asset)
    return project


def _vclip(asset, start=0.0, length=2.0, **kw):
    kind = MediaType.IMAGE if asset.is_image else MediaType.VIDEO
    return Clip(media_type=kind, source_path=asset.path, source_out=length, timeline_start=start,
                source_duration=asset.duration or None, asset_id=asset.id, **kw)


def _aclip(asset, start=0.0, length=2.0, **kw):
    return Clip(media_type=MediaType.AUDIO, source_path=asset.path, source_out=length, timeline_start=start,
                source_duration=asset.duration, asset_id=asset.id, **kw)


def _matrix():
    """(name, project, extra graph kwargs) for every feature and several combinations."""
    cases = []

    def case(name, build, **graph_kw):
        cases.append((name, build, graph_kw))

    def plain():
        red = _asset("red")
        p = _project(red)
        p.tracks[0].clips.append(_vclip(red))
        return p

    def crossfade_ramp_keyframes():
        red, blue = _asset("red"), _asset("blue")
        p = _project(red, blue)
        p.tracks[0].clips += [
            _vclip(red, 0.0, transition_out=TransitionType.WIPE_LEFT, transition_duration=0.5,
                   opacity_keyframes=[(0.0, 0.2), (1.0, 1.0)]),
            _vclip(blue, 2.0, speed_keyframes=[(0.0, 1.0), (1.0, 2.0)],
                   position_keyframes=[(0.0, 0.3, 0.3), (1.0, 0.7, 0.7)]),
        ]
        return p

    def effects_everything():
        red = _asset("red")
        p = _project(red)
        p.tracks[0].clips.append(_vclip(red, effects=[EffectInstance.create(kind) for kind in
                                                     ("sharpen", "blur", "vignette", "video_denoise", "chromakey")]
                                       + [EffectInstance.create("lut3d", file=FIX["lut"])],
                                       chroma_key_enabled=True, brightness=0.1, transform_rotation=10.0,
                                       transform_scale=0.8, fade_in=0.2))
        return p

    def effects_on_ramp_fill():
        red = _asset("red")
        p = _project(red)
        p.tracks[0].clips.append(_vclip(red, reframe_mode=ReframeMode.FILL, speed_keyframes=[(0.0, 2.0), (1.0, 1.0)],
                                        effects=[EffectInstance.create("vignette"), EffectInstance.create("blur")]))
        return p

    def hdr_and_sd():
        hdr = _asset("hdr", duration=1.0, has_audio=False, color_transfer="arib-std-b67",
                     color_primaries="bt2020", color_space="bt2020nc", bit_depth=10)
        sd = _asset("red", color_space="smpte170m")
        p = _project(hdr, sd)
        p.tracks[0].clips += [_vclip(hdr, 0.0, 1.0), _vclip(sd, 1.0)]
        return p

    def audio_features():
        red, tone = _asset("red"), _asset("tone")
        p = _project(red, tone)
        p.tracks[0].clips.append(_vclip(red, volume_keyframes=[(0.0, 0.0), (1.0, 1.0)]))
        music = p.tracks[1]
        music.clips += [_aclip(tone, 0.0, transition_out=TransitionType.CROSSFADE, transition_duration=0.5),
                        _aclip(tone, 2.0, volume_keyframes=[(0.0, 1.0), (1.0, 0.2)], denoise_enabled=True)]
        music.duck_trigger_track_id = p.tracks[0].id
        return p

    def hidden_muted_text_image():
        red, image = _asset("red"), _asset("image")
        p = _project(red, image)
        p.tracks[0].clips.append(_vclip(red))
        p.tracks[0].hidden = True
        top = p.add_track(TrackType.VIDEO)
        top.clips += [_vclip(image, 0.0, 1.0), Clip(media_type=MediaType.TEXT, source_out=2.0,
                                                    text_style=TextStyle(content="Hi: 50%", box_enabled=True))]
        top.muted = True
        return p

    case("plain", plain)
    case("crossfade+ramp+keyframes", crossfade_ramp_keyframes)
    case("crossfade+ramp+keyframes trimmed", crossfade_ramp_keyframes, trim_range=(1.2, 3.0))
    case("all effects", effects_everything)
    case("effects on ramp+fill", effects_on_ramp_fill)
    case("hdr tonemap + bt601", hdr_and_sd)
    case("audio crossfade+automation+ducking", audio_features)
    case("audio crossfade trimmed", audio_features, trim_range=(1.0, 3.5))
    case("hidden/muted/text/image", hidden_muted_text_image)
    case("video only (frame render)", crossfade_ramp_keyframes, video_only=True)
    return cases


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/FFprobe not found")
class GraphMatrixTests(unittest.TestCase):
    """T2: every combination's graph actually runs (syntax, pad, format-negotiation errors)."""

    def test_every_combination_runs(self):
        for name, build, graph_kw in _matrix():
            with self.subTest(name):
                project = build()
                graph = build_filter_graph(project, project.settings, **graph_kw)
                try:
                    args = [FFMPEG, "-v", "error", "-nostdin"]
                    for spec in graph.input_specs:
                        args += spec.pre_args + ["-i", spec.path]
                    args += ["-filter_complex", graph.filter_complex, "-map", f"[{graph.video_out_label}]",
                             "-map", f"[{graph.audio_out_label}]", "-t", "0.2", "-f", "null", "-"]
                    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
                finally:
                    graph.cleanup()
                self.assertEqual(result.returncode, 0, f"{name}: {result.stderr[-1500:]}")


def _probe(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _png_pixel(png, x, y):
    raw = subprocess.run([FFMPEG, "-v", "error", "-f", "image2pipe", "-i", "-", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         input=png, capture_output=True, check=True).stdout
    width = int.from_bytes(png[16:20], "big")
    i = (y * width + x) * 3
    return tuple(raw[i:i + 3])


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/FFprobe not found")
class FeatureOutputTests(unittest.TestCase):
    def test_frame_render_shows_the_composite(self):  # F10b
        from core.frame_render import render_frame

        red, blue = _asset("red"), _asset("blue")
        p = _project(red, blue)
        p.settings = ProjectSettings(width=320, height=180, fps=30.0)
        p.tracks[0].clips.append(_vclip(red))
        pip = p.add_track(TrackType.VIDEO)
        pip.clips.append(_vclip(blue, transform_scale=0.4, transform_x=0.2, transform_y=0.25))
        with patch("core.ffmpeg_utils.find_binary", return_value=FFMPEG), patch("core.frame_render.find_binary", return_value=FFMPEG):
            png = render_frame(p, 1.0, size=(160, 90))
        self.assertTrue(png.startswith(b"\x89PNG"))
        r, g, b = _png_pixel(png, 32, 22)  # inside the picture-in-picture
        self.assertGreater(b, 150)
        self.assertLess(r, 80)
        r, g, b = _png_pixel(png, 110, 70)  # bottom layer (pillarboxed 120x90 inside 160x90)
        self.assertGreater(r, 150)
        self.assertLess(b, 80)

    def _export(self, project, name, **settings):
        out = os.path.join(_TMP, name)
        with patch("core.exporter.find_binary", return_value=FFMPEG):
            Exporter(project).export(ExportSettings(output_path=out, width=64, height=36, fps=30.0,
                                                    use_gpu=False, **settings))
        return _probe(out)

    def test_codecs_and_bt709_tags(self):  # PG1, B6
        red = _asset("red")
        for ext, codec, expected in ((".mkv", "hevc", "hevc"), (".mp4", "av1", "av1"),
                                     (".mov", "prores", "prores"), (".mov", "dnxhr", "dnxhd")):
            with self.subTest(codec):
                p = _project(red)
                p.tracks[0].clips.append(_vclip(red, length=0.5))
                width = 256 if codec == "dnxhr" else 64  # DNxHR needs at least 256x120
                out = os.path.join(_TMP, f"codec_{codec}{ext}")
                with patch("core.exporter.find_binary", return_value=FFMPEG):
                    Exporter(p).export(ExportSettings(output_path=out, width=width, height=144 if width == 256 else 36,
                                                      fps=30.0, use_gpu=False, video_codec=codec))
                video = [s for s in _probe(out)["streams"] if s["codec_type"] == "video"][0]
                self.assertEqual(video["codec_name"], expected)
                self.assertEqual(video.get("color_primaries"), "bt709")

    def test_hdr_source_exports_as_tagged_sdr(self):  # B6
        hdr = _asset("hdr", duration=1.0, has_audio=False, color_transfer="arib-std-b67",
                     color_primaries="bt2020", color_space="bt2020nc", bit_depth=10)
        p = _project(hdr)
        p.tracks[0].clips.append(_vclip(hdr, length=1.0))
        video = [s for s in self._export(p, "hdr_out.mp4")["streams"] if s["codec_type"] == "video"][0]
        self.assertEqual((video["pix_fmt"], video["color_transfer"], video["color_space"]), ("yuv420p", "bt709", "bt709"))

    def test_audio_track_crossfade_overlaps_the_clips(self):  # B5
        tone = _asset("tone")
        p = _project(tone)
        p.tracks[1].clips += [_aclip(tone, 0.0, transition_out=TransitionType.CROSSFADE, transition_duration=0.5),
                              _aclip(tone, 2.0)]
        data = self._export(p, "xfade_audio.mp4")
        audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
        self.assertEqual(len(audio), 1)
        self.assertAlmostEqual(float(data["format"]["duration"]), 4.0, delta=0.15)

    def test_cpu_proxy_is_540p_short_gop(self):  # B10
        from core.proxy import PROXY_HEIGHT, generate_proxy

        cache = os.path.join(_TMP, "proxies")
        with patch("core.ffmpeg_utils.find_binary", return_value=FFMPEG), patch("core.proxy.find_binary", return_value=FFMPEG):
            path = generate_proxy(_asset("red"), cache, use_gpu=False)
        self.assertIsNotNone(path)
        video = [s for s in _probe(path)["streams"] if s["codec_type"] == "video"][0]
        self.assertEqual(video["height"], PROXY_HEIGHT)
        self.assertFalse(any(name.endswith(".partial.mp4") for name in os.listdir(cache)))

    def test_self_test_passes(self):  # T4
        from core.selftest import run_self_test

        lines = []
        self.assertEqual(run_self_test(out=lines.append), 0, "\n".join(lines))
        self.assertIn("RESULT: PASS", lines)


if __name__ == "__main__":
    unittest.main()
