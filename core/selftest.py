"""``--self-test``: check a build can find and use its FFmpeg, without starting the GUI.

Run as ``VideoEditor.exe --self-test`` (or ``python main.py --self-test``)
before publishing a release. Prints the version, the FFmpeg/FFprobe paths
and versions, NVENC availability and the filters the export engine needs,
then renders a one-second lavfi-generated project (video, audio, text)
through :class:`core.exporter.Exporter` and checks the result with ffprobe.
Exit code 0 means every check passed.

A windowed (no-console) build has no stdout, so the report is also written
to ``%TEMP%/VideoEditor-selftest.txt`` (path printed in the report).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
from typing import Callable, List

from core import __version__

REQUIRED_FILTERS = (
    "drawtext", "xfade", "overlay", "amix", "alimiter", "loudnorm", "sidechaincompress", "afftdn",
    "colorkey", "chromakey", "despill", "zscale", "tonemap", "lut3d", "unsharp", "gblur", "vignette", "hqdn3d",
)
REPORT_NAME = "VideoEditor-selftest.txt"


def _filters_available(ffmpeg: str) -> List[str]:
    from core.ffmpeg_utils import run

    output = run([ffmpeg, "-hide_banner", "-filters"], timeout=30).stdout or ""
    names = {line.split()[1] for line in output.splitlines() if len(line.split()) > 2 and line.startswith(" ")}
    return [name for name in REQUIRED_FILTERS if name not in names]


def _render_check(ffmpeg: str, ffprobe: str, workdir: str, log: Callable[[str], None]) -> bool:
    from core.clip import Clip, MediaType, TextStyle
    from core.exporter import ExportSettings, Exporter
    from core.ffmpeg_utils import run
    from core.media_asset import MediaAsset
    from core.project import Project

    source = os.path.join(workdir, "source.mp4")
    made = run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=1", "-f", "lavfi", "-i", "sine=frequency=440:d=1",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", source],
        timeout=60,
    )
    if made.returncode != 0:
        log(f"FAIL  could not generate a test clip: {(made.stderr or '').strip()[-300:]}")
        return False
    project = Project(name="selftest")
    project.add_media_asset(MediaAsset(path=source, duration=1.0, width=320, height=180, has_video=True, has_audio=True))
    project.tracks[0].clips.append(Clip(media_type=MediaType.VIDEO, source_path=source, source_out=1.0))
    project.tracks[0].clips.append(
        Clip(media_type=MediaType.TEXT, source_out=1.0, text_style=TextStyle(content="Self-test Árbol"))
    )
    output = os.path.join(workdir, "out.mp4")
    Exporter(project).export(ExportSettings(output_path=output, width=320, height=180, fps=30.0, use_gpu=False))
    probe = run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", output], timeout=30)
    data = json.loads(probe.stdout or "{}")
    duration = float(data.get("format", {}).get("duration", 0.0))
    kinds = sorted(s.get("codec_type") for s in data.get("streams", []))
    if abs(duration - 1.0) > 0.1 or kinds != ["audio", "video"]:
        log(f"FAIL  test export: duration {duration:.3f} s, streams {kinds}")
        return False
    log(f"ok    test export: {duration:.3f} s, streams {kinds}")
    return True


def run_self_test(out: Callable[[str], None] = print) -> int:
    lines: List[str] = []

    def log(line: str) -> None:
        lines.append(line)
        try:
            out(line)
        except Exception:  # noqa: BLE001 - a windowed build may have no usable stdout
            pass

    ok = True
    log(f"Open Video Editor {__version__} self-test")
    log(f"Python {sys.version.split()[0]} ({'frozen' if getattr(sys, 'frozen', False) else 'source'})")
    workdir = tempfile.mkdtemp(prefix="ve_selftest_")
    try:
        from core.ffmpeg_utils import binary_version, find_binary, nvenc_available

        ffmpeg, ffprobe = find_binary("ffmpeg"), find_binary("ffprobe")
        log(f"ok    ffmpeg:  {ffmpeg}  ({binary_version('ffmpeg') or 'version unknown'})")
        log(f"ok    ffprobe: {ffprobe}  ({binary_version('ffprobe') or 'version unknown'})")
        log(f"info  NVENC available: {'yes' if nvenc_available() else 'no'}")
        missing = _filters_available(ffmpeg)
        if missing:
            ok = False
            log(f"FAIL  filters missing from this FFmpeg build: {', '.join(missing)}")
        else:
            log(f"ok    all {len(REQUIRED_FILTERS)} required filters present")
        ok = _render_check(ffmpeg, ffprobe, workdir, log) and ok
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        ok = False
        log(f"FAIL  {exc}")
        log(traceback.format_exc().strip())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    log("RESULT: PASS" if ok else "RESULT: FAIL")
    report = os.path.join(tempfile.gettempdir(), REPORT_NAME)
    try:
        with open(report, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        try:
            out(f"(report written to {report})")
        except Exception:  # noqa: BLE001
            pass
    except OSError:
        pass
    return 0 if ok else 1
