"""Real-FFmpeg tests for the fourth round: reverse, freeze frame, subtitles, GPU-decode fallback.

Fixtures are lavfi-generated at tiny sizes (no media files in the repo);
skipped without FFmpeg (repo ``bin/`` or ``PATH``). The ramp fixture's
brightness rises with every frame and its sound is silent for the first
second and a tone for the second, so the direction of playback and where
the sound is can both be measured on the exported file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType
from core.exporter import ExportSettings, Exporter
from core.ffmpeg_utils import DECODE_HWACCEL_ENV, MAX_INLINE_FILTER_CHARS
from core.filter_graph import build_filter_graph
from core.frame_render import render_frame
from core.media_asset import MediaAsset
from core.project import Project, ProjectSettings
from core.subtitles import cues_to_text_clips, parse_subtitle_text
from core.track import Track, TrackType
from tests.integration import test_p2_real_ffmpeg as p2

FFMPEG, FFPROBE = p2.FFMPEG, p2.FFPROBE
FIX = {}
_TMP = None
W, H = 64, 36
SW, SH = 320, 180  # subtitle tests: big enough for legible caption text


def _run(args):
    return subprocess.run(args, capture_output=True, check=True)


def setUpModule():
    global _TMP
    if not (FFMPEG and FFPROBE):
        return
    _TMP = tempfile.mkdtemp(prefix="ve_p4_integration_")
    FIX["ramp"] = os.path.join(_TMP, "ramp.mp4")
    _run([FFMPEG, "-y", "-v", "error",
          "-f", "lavfi", "-i", f"nullsrc=s={W}x{H}:r=30:d=2,geq=lum='16+N*3.5':cb=128:cr=128,format=yuv420p",
          "-f", "lavfi", "-i", "aevalsrc='if(gte(t,1),0.5*sin(2*PI*440*t),0)':s=48000:d=2",
          "-c:v", "libx264", "-preset", "ultrafast", "-g", "1", "-c:a", "aac", "-shortest", FIX["ramp"]])
    FIX["black"] = os.path.join(_TMP, "black.mp4")
    _run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r=30:d=20",
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", FIX["black"]])


def tearDownModule():
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)


def _asset(key, duration):
    return MediaAsset(path=FIX[key], duration=duration, width=W, height=H, has_video=True,
                      has_audio=key == "ramp")


def _project(asset, *clips):
    project = Project()
    project.settings = ProjectSettings(width=W, height=H, fps=30.0)
    project.add_media_asset(asset)
    project.tracks[0].clips.extend(clips)
    return project


def _clip(asset, src_in=0.0, src_out=2.0, start=0.0, **kw):
    return Clip(media_type=MediaType.VIDEO, source_path=asset.path, source_in=src_in, source_out=src_out,
                timeline_start=start, source_duration=asset.duration, asset_id=asset.id, **kw)


def _export(project, name, size=(W, H), **settings):
    out = os.path.join(_TMP, name)
    with patch("core.exporter.find_binary", return_value=FFMPEG):
        Exporter(project).export(ExportSettings(output_path=out, width=size[0], height=size[1], fps=30.0,
                                                use_gpu=False, **settings))
    return out


def _duration(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


def _mean_luma(path, t, crop=None):
    """Average 8-bit luma of the frame at ``t`` seconds (optionally of a ``w:h:x:y`` crop)."""
    vf = (f"crop={crop}," if crop else "") + "format=gray"
    raw = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1", "-vf", vf,
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return sum(raw) / max(1, len(raw))


def _max_luma(path, t, crop):
    raw = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                          "-vf", f"crop={crop},format=gray", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return max(raw) if raw else 0


def _png_luma(data):
    raw = subprocess.run([FFMPEG, "-v", "error", "-f", "png_pipe", "-i", "-", "-vf", "format=gray",
                          "-f", "rawvideo", "-"], input=data, capture_output=True, check=True).stdout
    return sum(raw) / max(1, len(raw))


def _max_volume_db(path, start, length):
    """Loudest sample (dB) in ``[start, start + length)`` of ``path``'s audio; -inf for digital silence."""
    log = subprocess.run([FFMPEG, "-v", "info", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", path,
                          "-vn", "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    match = re.search(r"max_volume: (-?[\d.]+|-inf) dB", log)
    return float("-inf") if match is None or match.group(1) == "-inf" else float(match.group(1))


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/FFprobe not found")
class ReverseExportTests(unittest.TestCase):
    def test_reversed_clip_plays_picture_and_sound_backwards(self):
        asset = _asset("ramp", 2.0)
        out = _export(_project(asset, _clip(asset, reverse=True)), "reverse.mp4")
        self.assertAlmostEqual(_duration(out), 2.0, delta=0.1)
        self.assertGreater(_mean_luma(out, 0.1), _mean_luma(out, 1.8) + 100)  # bright -> dark
        self.assertGreater(_max_volume_db(out, 0.1, 0.7), -20)  # the tone (source 1..2) now comes first
        self.assertLess(_max_volume_db(out, 1.2, 0.7), -50)

    def test_reversed_speed_ramp_renders_with_its_duration(self):
        asset = _asset("ramp", 2.0)
        clip = _clip(asset, reverse=True, speed_keyframes=[(0.0, 2.0), (1.0, 1.0)])  # 0.5 s + 1 s
        out = _export(_project(asset, clip), "reverse_ramp.mp4")
        self.assertAlmostEqual(_duration(out), 1.5, delta=0.1)
        self.assertGreater(_mean_luma(out, 0.05), _mean_luma(out, 1.4) + 100)

    def test_trimmed_export_and_preview_frames_of_a_reversed_clip_match_the_full_export(self):
        asset = _asset("ramp", 2.0)
        project = _project(asset, _clip(asset, src_in=0.5, src_out=2.0, reverse=True))
        full = _export(project, "reverse_full.mp4")
        window = _export(project, "reverse_window.mp4", trim_start=0.5, trim_end=1.0)
        self.assertAlmostEqual(_mean_luma(window, 0.0), _mean_luma(full, 0.5), delta=4)
        with patch("core.frame_render.find_binary", return_value=FFMPEG):
            frame = render_frame(project, 1.0, (W, H))
        self.assertAlmostEqual(_png_luma(frame), _mean_luma(full, 1.0), delta=4)


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/FFprobe not found")
class FreezeFrameExportTests(unittest.TestCase):
    def test_freeze_holds_the_last_frame_in_silence(self):
        asset = _asset("ramp", 2.0)
        out = _export(_project(asset, _clip(asset, src_in=1.0, src_out=2.0, freeze_frame_duration=1.0)),
                      "freeze.mp4")
        self.assertAlmostEqual(_duration(out), 2.0, delta=0.1)
        last = _mean_luma(out, 0.95)
        self.assertAlmostEqual(_mean_luma(out, 1.5), last, delta=3)
        self.assertAlmostEqual(_mean_luma(out, 1.9), last, delta=3)
        self.assertGreater(_max_volume_db(out, 0.1, 0.7), -20)
        self.assertLess(_max_volume_db(out, 1.2, 0.7), -50)

    def test_fade_out_reaches_into_the_freeze_and_reversed_freeze_holds_the_first_frame(self):
        asset = _asset("ramp", 2.0)
        faded = _export(_project(asset, _clip(asset, src_out=1.0, freeze_frame_duration=1.0, fade_out=0.5)),
                        "freeze_fade.mp4")
        self.assertLess(_mean_luma(faded, 1.95), 25)  # faded to the black canvas
        self.assertGreater(_mean_luma(faded, 1.2), 80)
        rev = _export(_project(asset, _clip(asset, reverse=True, freeze_frame_duration=0.5)), "freeze_rev.mp4")
        self.assertLess(_mean_luma(rev, 2.3), 30)  # holds source_in's (dark) frame

    def test_preview_frame_inside_the_freeze_matches_the_export(self):
        asset = _asset("ramp", 2.0)
        project = _project(asset, _clip(asset, src_out=1.5, freeze_frame_duration=2.0))
        out = _export(project, "freeze_preview.mp4")
        with patch("core.frame_render.find_binary", return_value=FFMPEG):
            frame = render_frame(project, 2.5, (W, H))
        self.assertAlmostEqual(_png_luma(frame), _mean_luma(out, 2.5), delta=4)
        window = _export(project, "freeze_window.mp4", trim_start=2.5, trim_end=3.0)
        self.assertAlmostEqual(_mean_luma(window, 0.1), _mean_luma(out, 2.5), delta=4)


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/FFprobe not found")
class SubtitleExportTests(unittest.TestCase):
    def _project_with_subtitles(self, srt, duration):
        asset = _asset("black", 20.0)
        project = _project(asset, _clip(asset, src_out=duration))
        project.settings = ProjectSettings(width=SW, height=SH, fps=30.0)  # captions need real pixels
        track = Track(track_type=TrackType.VIDEO, name="Subtitles")
        track.clips.extend(cues_to_text_clips(parse_subtitle_text(srt)))
        project.tracks.append(track)
        return project

    def test_imported_cues_are_burned_in_only_while_shown(self):
        srt = "1\n00:00:00,500 --> 00:00:01,500\nHELLO\n\n2\n00:00:02,000 --> 00:00:02,500\nWORLD\n"
        out = _export(self._project_with_subtitles(srt, 3.0), "subs.mp4", size=(SW, SH))
        band = f"{SW}:{SH // 4}:0:{SH - SH // 4}"  # the bottom quarter, where captions sit
        for shown, hidden in ((1.0, 1.75), (2.25, 2.75)):
            self.assertGreater(_max_luma(out, shown, band), 150)  # white caption text
            self.assertLess(_max_luma(out, hidden, band), 40)  # plain black

    def test_hundreds_of_cues_export_through_a_filter_graph_file(self):
        cues = "\n".join(
            f"{i + 1}\n00:00:{i // 10:02d},{(i % 10) * 100:03d} --> 00:00:{i // 10:02d},{(i % 10) * 100 + 90:03d}\n"
            f"Cue {i}\n" for i in range(150)
        )
        project = self._project_with_subtitles(cues, 15.0)
        graph = build_filter_graph(project, project.settings)
        graph.cleanup()
        self.assertGreater(len(graph.filter_complex), MAX_INLINE_FILTER_CHARS)
        out = _export(project, "many_subs.mp4", size=(SW, SH))
        self.assertAlmostEqual(_duration(out), 15.0, delta=0.2)
        leftovers = [n for n in os.listdir(tempfile.gettempdir()) if n.startswith("ve_graph_")]
        self.assertEqual(leftovers, [])

    SOFT_SRT = "1\n00:00:00,500 --> 00:00:01,500\nHELLO\n\n2\n00:00:02,000 --> 00:00:02,500\nWORLD\n"

    def _subtitle_streams(self, path):
        out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "s", "-show_entries",
                              "stream=codec_name", "-of", "json", path], capture_output=True, text=True, check=True).stdout
        return [s["codec_name"] for s in json.loads(out).get("streams", [])]

    def _extracted_srt(self, path):
        return subprocess.run([FFMPEG, "-v", "error", "-i", path, "-map", "0:s:0", "-f", "srt", "-"],
                              capture_output=True, text=True, check=True).stdout

    def test_embedded_subtitles_are_a_stream_not_pixels(self):
        band = f"{SW}:{SH // 4}:0:{SH - SH // 4}"
        for name, codec in (("soft.mp4", "mov_text"), ("soft.mkv", "subrip"), ("soft.webm", "webvtt")):
            with self.subTest(container=name):
                out = _export(self._project_with_subtitles(self.SOFT_SRT, 3.0), name, size=(SW, SH),
                              subtitle_mode="embed")
                self.assertEqual(self._subtitle_streams(out), [codec])
                self.assertLess(_max_luma(out, 1.0, band), 40)  # not burned in
                text = self._extracted_srt(out)
                self.assertIn("00:00:00,500 --> 00:00:01,500", text)
                self.assertIn("WORLD", text)
        leftovers = [n for n in os.listdir(tempfile.gettempdir()) if n.startswith("ve_subtitles_")]
        self.assertEqual(leftovers, [])

    def test_a_trimmed_embed_re_times_the_cues(self):
        out = _export(self._project_with_subtitles(self.SOFT_SRT, 3.0), "soft_trim.mkv", size=(SW, SH),
                      subtitle_mode="embed", trim_start=1.0, trim_end=3.0)
        text = self._extracted_srt(out)
        self.assertIn("00:00:00,000 --> 00:00:00,500\nHELLO", text)
        self.assertIn("00:00:01,000 --> 00:00:01,500\nWORLD", text)

    def test_sidecar_mode_writes_an_srt_next_to_the_video(self):
        out = _export(self._project_with_subtitles(self.SOFT_SRT, 3.0), "sidecar.mp4", size=(SW, SH),
                      subtitle_mode="sidecar")
        band = f"{SW}:{SH // 4}:0:{SH - SH // 4}"
        self.assertEqual(self._subtitle_streams(out), [])
        self.assertLess(_max_luma(out, 1.0, band), 40)
        with open(os.path.splitext(out)[0] + ".srt", encoding="utf-8") as f:
            self.assertEqual(f.read(), "1\n00:00:00,500 --> 00:00:01,500\nHELLO\n\n2\n00:00:02,000 --> 00:00:02,500\nWORLD\n")

    def test_embedding_into_a_container_without_subtitle_support_fails_clearly(self):
        from core.exporter import ExportError

        with self.assertRaisesRegex(ExportError, "carry a subtitle track"):
            _export(self._project_with_subtitles(self.SOFT_SRT, 3.0), "soft.avi", size=(SW, SH),
                    subtitle_mode="embed")


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/FFprobe not found")
class HardwareDecodeFallbackTests(unittest.TestCase):
    """A decode mode FFmpeg can't use must end in a normal software-decoded result."""

    BOGUS = ["not_a_real_hwaccel", None]

    def test_export_falls_back_to_software_and_leaves_no_partial_file(self):
        asset = _asset("ramp", 2.0)
        project = _project(asset, _clip(asset, reverse=True))
        messages = []
        out = os.path.join(_TMP, "hw_fallback.mp4")
        with patch("core.exporter.find_binary", return_value=FFMPEG), \
                patch("core.ffmpeg_utils.decode_hwaccel_modes", return_value=self.BOGUS):
            Exporter(project).export(ExportSettings(output_path=out, width=W, height=H, fps=30.0, use_gpu=False),
                                     on_progress=lambda f, m: messages.append(m))
        self.assertAlmostEqual(_duration(out), 2.0, delta=0.1)
        self.assertTrue(any("retrying with software decoding" in m for m in messages))
        self.assertFalse([n for n in os.listdir(_TMP) if ".partial" in n])

    def test_frame_thumbnail_and_proxy_fall_back_too(self):
        from core.media_probe import generate_thumbnail
        from core.proxy import generate_proxy

        asset = _asset("ramp", 2.0)
        project = _project(asset, _clip(asset))
        cache = tempfile.mkdtemp(prefix="ve_p4_cache_", dir=_TMP)
        with patch("core.ffmpeg_utils.decode_hwaccel_modes", return_value=self.BOGUS), \
                patch("core.frame_render.find_binary", return_value=FFMPEG):
            self.assertTrue(render_frame(project, 0.5, (W, H)).startswith(b"\x89PNG"))
        with patch("core.ffmpeg_utils.decode_hwaccel_modes", return_value=self.BOGUS), \
                patch("core.media_probe.find_binary", return_value=FFMPEG):
            self.assertTrue(os.path.isfile(generate_thumbnail(asset.path, cache, 0.5)))
        with patch("core.ffmpeg_utils.decode_hwaccel_modes", return_value=self.BOGUS), \
                patch("core.proxy.find_binary", return_value=FFMPEG):
            proxy = generate_proxy(asset, cache, use_gpu=False)
        self.assertTrue(proxy and os.path.isfile(proxy))
        self.assertFalse([n for n in os.listdir(cache) if ".partial" in n])

    def test_gpu_decoding_on_this_machine_matches_software(self):
        """With VIDEO_EDITOR_HWACCEL=auto the result equals software decoding (GPU or not)."""
        asset = _asset("ramp", 2.0)
        project = _project(asset, _clip(asset, src_in=0.5, src_out=1.5))
        soft = _export(project, "decode_sw.mp4")
        with patch.dict(os.environ, {DECODE_HWACCEL_ENV: "auto"}):
            hard = _export(project, "decode_auto.mp4")
        for t in (0.1, 0.5, 0.9):
            self.assertAlmostEqual(_mean_luma(hard, t), _mean_luma(soft, t), delta=3)


if __name__ == "__main__":
    unittest.main()
