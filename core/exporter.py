"""Renders a :class:`~core.project.Project` to a final video file via FFmpeg."""

from __future__ import annotations

import glob
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from core.ffmpeg_utils import find_binary, popen
from core.filter_graph import GraphResult, build_filter_graph
from core.project import Project, ProjectSettings

ProgressCallback = Callable[[float, str], None]
"""Called with (fraction_complete 0..1, status_message)."""

_TIME_RE = re.compile(r"out_time_ms=(\d+)")
_SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")

# Presets balance quality vs. speed/size for common delivery targets.
QUALITY_PRESETS = {
    "High (CRF 18)": "18",
    "Balanced (CRF 23)": "23",
    "Smaller File (CRF 28)": "28",
}

CONTAINER_CODECS = {
    ".mp4": ("libx264", "aac"),
    ".mov": ("libx264", "aac"),
    ".mkv": ("libx264", "aac"),
    ".avi": ("libx264", "aac"),
    ".flv": ("libx264", "aac"),
    ".webm": ("libvpx-vp9", "libopus"),
}

# Handled separately from CONTAINER_CODECS: GIF has no audio and uses its
# own palette-based encode (see Exporter._export_gif), not a CRF video codec.
GIF_EXTENSION = ".gif"

AUDIO_BITRATE_KBPS = 192
MIN_VIDEO_BITRATE_KBPS = 150


@dataclass
class ExportSettings:
    output_path: str
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    quality_preset: str = "Balanced (CRF 23)"
    # Two-pass, target-filesize encoding. Only takes effect when both
    # `two_pass` is set and `target_size_mb` is a positive number; otherwise
    # the export falls back to the normal single-pass CRF encode.
    two_pass: bool = False
    target_size_mb: Optional[float] = None


class ExportError(RuntimeError):
    pass


class ExportCancelled(Exception):
    pass


class Exporter:
    """Renders a project to disk, reporting progress via callback."""

    def __init__(self, project: Project):
        self.project = project
        self._cancelled = False
        self._process: Optional[subprocess.Popen] = None

    def cancel(self) -> None:
        """Request cancellation. Safe to call from another thread.

        Terminates the in-flight FFmpeg process immediately rather than
        waiting for the run loop to next notice ``_cancelled`` — FFmpeg can
        go quiet for a while (e.g. during the first pass of a two-pass
        encode), and callers that need to tear down promptly (closing the
        export dialog) shouldn't have to wait for that.
        """
        self._cancelled = True
        if self._process is not None and self._process.poll() is None:
            try:
                self._process.terminate()
            except OSError:
                pass

    def export(self, settings: ExportSettings, on_progress: Optional[ProgressCallback] = None) -> str:
        """Render synchronously. Intended to be called from a worker thread.

        Returns the output path on success. Raises :class:`ExportError` on
        failure or :class:`ExportCancelled` if :meth:`cancel` was called.
        """
        self._cancelled = False

        project_settings = ProjectSettings(
            width=settings.width,
            height=settings.height,
            fps=settings.fps,
            sample_rate=self.project.settings.sample_rate,
        )
        graph = build_filter_graph(self.project, project_settings)

        ext = os.path.splitext(settings.output_path)[1].lower()
        total_duration = max(self.project.total_duration, 0.01)

        if ext == GIF_EXTENSION:
            # No audio track, no CRF/bitrate concept, no two-pass, and no
            # chapters — GIF is a fundamentally different (palette-based)
            # encode with no real concept of chapter markers.
            output_path = self._export_gif(settings, graph, total_duration, on_progress)
        else:
            video_codec, audio_codec = CONTAINER_CODECS.get(ext, ("libx264", "aac"))
            chapters_args, chapters_index, chapters_path = self._build_chapters_args(graph, total_duration)
            try:
                use_two_pass = settings.two_pass and settings.target_size_mb and settings.target_size_mb > 0
                if use_two_pass:
                    video_kbps = self._target_video_bitrate_kbps(settings.target_size_mb, total_duration, graph.has_audio)
                    output_path = self._export_two_pass(
                        settings, graph, ext, video_codec, audio_codec, video_kbps, total_duration, on_progress,
                        chapters_args, chapters_index,
                    )
                else:
                    output_path = self._export_single_pass(
                        settings, graph, ext, video_codec, audio_codec, total_duration, on_progress,
                        chapters_args, chapters_index,
                    )
            finally:
                if chapters_path is not None:
                    try:
                        os.remove(chapters_path)
                    except OSError:
                        pass

        if on_progress:
            on_progress(1.0, "Done")
        return output_path

    # ------------------------------------------------------------------
    @staticmethod
    def _target_video_bitrate_kbps(target_size_mb: float, duration_s: float, has_audio: bool) -> int:
        total_kbps = (target_size_mb * 8 * 1024) / duration_s
        audio_kbps = AUDIO_BITRATE_KBPS if has_audio else 0
        return max(MIN_VIDEO_BITRATE_KBPS, int(total_kbps - audio_kbps))

    def _common_args(self, graph: GraphResult, chapters_args: Optional[List[str]] = None) -> List[str]:
        ffmpeg = find_binary("ffmpeg")
        args = [ffmpeg, "-y"]
        for spec in graph.input_specs:
            args += spec.pre_args
            args += ["-i", spec.path]
        if chapters_args:
            # The chapters ffmetadata pseudo-input (see _build_chapters_args)
            # must be declared alongside the real inputs, before
            # -filter_complex — it has no A/V streams of its own, so it
            # doesn't shift any [N:v]/[N:a] index the filter graph already
            # references.
            args += chapters_args
        args += ["-filter_complex", graph.filter_complex]
        return args

    def _write_chapters_file(self, total_duration: float) -> str:
        """Write an FFMETADATA chapters file from the project's markers.

        Each marker becomes one chapter spanning from its own time to the
        next marker's time (or the project's end, for the last one).
        """
        lines = [";FFMETADATA1"]
        markers = self.project.sorted_markers()
        for i, marker in enumerate(markers):
            start_ms = int(max(0.0, marker.time) * 1000)
            end_time = markers[i + 1].time if i + 1 < len(markers) else total_duration
            end_ms = max(start_ms, int(end_time * 1000))
            # FFMETADATA is a plain key=value text format — '=', ';', '#',
            # and a literal backslash are its own escape-worthy metacharacters.
            title = re.sub(r"([=;#\\])", r"\\\1", marker.name.replace("\n", " ")) or "Chapter"
            lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start_ms}", f"END={end_ms}", f"title={title}"]

        fd, path = tempfile.mkstemp(suffix=".txt", prefix="ve_chapters_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def _build_chapters_args(
        self, graph: GraphResult, total_duration: float
    ) -> Tuple[List[str], Optional[int], Optional[str]]:
        """Returns (extra -i args for the chapters pseudo-input, its input
        index for -map_metadata, and the temp file path to delete
        afterward) — ``([], None, None)`` when the project has no markers.
        """
        if not self.project.markers:
            return [], None, None
        chapters_path = self._write_chapters_file(total_duration)
        chapters_index = len(graph.input_specs)
        return ["-f", "ffmetadata", "-i", chapters_path], chapters_index, chapters_path

    def _export_single_pass(
        self,
        settings: ExportSettings,
        graph: GraphResult,
        ext: str,
        video_codec: str,
        audio_codec: str,
        total_duration: float,
        on_progress: Optional[ProgressCallback],
        chapters_args: Optional[List[str]] = None,
        chapters_index: Optional[int] = None,
    ) -> str:
        crf = QUALITY_PRESETS.get(settings.quality_preset, "23")

        args = self._common_args(graph, chapters_args)
        args += ["-map", f"[{graph.video_out_label}]"]
        if graph.has_audio:
            args += ["-map", f"[{graph.audio_out_label}]"]

        args += ["-c:v", video_codec]
        if video_codec == "libx264":
            args += ["-crf", crf, "-preset", "medium", "-pix_fmt", "yuv420p"]
        elif video_codec == "libvpx-vp9":
            args += ["-crf", crf, "-b:v", "0"]

        if graph.has_audio:
            args += ["-c:a", audio_codec, "-b:a", f"{AUDIO_BITRATE_KBPS}k"]

        if chapters_index is not None:
            args += ["-map_metadata", str(chapters_index)]
        args += ["-r", str(settings.fps)]
        args += ["-movflags", "+faststart"] if ext == ".mp4" else []
        args += ["-progress", "pipe:1", "-nostats", settings.output_path]

        self._run_ffmpeg(args, total_duration, on_progress, progress_range=(0.0, 1.0), label="Rendering")
        return settings.output_path

    def _export_gif(
        self,
        settings: ExportSettings,
        graph: GraphResult,
        total_duration: float,
        on_progress: Optional[ProgressCallback],
    ) -> str:
        """Encode to an animated GIF via FFmpeg's two-pass palette filters.

        A GIF's 256-color palette makes the default per-frame quantizer look
        noticeably banded; generating one palette for the whole clip
        (``palettegen``) and dithering against it (``paletteuse``) is the
        standard, much higher quality approach. GIF has no audio track and
        no CRF/bitrate concept, so neither is touched here.
        """
        args = self._common_args(graph)
        # _common_args() always appends exactly one "-filter_complex <value>"
        # pair; splice the palette stage onto the end of that same value
        # rather than adding a second -filter_complex (FFmpeg only honors
        # the last one). build_filter_graph() always produces an audio pad
        # (even a silent anullsrc one when nothing is audible) — since we
        # only map [gifout], that pad would otherwise be "unconnected" and
        # FFmpeg refuses to run at all; anullsink discards it cleanly.
        filter_index = args.index("-filter_complex") + 1
        args[filter_index] += (
            f";[{graph.video_out_label}]split[gifsrc1][gifsrc2]"
            ";[gifsrc1]palettegen=stats_mode=diff[gifpal]"
            ";[gifsrc2][gifpal]paletteuse=dither=bayer:bayer_scale=3[gifout]"
            f";[{graph.audio_out_label}]anullsink"
        )
        args += ["-map", "[gifout]", "-loop", "0", "-r", str(settings.fps)]
        args += ["-progress", "pipe:1", "-nostats", settings.output_path]

        self._run_ffmpeg(args, total_duration, on_progress, progress_range=(0.0, 1.0), label="Rendering GIF")
        return settings.output_path

    def _export_two_pass(
        self,
        settings: ExportSettings,
        graph: GraphResult,
        ext: str,
        video_codec: str,
        audio_codec: str,
        video_kbps: int,
        total_duration: float,
        on_progress: Optional[ProgressCallback],
        chapters_args: Optional[List[str]] = None,
        chapters_index: Optional[int] = None,
    ) -> str:
        passlog_prefix = os.path.join(
            os.path.dirname(os.path.abspath(settings.output_path)) or ".",
            f".{os.path.splitext(os.path.basename(settings.output_path))[0]}-2pass",
        )
        null_output = os.devnull

        try:
            # ---- Pass 1: analyze only, no audio, discard the output ----
            pass1_args = self._common_args(graph)
            if graph.has_audio:
                # build_filter_graph() always produces an audio pad (even a
                # silent anullsrc one when nothing is audible), and pass 1
                # never maps it (-an, video only) — an unconsumed named
                # filtergraph output makes FFmpeg refuse to run at all
                # ("Filter ... has output ... unconnected"). Same class of
                # bug as GIF export's anullsink fix; anullsink discards it
                # cleanly here too. (This was a real, pre-existing bug: two-
                # pass export was broken for every project, not just ones
                # with markers — caught while separately verifying that
                # chapter embedding didn't break two-pass, not by a test
                # aimed at two-pass itself.)
                filter_index = pass1_args.index("-filter_complex") + 1
                pass1_args[filter_index] += f";[{graph.audio_out_label}]anullsink"
            pass1_args += ["-map", f"[{graph.video_out_label}]"]
            pass1_args += [
                "-c:v", video_codec,
                "-b:v", f"{video_kbps}k",
                "-pass", "1",
                "-passlogfile", passlog_prefix,
                "-an",
                "-r", str(settings.fps),
                "-f", "null",
                "-progress", "pipe:1", "-nostats",
                null_output,
            ]
            self._run_ffmpeg(
                pass1_args, total_duration, on_progress, progress_range=(0.0, 0.5), label="Analyzing (pass 1/2)"
            )

            # ---- Pass 2: real encode at the bitrate computed for the target size ----
            pass2_args = self._common_args(graph, chapters_args)
            pass2_args += ["-map", f"[{graph.video_out_label}]"]
            if graph.has_audio:
                pass2_args += ["-map", f"[{graph.audio_out_label}]"]
            pass2_args += ["-c:v", video_codec, "-b:v", f"{video_kbps}k", "-pass", "2", "-passlogfile", passlog_prefix]
            if video_codec == "libx264":
                pass2_args += ["-preset", "medium", "-pix_fmt", "yuv420p"]
            if graph.has_audio:
                pass2_args += ["-c:a", audio_codec, "-b:a", f"{AUDIO_BITRATE_KBPS}k"]
            if chapters_index is not None:
                pass2_args += ["-map_metadata", str(chapters_index)]
            pass2_args += ["-r", str(settings.fps)]
            pass2_args += ["-movflags", "+faststart"] if ext == ".mp4" else []
            pass2_args += ["-progress", "pipe:1", "-nostats", settings.output_path]

            self._run_ffmpeg(
                pass2_args, total_duration, on_progress, progress_range=(0.5, 1.0), label="Rendering (pass 2/2)"
            )
        finally:
            for leftover in glob.glob(passlog_prefix + "*"):
                try:
                    os.remove(leftover)
                except OSError:
                    pass

        return settings.output_path

    def _run_ffmpeg(
        self,
        args: List[str],
        total_duration: float,
        on_progress: Optional[ProgressCallback],
        progress_range: tuple,
        label: str,
    ) -> None:
        if self._cancelled:
            raise ExportCancelled()

        range_start, range_end = progress_range
        process = popen(args)
        self._process = process
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if self._cancelled:
                    process.terminate()
                    raise ExportCancelled()
                match = _TIME_RE.search(line)
                if match and on_progress:
                    out_time_s = int(match.group(1)) / 1_000_000
                    local_fraction = min(1.0, out_time_s / total_duration)
                    fraction = range_start + local_fraction * (range_end - range_start)
                    on_progress(fraction, f"{label}... {local_fraction * 100:.0f}%")
            process.wait()
        finally:
            if process.stdout:
                process.stdout.close()
            self._process = None

        if self._cancelled:
            raise ExportCancelled()

        if process.returncode != 0:
            raise ExportError(
                f"FFmpeg exited with code {process.returncode}. "
                "Check that all source files are valid and accessible."
            )
