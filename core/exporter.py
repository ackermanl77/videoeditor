"""Renders a :class:`~core.project.Project` to a final video file via FFmpeg."""

from __future__ import annotations

import dataclasses
import glob
import os
import re
import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from core.clip import ReframeMode
from core.ffmpeg_utils import find_binary, nvenc_available, popen
from core.filter_graph import GraphResult, build_filter_graph
from core.project import Project, ProjectSettings

ProgressCallback = Callable[[float, str], None]
"""Called with (fraction_complete 0..1, status_message)."""

_TIME_RE = re.compile(r"out_time_ms=(\d+)")
_SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")

# `-progress pipe:1` key=value lines, interleaved with FFmpeg's own log output
# on the merged stdout/stderr pipe (see core.ffmpeg_utils.popen). Filtered out
# of the error-log tail kept for ExportError, which only wants real log lines.
_PROGRESS_LINE_RE = re.compile(
    r"^(frame|fps|stream_\d+_\d+_q|bitrate|total_size|out_time_us|out_time_ms|out_time"
    r"|dup_frames|drop_frames|speed|progress)="
)

# How many trailing FFmpeg log lines an ExportError carries.
ERROR_LOG_TAIL_LINES = 30

# FFmpeg log text that marks a failure as NVENC/GPU-specific (driver missing
# or too old, no capable device, session limit reached, GPU out of memory...)
# rather than a problem with the project itself -- see
# Exporter._is_gpu_specific_failure.
_GPU_FAILURE_RE = re.compile(
    r"nvenc|nvcuda|cuda|no capable devices|OpenEncodeSession|out of memory|driver does not support",
    re.IGNORECASE,
)

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

# GPU (NVENC) equivalent of each CPU codec CONTAINER_CODECS can select, used
# by Exporter._resolve_video_codec() when ExportSettings.use_gpu is set and
# core.ffmpeg_utils.nvenc_available() confirms this ffmpeg build/driver
# actually has it. Only libx264 has an entry: nothing in CONTAINER_CODECS
# currently maps to libx265/HEVC, and VP9 (.webm) has no NVENC counterpart
# worth relying on here, so GPU encoding is single-pass MP4/MOV/MKV/AVI/FLV
# only for now. Add "libx265": "hevc_nvenc" here the day an HEVC output
# option is added to CONTAINER_CODECS -- no other code path needs to change.
NVENC_CODEC_MAP = {
    "libx264": "h264_nvenc",
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
    # Shorts/vertical export support. `reframe_override`, when set, forces
    # every video/image clip's reframe mode for this export only (see
    # core.filter_graph._project_with_reframe_override) instead of requiring
    # each clip's own Fill Frame setting to be turned on individually.
    # `trim_start`/`trim_end` cut the rendered output down to that range
    # (`trim_end=None` means "to the end") — used for the ≤60s duration cap.
    reframe_override: Optional[ReframeMode] = None
    trim_start: float = 0.0
    trim_end: Optional[float] = None
    # Prefer NVENC hardware encoding over the CPU codec (libx264) when this
    # machine's ffmpeg build/driver actually has it (see
    # core.ffmpeg_utils.nvenc_available). Single-pass exports only -- see
    # NVENC_CODEC_MAP above and Exporter._export_two_pass, which always uses
    # the CPU codec regardless of this flag. If NVENC is selected but the
    # real encode fails (e.g. a driver issue, or another process holding the
    # GPU's limited concurrent session count), Exporter transparently retries
    # once with the CPU codec rather than failing the export outright.
    use_gpu: bool = True


class ExportError(RuntimeError):
    """An export failed. ``log_tail`` holds the last FFmpeg log lines, if any
    (already included in ``str(exc)``, kept separately for callers that want
    to show them differently). ``encoded_seconds`` is how far FFmpeg got
    before failing, per its own progress reports."""

    def __init__(self, message: str, log_tail: Optional[List[str]] = None, encoded_seconds: float = 0.0):
        self.log_tail = list(log_tail or [])
        self.encoded_seconds = encoded_seconds
        if self.log_tail:
            message = f"{message}\n\nFFmpeg output (last {len(self.log_tail)} lines):\n" + "\n".join(self.log_tail)
        super().__init__(message)


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
        full_duration = max(self.project.total_duration, 0.01)
        trim_start = max(0.0, settings.trim_start)
        trim_end = full_duration if settings.trim_end is None else min(settings.trim_end, full_duration)
        is_trimmed = trim_start > 0.0 or trim_end < full_duration
        trim_range = (trim_start, trim_end) if is_trimmed else None

        graph = build_filter_graph(
            self.project, project_settings, reframe_override=settings.reframe_override, trim_range=trim_range
        )
        try:
            self._check_sources_exist(graph)
            final_path = settings.output_path
            partial_path = self._partial_output_path(final_path)
            render_settings = dataclasses.replace(settings, output_path=partial_path)
            try:
                self._render(render_settings, graph, full_duration, trim_start, trim_end, is_trimmed, on_progress)
                if not os.path.isfile(partial_path):
                    raise ExportError("FFmpeg reported success but did not produce an output file.")
                os.replace(partial_path, final_path)
            except BaseException:
                # Failure or cancellation: never leave a truncated file
                # behind, and never touch an existing file at the real
                # destination (it's only replaced on success, above).
                self._remove_quietly(partial_path)
                raise
        finally:
            graph.cleanup()

        if on_progress:
            on_progress(1.0, "Done")
        return final_path

    @staticmethod
    def _partial_output_path(output_path: str) -> str:
        """Temp render target next to ``output_path``: ``name.partial.mp4``.

        Same folder (so the final ``os.replace`` is an atomic same-volume
        rename) and the real extension last (FFmpeg picks the muxer from
        it). Rendering straight to the destination used to leave a
        truncated, unplayable file there -- or clobber a good previous
        export -- whenever an export failed or was cancelled.
        """
        stem, ext = os.path.splitext(output_path)
        return f"{stem}.partial{ext}"

    @staticmethod
    def _remove_quietly(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    @staticmethod
    def _check_sources_exist(graph: GraphResult) -> None:
        """Fail fast, with a clear list, if any source file FFmpeg would open is missing.

        Checks exactly the graph's ``-i`` inputs (so a hidden-and-muted
        track's clips, which aren't rendered, don't count) -- otherwise the
        user gets only FFmpeg's own error for the first missing file,
        after a render may already have started.
        """
        missing = sorted({spec.path for spec in graph.input_specs if not os.path.isfile(spec.path)})
        if missing:
            shown = "\n".join(missing[:10])
            more = f"\n...and {len(missing) - 10} more" if len(missing) > 10 else ""
            raise ExportError(
                f"{len(missing)} source file(s) used by the timeline could not be found:\n{shown}{more}\n\n"
                "Restore them at their original location, then export again."
            )

    def _render(
        self,
        settings: ExportSettings,
        graph: GraphResult,
        full_duration: float,
        trim_start: float,
        trim_end: float,
        is_trimmed: bool,
        on_progress: Optional[ProgressCallback],
    ) -> str:
        """Run the actual encode(s) into ``settings.output_path``."""
        ext = os.path.splitext(settings.output_path)[1].lower()
        # The output's actual rendered length — used for FFmpeg progress
        # fraction, distinct from `full_duration` (the untrimmed project
        # length chapters are anchored against).
        total_duration = max(trim_end - trim_start, 0.01) if is_trimmed else full_duration

        if ext == GIF_EXTENSION:
            # No audio track, no CRF/bitrate concept, no two-pass, and no
            # chapters — GIF is a fundamentally different (palette-based)
            # encode with no real concept of chapter markers.
            output_path = self._export_gif(settings, graph, total_duration, on_progress)
        else:
            video_codec, audio_codec = CONTAINER_CODECS.get(ext, ("libx264", "aac"))
            chapters_args, chapters_index, chapters_path = self._build_chapters_args(
                graph, full_duration, trim_start, trim_end if is_trimmed else None
            )
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

    def _write_chapters_file(
        self, total_duration: float, trim_start: float = 0.0, trim_end: Optional[float] = None
    ) -> str:
        """Write an FFMETADATA chapters file from the project's markers.

        Each marker becomes one chapter spanning from its own time to the
        next marker's time (or the project's end, for the last one). Marker
        times are in the untrimmed project's own timeline; when
        ``trim_start``/``trim_end`` narrow the actual rendered output (e.g.
        the Shorts duration cap), each chapter is clipped to that range and
        re-anchored to it (so a chapter starting before the trim point
        starts at 0 in the output, not at a negative time), and any chapter
        that falls entirely outside the range is dropped.
        """
        lines = [";FFMETADATA1"]
        markers = self.project.sorted_markers()
        effective_end = total_duration if trim_end is None else trim_end
        for i, marker in enumerate(markers):
            marker_end = markers[i + 1].time if i + 1 < len(markers) else effective_end
            seg_start = max(marker.time, trim_start)
            seg_end = min(marker_end, effective_end)
            if seg_end <= seg_start:
                continue
            start_ms = int((seg_start - trim_start) * 1000)
            end_ms = max(start_ms, int((seg_end - trim_start) * 1000))
            # FFMETADATA is a plain key=value text format — '=', ';', '#',
            # and a literal backslash are its own escape-worthy metacharacters.
            title = re.sub(r"([=;#\\])", r"\\\1", marker.name.replace("\n", " ")) or "Chapter"
            lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start_ms}", f"END={end_ms}", f"title={title}"]

        fd, path = tempfile.mkstemp(suffix=".txt", prefix="ve_chapters_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def _build_chapters_args(
        self, graph: GraphResult, total_duration: float, trim_start: float = 0.0, trim_end: Optional[float] = None
    ) -> Tuple[List[str], Optional[int], Optional[str]]:
        """Returns (extra -i args for the chapters pseudo-input, its input
        index for -map_metadata, and the temp file path to delete
        afterward) — ``([], None, None)`` when the project has no markers.
        """
        if not self.project.markers:
            return [], None, None
        chapters_path = self._write_chapters_file(total_duration, trim_start, trim_end)
        chapters_index = len(graph.input_specs)
        return ["-f", "ffmetadata", "-i", chapters_path], chapters_index, chapters_path

    @staticmethod
    def _resolve_video_codec(base_codec: str, prefer_gpu: bool) -> Tuple[str, bool]:
        """Returns ``(codec_to_use, is_gpu)``.

        Falls back to the CPU codec whenever GPU encoding wasn't requested,
        there's no NVENC equivalent for this container's codec (see
        NVENC_CODEC_MAP), or NVENC isn't actually available on this
        machine's ffmpeg build/driver (probed once and cached -- see
        core.ffmpeg_utils.nvenc_available).
        """
        if not prefer_gpu:
            return base_codec, False
        nvenc_codec = NVENC_CODEC_MAP.get(base_codec)
        if nvenc_codec is None or not nvenc_available():
            return base_codec, False
        return nvenc_codec, True

    @staticmethod
    def _video_quality_args(codec: str, is_gpu: bool, crf: str) -> List[str]:
        """Quality-control flags for one video codec.

        NVENC doesn't understand ``-crf`` (a libx264/libvpx-only rate-control
        mode) -- its nearest equivalent is ``-rc vbr -cq <value>`` (constant
        quality within a VBR envelope, ``-b:v 0`` letting ``-cq`` alone drive
        the bitrate) plus a ``-preset``, using the same p1(fastest)-p7(best)
        naming as ffmpeg's own NVENC preset scale. NVENC's ``-cq`` uses the
        same 0-51 lower-is-better scale as x264's ``-crf``, so the existing
        CRF preset numbers (18/23/28) carry over directly as sensible ``-cq``
        values rather than needing a separate mapping table.
        """
        if is_gpu:
            return ["-preset", "p4", "-rc:v", "vbr", "-cq:v", crf, "-b:v", "0", "-pix_fmt", "yuv420p"]
        if codec == "libx264":
            return ["-crf", crf, "-preset", "medium", "-pix_fmt", "yuv420p"]
        if codec == "libvpx-vp9":
            return ["-crf", crf, "-b:v", "0"]
        return []

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

        def build_args(codec: str, is_gpu: bool) -> List[str]:
            args = self._common_args(graph, chapters_args)
            args += ["-map", f"[{graph.video_out_label}]"]
            if graph.has_audio:
                args += ["-map", f"[{graph.audio_out_label}]"]

            args += ["-c:v", codec]
            args += self._video_quality_args(codec, is_gpu, crf)

            if graph.has_audio:
                args += ["-c:a", audio_codec, "-b:a", f"{AUDIO_BITRATE_KBPS}k"]

            if chapters_index is not None:
                args += ["-map_metadata", str(chapters_index)]
            args += ["-r", str(settings.fps)]
            args += ["-movflags", "+faststart"] if ext == ".mp4" else []
            args += ["-progress", "pipe:1", "-nostats", settings.output_path]
            return args

        effective_codec, using_gpu = self._resolve_video_codec(video_codec, settings.use_gpu)
        args = build_args(effective_codec, using_gpu)
        label = "Rendering (GPU)" if using_gpu else "Rendering"
        try:
            self._run_ffmpeg(args, total_duration, on_progress, progress_range=(0.0, 1.0), label=label)
        except ExportError as exc:
            # NVENC can fail for reasons that have nothing to do with the
            # project itself (a driver issue, another process holding the
            # GPU's limited concurrent NVENC session count, etc.) -- retry
            # once with the CPU codec rather than failing the whole export.
            # Only for failures that look GPU-specific, though: re-rendering
            # the whole project after e.g. a disk-full or bad-media error
            # deep into a long export just doubles the wait for the same error.
            if not using_gpu or self._cancelled or not self._is_gpu_specific_failure(exc):
                raise
            if on_progress:
                on_progress(0.0, "GPU encoding failed -- falling back to CPU encoder...")
            fallback_args = build_args(video_codec, False)
            self._run_ffmpeg(
                fallback_args, total_duration, on_progress, progress_range=(0.0, 1.0), label="Rendering (CPU fallback)"
            )
        return settings.output_path

    @staticmethod
    def _is_gpu_specific_failure(exc: "ExportError") -> bool:
        """Whether a failed NVENC encode is worth retrying on the CPU codec.

        True when FFmpeg's log mentions NVENC/CUDA/driver-level trouble, or
        when it failed before encoding any output at all (encoder/session
        initialisation is where NVENC problems surface; a retry that fails
        equally early costs next to nothing). A failure after real progress
        with no GPU-related text is treated as a project/media/disk problem.
        """
        if any(_GPU_FAILURE_RE.search(line) for line in exc.log_tail):
            return True
        return exc.encoded_seconds <= 0.0

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
        log_tail: deque = deque(maxlen=ERROR_LOG_TAIL_LINES)
        encoded_seconds = 0.0
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if self._cancelled:
                    process.terminate()
                    raise ExportCancelled()
                match = _TIME_RE.search(line)
                if match:
                    out_time_s = int(match.group(1)) / 1_000_000
                    encoded_seconds = max(encoded_seconds, out_time_s)
                    if on_progress:
                        local_fraction = min(1.0, out_time_s / total_duration)
                        fraction = range_start + local_fraction * (range_end - range_start)
                        on_progress(fraction, f"{label}... {local_fraction * 100:.0f}%")
                elif not _PROGRESS_LINE_RE.match(line):
                    stripped = line.rstrip()
                    if stripped:
                        log_tail.append(stripped)
            process.wait()
        finally:
            if process.stdout:
                process.stdout.close()
            self._process = None

        if self._cancelled:
            raise ExportCancelled()

        if process.returncode != 0:
            raise ExportError(
                f"FFmpeg exited with code {process.returncode} ({label}).",
                log_tail=list(log_tail),
                encoded_seconds=encoded_seconds,
            )
