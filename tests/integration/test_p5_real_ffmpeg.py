"""Real-FFmpeg tests for styled ASS/SSA subtitles rendered with libass.

A styled script puts a yellow line at the bottom and a magenta, \\pos-placed
line at the top; the exports are checked for those colours in those places at
the right times. Fixtures are lavfi-generated; skipped without FFmpeg.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType
from core.exporter import ExportSettings, Exporter
from core.frame_render import render_frame
from core.media_asset import MediaAsset
from core.project import Project, ProjectSettings
from core.subtitles import split_ass_script, styled_subtitle_tracks
from core.track import Track, TrackType
from tests.integration import test_p2_real_ffmpeg as p2

FFMPEG, FFPROBE = p2.FFMPEG, p2.FFPROBE
W, H = 320, 180
_TMP = None
BLACK = None

SCRIPT = """[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 180

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,28,&H0000FFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1
Style: Sign,Arial,24,&H00FF00FF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,8,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.50,Default,,0,0,0,,BOTTOM LINE
Dialogue: 0,0:00:00.50,0:00:01.50,Sign,,0,0,0,,{\\pos(160,30)}TOP SIGN
"""

BOTTOM = f"{W}:{H // 4}:0:{H - H // 4}"
TOP = f"{W}:{H // 3}:0:0"


def setUpModule():
    global _TMP, BLACK
    if not (FFMPEG and FFPROBE):
        return
    _TMP = tempfile.mkdtemp(prefix="ve_p5_integration_")
    BLACK = os.path.join(_TMP, "black.mp4")
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r=30:d=5",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", BLACK], check=True)


def tearDownModule():
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)


def _project(duration=2.0):
    asset = MediaAsset(path=BLACK, duration=5.0, width=W, height=H, has_video=True, has_audio=False)
    project = Project()
    project.settings = ProjectSettings(width=W, height=H, fps=30.0)
    project.add_media_asset(asset)
    project.tracks[0].clips.append(Clip(
        media_type=MediaType.VIDEO, source_path=BLACK, source_in=0.0, source_out=duration, timeline_start=0.0,
        source_duration=5.0, asset_id=asset.id,
    ))
    script = split_ass_script(SCRIPT)
    project.subtitle_scripts["s1"] = script.header
    for n, clips in enumerate(styled_subtitle_tracks(script, "s1"), start=1):
        track = Track(track_type=TrackType.VIDEO, name="Subtitles" if n == 1 else f"Subtitles {n}")
        track.clips.extend(clips)
        project.tracks.append(track)
    return project


def _export(project, name, **settings):
    out = os.path.join(_TMP, name)
    with patch("core.exporter.find_binary", return_value=FFMPEG):
        Exporter(project).export(ExportSettings(output_path=out, width=W, height=H, fps=30.0, use_gpu=False, **settings))
    return out


def _rgb(raw):
    return [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw) - 2, 3)]


def _pixels(path, t, crop):
    raw = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                          "-vf", f"crop={crop},format=rgb24", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return _rgb(raw)


def _png_pixels(data, crop):
    raw = subprocess.run([FFMPEG, "-v", "error", "-f", "png_pipe", "-i", "-", "-vf", f"crop={crop},format=rgb24",
                          "-f", "rawvideo", "-"], input=data, capture_output=True, check=True).stdout
    return _rgb(raw)


def _yellow(pixels):
    return sum(1 for r, g, b in pixels if r > 150 and g > 150 and b < 100)


def _magenta(pixels):
    return sum(1 for r, g, b in pixels if r > 150 and b > 150 and g < 100)


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/FFprobe not found")
class StyledSubtitleExportTests(unittest.TestCase):
    def test_burned_in_with_their_own_colours_positions_and_times(self):
        out = _export(_project(), "styled.mp4")
        self.assertGreater(_yellow(_pixels(out, 1.0, BOTTOM)), 50)
        self.assertGreater(_magenta(_pixels(out, 1.0, TOP)), 30)
        self.assertEqual(_magenta(_pixels(out, 0.25, TOP)), 0)  # the sign starts at 0.5 s
        self.assertEqual(_yellow(_pixels(out, 1.75, BOTTOM)), 0)  # both end at 1.5 s
        self.assertEqual(_yellow(_pixels(out, 1.0, TOP)), 0)  # each line in its own place

    def test_moving_a_line_moves_it_and_an_edited_line_keeps_its_style(self):
        project = _project(3.0)
        bottom = next(t for t in project.tracks if t.name == "Subtitles").clips[0]
        bottom.timeline_start = 1.0
        bottom.text_style.content = "EDITED"
        out = _export(project, "styled_moved.mp4")
        self.assertEqual(_yellow(_pixels(out, 0.5, BOTTOM)), 0)
        self.assertGreater(_yellow(_pixels(out, 2.0, BOTTOM)), 30)  # still the yellow Default style

    def test_a_trimmed_export_re_times_the_lines(self):
        out = _export(_project(), "styled_trim.mp4", trim_start=1.0, trim_end=2.0)
        self.assertGreater(_yellow(_pixels(out, 0.2, BOTTOM)), 50)
        self.assertEqual(_yellow(_pixels(out, 0.75, BOTTOM)), 0)

    def test_mkv_embeds_an_ass_stream_and_a_sidecar_is_an_ass_file(self):
        out = _export(_project(), "styled.mkv", subtitle_mode="embed")
        streams = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "s", "-show_entries", "stream=codec_name",
                                  "-of", "json", out], capture_output=True, text=True, check=True).stdout
        self.assertEqual([s["codec_name"] for s in json.loads(streams)["streams"]], ["ass"])
        self.assertEqual(_yellow(_pixels(out, 1.0, BOTTOM)), 0)  # a stream, not burned in
        extracted = subprocess.run([FFMPEG, "-v", "error", "-i", out, "-map", "0:s:0", "-f", "ass", "-"],
                                   capture_output=True, text=True, check=True).stdout
        self.assertIn("Style: Sign", extracted)
        self.assertIn("\\pos(160,30)", extracted)
        sidecar = _export(_project(), "styled_sidecar.mp4", subtitle_mode="sidecar")
        with open(os.path.splitext(sidecar)[0] + ".ass", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Style: Default,Arial,28", text)
        self.assertIn("Dialogue: 0,0:00:00.50,0:00:01.50,Sign", text)
        self.assertFalse(os.path.exists(os.path.splitext(sidecar)[0] + ".srt"))

    def test_the_accurate_frame_preview_shows_the_styled_lines(self):
        project = _project()
        with patch("core.frame_render.find_binary", return_value=FFMPEG):
            frame = render_frame(project, 1.0, (W, H))
        self.assertGreater(_magenta(_png_pixels(frame, TOP)), 30)
        self.assertGreater(_yellow(_png_pixels(frame, BOTTOM)), 50)

    def test_temp_scripts_are_removed(self):
        _export(_project(), "styled_clean.mp4")
        leftovers = [n for n in os.listdir(tempfile.gettempdir()) if n.startswith("ve_text_") and n.endswith(".ass")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
