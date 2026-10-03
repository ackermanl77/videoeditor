"""Renders a :class:`~core.project.Project` to a final video file via FFmpeg."""

from __future__ import annotations

import dataclasses
import glob
import json
import math
import os
import re
import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from core.clip import MediaType, ReframeMode
from core.effects import effect_files
from core.ffmpeg_utils import (
    apply_decode_hwaccel,
    decode_hwaccel_modes,
    encoder_supports_10bit,
    externalize_filter_graph,
    find_binary,
    is_hw_decode_failure,
    nvenc_available,
    nvenc_encoder_available,
    popen,
    remove_quietly,
)
from core.filter_graph import GraphResult, build_filter_graph, fps_expr
from core.output_color import SDR_8BIT, OutputColor
from core.project import Project, ProjectSettings
from core.stabilize import StabilizeJob, detect_args, partial_path, pending_jobs

ProgressCallback = Callable[[float, str], None]
"""Called with (fraction_complete 0..1, status_message)."""

_TIME_RE = re.compile(r"out_time_ms=(\d+)")
_SPEED_RE = re.compile(r"^speed=\s*([0-9.]+)x")

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
    r"nvenc|nvcuda|cuda|d3d11va|hwaccel|no capable devices|OpenEncodeSession|out of memory|driver does not support",
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

# Video codec choices offered by the Export dialog ("auto" = the container's
# default from CONTAINER_CODECS), the CPU encoder each one uses, and the
# containers it can be written to.
VIDEO_CODEC_CHOICES = {
    "auto": "Container default",
    "h264": "H.264",
    "hevc": "HEVC (H.265)",
    "av1": "AV1",
    "prores": "ProRes 422 HQ (intermediate)",
    "dnxhr": "DNxHR HQ (intermediate)",
}
CODEC_ENCODERS = {
    "h264": "libx264",
    "hevc": "libx265",
    "av1": "libaom-av1",
    "prores": "prores_ks",
    "dnxhr": "dnxhd",
}
CODEC_CONTAINERS = {
    "h264": (".mp4", ".mov", ".mkv", ".avi", ".flv"),
    "hevc": (".mp4", ".mov", ".mkv"),
    "av1": (".mp4", ".mkv", ".webm"),
    "prores": (".mov", ".mkv"),
    "dnxhr": (".mov", ".mkv"),
}
# Intermediate codecs carry uncompressed PCM audio, as editing intermediates do.
INTERMEDIATE_ENCODERS = ("prores_ks", "dnxhd")
# Target-size (two-pass) needs a codec with a real two-pass mode here.
TWO_PASS_ENCODERS = ("libx264", "libvpx-vp9", "libaom-av1")

# GPU (NVENC) equivalent of each CPU encoder, used by
# Exporter._resolve_video_codec() when ExportSettings.use_gpu is set and the
# encoder really works on this machine (core.ffmpeg_utils.nvenc_available /
# nvenc_encoder_available: a GPU can do H.264 but not AV1). VP9 (.webm) and
# the intermediates have no NVENC counterpart.
NVENC_CODEC_MAP = {
    "libx264": "h264_nvenc",
    "libx265": "hevc_nvenc",
    "libaom-av1": "av1_nvenc",
}

# NVENC quality flags on top of preset p4. Measured on an RTX 5080 (10 s of
# 1080p, encode only): 1.03-1.24x the time of plain p4 for H.264/HEVC/AV1.
# Preset p5 with full-resolution multipass cost 1.12-1.36x, over the ~1.2x
# budget, so it isn't used.
NVENC_TUNING = [
    "-preset", "p4", "-tune", "hq", "-multipass", "qres", "-spatial-aq", "1", "-temporal-aq", "1",
    "-rc-lookahead", "20", "-b_ref_mode", "middle",
]

# Every non-GIF output is tagged with its colour target: SDR BT.709 by
# default (HDR sources are tone-mapped to it in the graph), or BT.2020 PQ/HLG
# for an HDR passthrough export (core/output_color.py). Untagged files left
# players guessing BT.601 vs BT.709 from the frame size; an HDR source used
# to come out with a BT.2020 matrix tag and no transfer at all.
BT709_OUTPUT_TAGS = SDR_8BIT.output_tag_args()

# Codec choices that can be written with 10 bits per sample (10-bit SDR and
# HDR exports). H.264 and VP9 stay 8-bit here.
TEN_BIT_CODECS = ("hevc", "av1", "prores", "dnxhr")
TEN_BIT_ENCODERS = ("libx265", "libaom-av1", "prores_ks", "dnxhd", "hevc_nvenc", "av1_nvenc")

# Handled separately from CONTAINER_CODECS: GIF has no audio and uses its
# own palette-based encode (see Exporter._export_gif), not a CRF video codec.
GIF_EXTENSION = ".gif"

AUDIO_BITRATE_KBPS = 192
AUDIO_BITRATE_OPTIONS_KBPS = (128, 192, 256, 320)
MIN_VIDEO_BITRATE_KBPS = 150

# Integrated-loudness targets offered by the export dialog (EBU R128 /
# ITU BS.1770 LUFS). Normalization is two-pass loudnorm: an audio-only
# measurement pass, then the real render with the measured values and
# linear=true (a single gain change, no dynamic compression).
LOUDNESS_TARGETS = {
    "Off": None,
    "-14 LUFS (YouTube / streaming)": -14.0,
    "-16 LUFS (podcast)": -16.0,
    "-23 LUFS (EBU R128 broadcast)": -23.0,
}
LOUDNESS_TRUE_PEAK = -1.0
LOUDNESS_LRA = 11.0


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
    # AAC/Opus bitrate for the audio track.
    audio_bitrate_kbps: int = AUDIO_BITRATE_KBPS
    # Integrated loudness target in LUFS (see LOUDNESS_TARGETS), or None to
    # leave the mix level alone (the master limiter still caps peaks).
    loudness_target: Optional[float] = None
    # Video codec (a VIDEO_CODEC_CHOICES key); "auto" = the container's default.
    video_codec: str = "auto"
    # Target-size export on the GPU: one NVENC pass with a VBR rate cap
    # (lands close to, not exactly on, the size) instead of x264 two-pass.
    gpu_target_size: bool = False
    # Colour format (core.output_color.COLOR_MODE_LABELS key): "sdr8" (the
    # default, 8-bit SDR BT.709), "sdr10" (10-bit SDR), or "pq"/"hlg" (10-bit
    # HDR passthrough for HDR sources). 10-bit needs HEVC, AV1, ProRes or DNxHR.
    color_mode: str = "sdr8"


def resolve_codecs(ext: str, choice: str = "auto") -> Tuple[str, str]:
    """``(video_encoder, audio_encoder)`` for a container extension and codec choice.

    Raises ``ValueError`` when the codec can't go in that container (e.g.
    ProRes in .mp4) -- the dialog only offers valid pairs; this guards the API.
    """
    if not choice or choice == "auto":
        return CONTAINER_CODECS.get(ext, ("libx264", "aac"))
    if choice not in CODEC_ENCODERS:
        raise ValueError(f"Unknown video codec '{choice}'.")
    if ext not in CODEC_CONTAINERS[choice]:
        raise ValueError(f"{VIDEO_CODEC_CHOICES[choice]} can't be written to a {ext} file.")
    video = CODEC_ENCODERS[choice]
    if video in INTERMEDIATE_ENCODERS:
        return video, "pcm_s16le"
    return video, ("libopus" if ext == ".webm" else "aac")


def format_eta(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


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
        # Non-fatal problems from the last export (see GraphResult.warnings).
        self.warnings: List[str] = []
        # The export's colour target and the HDR sources' static metadata
        # (set per export; defaults for callers building arguments directly).
        self._color: OutputColor = SDR_8BIT
        self._hdr_metadata: Tuple[str, str] = ("", "")

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
        ext = os.path.splitext(settings.output_path)[1].lower()
        self._color = SDR_8BIT if ext == GIF_EXTENSION else OutputColor.from_mode(settings.color_mode)
        self._hdr_metadata = self._source_hdr_metadata(self._color)

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
            self.project, project_settings, reframe_override=settings.reframe_override, trim_range=trim_range,
            output_color=self._color,
        )
        self.warnings = list(graph.warnings)
        try:
            self._check_sources_exist(graph, self._effect_files())
            self._run_stabilize_analyses(graph.stabilize_jobs, on_progress)
            if settings.loudness_target is not None and graph.has_audible_clips:
                graph = self._with_loudness_normalization(
                    graph, project_settings, settings, trim_range, on_progress
                )
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

    def _source_hdr_metadata(self, color: OutputColor) -> Tuple[str, str]:
        """``(master-display, max-cll)`` of the HDR sources in the target transfer (first found; "" if none)."""
        if not color.is_hdr:
            return "", ""
        master, cll = "", ""
        for track in self.project.tracks:
            for clip in track.clips:
                if clip.media_type != MediaType.VIDEO:
                    continue
                asset = self.project.asset_for_clip(clip)
                if asset is None or asset.color_transfer != color.transfer:
                    continue
                master = master or asset.mastering_display
                cll = cll or asset.max_cll
        return master, cll

    def _run_stabilize_analyses(self, jobs: List[StabilizeJob], on_progress: Optional[ProgressCallback]) -> None:
        """Pass 1 of stabilization (``vidstabdetect``) for every clip whose analysis isn't cached yet.

        Each result is written to a ``.partial`` file and renamed into the
        cache only when complete, so a cancelled or failed analysis never
        leaves a truncated result that a later export would trust.
        """
        todo = pending_jobs(jobs)
        for number, job in enumerate(todo, 1):
            tmp = partial_path(job)
            label = f"Analyzing motion for stabilization ({number}/{len(todo)})"
            try:
                self._run_ffmpeg(detect_args(find_binary("ffmpeg"), job, tmp), job.length, on_progress,
                                 progress_range=(0.0, 1.0), label=label)
                if not os.path.isfile(tmp):
                    raise ExportError("The stabilization analysis produced no result.")
                os.replace(tmp, job.result_path)
            finally:
                self._remove_quietly(tmp)

    def _with_loudness_normalization(
        self,
        graph: GraphResult,
        project_settings: ProjectSettings,
        settings: ExportSettings,
        trim_range: Optional[Tuple[float, float]],
        on_progress: Optional[ProgressCallback],
    ) -> GraphResult:
        """First loudnorm pass: measure the mix, then return ``graph`` with the second pass appended.

        The measurement runs on an audio-only build of the same graph
        (``build_filter_graph(audio_only=True)``), so it costs an audio
        decode/mix rather than a second video render. Returns ``graph``
        unchanged when the mix measures as silence.
        """
        target = settings.loudness_target
        measure_graph = build_filter_graph(
            self.project, project_settings, reframe_override=settings.reframe_override,
            trim_range=trim_range, audio_only=True,
        )
        try:
            args = self._common_args(measure_graph)
            filter_index = args.index("-filter_complex") + 1
            args[filter_index] += (
                f";[{measure_graph.video_out_label}]nullsink"
                f";[{measure_graph.audio_out_label}]{self._loudnorm_filter(target)}:print_format=json[lnmeasure]"
            )
            args += ["-map", "[lnmeasure]", "-f", "null", "-progress", "pipe:1", "-nostats", os.devnull]
            duration = max(project_settings_duration(self.project, trim_range), 0.01)
            log = self._run_ffmpeg(args, duration, on_progress, progress_range=(0.0, 1.0), label="Measuring loudness")
        finally:
            measure_graph.cleanup()
        measured = self._parse_loudnorm_json(log)
        if measured is None:
            raise ExportError("Could not read FFmpeg's loudness measurement.", log_tail=log)
        try:
            if not math.isfinite(float(measured["input_i"])):
                return graph  # silent mix: nothing to normalize
        except (KeyError, ValueError):
            raise ExportError("Could not read FFmpeg's loudness measurement.", log_tail=log)
        sample_rate = project_settings.sample_rate or 48000
        second_pass = (
            f"{self._loudnorm_filter(target)}"
            f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true"
            # loudnorm works (and outputs) at 192 kHz internally.
            f",aresample={sample_rate}"
        )
        return dataclasses.replace(
            graph,
            filter_complex=f"{graph.filter_complex};[{graph.audio_out_label}]{second_pass}[aloud]",
            audio_out_label="aloud",
        )

    @staticmethod
    def _loudnorm_filter(target: float) -> str:
        return f"loudnorm=I={target:.1f}:TP={LOUDNESS_TRUE_PEAK:.1f}:LRA={LOUDNESS_LRA:.1f}"

    @staticmethod
    def _parse_loudnorm_json(log_lines: List[str]) -> Optional[dict]:
        """The last ``{...}`` block loudnorm printed (``print_format=json``), or None."""
        text = "\n".join(log_lines)
        end = text.rfind("}")
        start = text.rfind("{", 0, end)
        if start < 0 or end < 0:
            return None
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None

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

    def _effect_files(self) -> List[str]:
        """Files read by enabled effects (LUTs) on any clip."""
        return [path for track in self.project.tracks for clip in track.clips for path in effect_files(clip.effects)]

    @staticmethod
    def _check_sources_exist(graph: GraphResult, extra_files: Optional[List[str]] = None) -> None:
        """Fail fast, with a clear list, if any source file FFmpeg would open is missing.

        Checks exactly the graph's ``-i`` inputs (so a hidden-and-muted
        track's clips, which aren't rendered, don't count) -- otherwise the
        user gets only FFmpeg's own error for the first missing file,
        after a render may already have started.
        """
        paths = [spec.path for spec in graph.input_specs] + list(extra_files or [])
        missing = sorted({path for path in paths if not os.path.isfile(path)})
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
            try:
                video_codec, audio_codec = resolve_codecs(ext, settings.video_codec)
            except ValueError as exc:
                raise ExportError(str(exc))
            if self._color.is_10bit and video_codec not in TEN_BIT_ENCODERS:
                raise ExportError(
                    "10-bit and HDR exports need HEVC, AV1, ProRes or DNxHR; choose one of them as the video codec."
                )
            graph = self._with_output_tags(graph, self._color)
            use_two_pass = bool(settings.two_pass and settings.target_size_mb and settings.target_size_mb > 0)
            if use_two_pass and video_codec not in TWO_PASS_ENCODERS:
                raise ExportError(
                    "Target file size needs H.264, VP9 or AV1; choose one of them or turn target size off."
                )
            chapters_args, chapters_index, chapters_path = self._build_chapters_args(
                graph, full_duration, trim_start, trim_end if is_trimmed else None
            )
            try:
                if use_two_pass:
                    video_kbps = self._target_video_bitrate_kbps(
                        settings.target_size_mb, total_duration, graph.has_audio, settings.audio_bitrate_kbps
                    )
                    output_path = None
                    if settings.use_gpu and settings.gpu_target_size:
                        output_path = self._export_gpu_target_size(
                            settings, graph, ext, video_codec, audio_codec, video_kbps, total_duration,
                            on_progress, chapters_args, chapters_index,
                        )
                    if output_path is None:
                        output_path = self._export_two_pass(
                            settings, graph, ext, video_codec, audio_codec, video_kbps, total_duration,
                            on_progress, chapters_args, chapters_index,
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

    @staticmethod
    def _with_output_tags(graph: GraphResult, color: OutputColor = SDR_8BIT) -> GraphResult:
        """Tag the final frames with the export's colour target (SDR BT.709 or BT.2020 PQ/HLG), limited range.

        Encoders in this FFmpeg take colour tags from the frames; the
        ``-colorspace``/``-color_trc`` output options alone left
        ``color_primaries``/``color_transfer`` unset in the file (verified
        with ffprobe), so the graph's output is tagged with ``setparams``.
        """
        return dataclasses.replace(
            graph,
            filter_complex=(
                f"{graph.filter_complex};[{graph.video_out_label}]{color.setparams()}[vtagged]"
            ),
            video_out_label="vtagged",
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _target_video_bitrate_kbps(
        target_size_mb: float, duration_s: float, has_audio: bool, audio_bitrate_kbps: int = AUDIO_BITRATE_KBPS
    ) -> int:
        total_kbps = (target_size_mb * 8 * 1024) / duration_s
        audio_kbps = audio_bitrate_kbps if has_audio else 0
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
    def _resolve_video_codec(base_codec: str, prefer_gpu: bool, ten_bit: bool = False) -> Tuple[str, bool]:
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
        # nvenc_available() tested H.264; other NVENC encoders get their own probe.
        if nvenc_codec != "h264_nvenc" and not nvenc_encoder_available(nvenc_codec):
            return base_codec, False
        # 10-bit NVENC needs a newer GPU than 8-bit (and AV1 needs Ada or later).
        if ten_bit and not encoder_supports_10bit(nvenc_codec):
            return base_codec, False
        return nvenc_codec, True

    @staticmethod
    def _video_quality_args(
        codec: str,
        is_gpu: bool,
        crf: str,
        color: OutputColor = SDR_8BIT,
        hdr_metadata: Tuple[str, str] = ("", ""),
    ) -> List[str]:
        """Quality-control flags for one video codec.

        NVENC doesn't understand ``-crf`` (a libx264/libvpx-only rate-control
        mode) -- its nearest equivalent is ``-rc vbr -cq <value>`` (constant
        quality within a VBR envelope, ``-b:v 0`` letting ``-cq`` alone drive
        the bitrate) plus a ``-preset``, using the same p1(fastest)-p7(best)
        naming as ffmpeg's own NVENC preset scale. NVENC's ``-cq`` uses the
        same 0-51 lower-is-better scale as x264's ``-crf``, so the existing
        CRF preset numbers (18/23/28) carry over directly as sensible ``-cq``
        values rather than needing a separate mapping table.

        ``color`` adds the pixel format/profile of a 10-bit export (and the
        HDR10 static metadata x265 can write) -- see :func:`_pixel_args`.
        """
        pixel = Exporter._pixel_args(codec, color, hdr_metadata)
        if is_gpu:
            return NVENC_TUNING + ["-rc:v", "vbr", "-cq:v", crf, "-b:v", "0"] + pixel
        if codec == "libx264":
            return ["-crf", crf, "-preset", "medium"] + pixel
        if codec == "libx265":
            # x265's CRF scale sits about 4-6 above x264's for the same quality.
            return ["-crf", str(int(crf) + 4), "-preset", "medium"] + pixel
        if codec == "libaom-av1":
            # libaom's 0-63 scale: x264 18/23/28 -> about 25/32/39.
            return ["-crf", str(round(int(crf) * 1.4)), "-b:v", "0", "-cpu-used", "6", "-row-mt", "1"] + pixel
        if codec == "libvpx-vp9":
            return ["-crf", crf, "-b:v", "0"]
        if codec in ("prores_ks", "dnxhd"):
            return pixel
        return []

    @staticmethod
    def _pixel_args(codec: str, color: OutputColor = SDR_8BIT, hdr_metadata: Tuple[str, str] = ("", "")) -> List[str]:
        """Pixel format, profile and encoder-level colour flags for ``codec`` at the export's bit depth.

        x265 also gets the HDR description in its own parameters (and the
        source's mastering display / MaxCLL when probed): it is the encoder
        here that can write HDR10 static metadata. NVENC and AV1 exports
        carry the colour tags only (the filter graph doesn't pass frame side
        data through to them).
        """
        ten = color.is_10bit
        if codec in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
            out = ["-pix_fmt", "p010le" if ten else "yuv420p"]
            return out + (["-profile:v", "main10"] if ten and codec == "hevc_nvenc" else [])
        if codec == "libx264":
            return ["-pix_fmt", "yuv420p"]
        if codec == "libx265":
            params = ["log-level=error"]
            if ten:
                params += [f"colorprim={color.primaries}", f"transfer={color.transfer}", f"colormatrix={color.matrix}"]
            if color.is_hdr:
                params += ["repeat-headers=1"]
                master, cll = hdr_metadata
                if color.hdr == "pq":
                    params += ["hdr10=1"] + ([f"master-display={master}"] if master else []) \
                        + ([f"max-cll={cll}"] if cll else [])
            out = ["-pix_fmt", "yuv420p10le" if ten else "yuv420p"]
            out += ["-profile:v", "main10"] if ten else []
            return out + ["-tag:v", "hvc1", "-x265-params", ":".join(params)]
        if codec == "libaom-av1":
            return ["-pix_fmt", "yuv420p10le" if ten else "yuv420p"]
        if codec == "prores_ks":
            return ["-profile:v", "3", "-vendor", "apl0", "-pix_fmt", "yuv422p10le"]
        if codec == "dnxhd":
            # DNxHR HQ is 8-bit; its 10-bit sibling is HQX.
            return ["-profile:v", "dnxhr_hqx", "-pix_fmt", "yuv422p10le"] if ten else \
                ["-profile:v", "dnxhr_hq", "-pix_fmt", "yuv422p"]
        return []

    def _output_args(
        self,
        settings: ExportSettings,
        graph: GraphResult,
        ext: str,
        codec: str,
        audio_codec: str,
        video_args: List[str],
        chapters_index: Optional[int],
    ) -> List[str]:
        """Everything after the inputs/graph for a final (audio-carrying) encode.

        Shared by single-pass, two-pass pass 2 and GPU target size, which used
        to carry diverging copies of it.
        """
        args = ["-map", f"[{graph.video_out_label}]"]
        if graph.has_audio:
            args += ["-map", f"[{graph.audio_out_label}]"]
        args += ["-c:v", codec] + video_args + self._color.output_tag_args()
        if graph.has_audio:
            args += ["-c:a", audio_codec]
            if not audio_codec.startswith("pcm_"):
                args += ["-b:a", f"{settings.audio_bitrate_kbps}k"]
        if chapters_index is not None:
            args += ["-map_metadata", str(chapters_index)]
        args += ["-r", fps_expr(settings.fps)]
        args += ["-movflags", "+faststart"] if ext in (".mp4", ".mov") else []
        args += ["-progress", "pipe:1", "-nostats", settings.output_path]
        return args

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
            return self._common_args(graph, chapters_args) + self._output_args(
                settings, graph, ext, codec, audio_codec,
                self._video_quality_args(codec, is_gpu, crf, self._color, self._hdr_metadata), chapters_index
            )

        effective_codec, using_gpu = self._resolve_video_codec(video_codec, settings.use_gpu, self._color.is_10bit)
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
        args += ["-map", "[gifout]", "-loop", "0", "-r", fps_expr(settings.fps)]
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
                *self._pixel_args(video_codec, self._color),
                "-an",
                "-r", fps_expr(settings.fps),
                "-f", "null",
                "-progress", "pipe:1", "-nostats",
                null_output,
            ]
            self._run_ffmpeg(
                pass1_args, total_duration, on_progress, progress_range=(0.0, 0.5), label="Analyzing (pass 1/2)"
            )

            # ---- Pass 2: real encode at the bitrate computed for the target size ----
            video_args = ["-b:v", f"{video_kbps}k", "-pass", "2", "-passlogfile", passlog_prefix]
            if video_codec == "libx264":
                video_args += ["-preset", "medium"]
            elif video_codec == "libaom-av1":
                video_args += ["-cpu-used", "6", "-row-mt", "1"]
            video_args += self._pixel_args(video_codec, self._color, self._hdr_metadata)
            pass2_args = self._common_args(graph, chapters_args) + self._output_args(
                settings, graph, ext, video_codec, audio_codec, video_args, chapters_index
            )

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

    def _export_gpu_target_size(
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
    ) -> Optional[str]:
        """Target size in one NVENC pass (VBR with a rate cap); None means "use CPU two-pass instead".

        NVENC has no ``-pass 1/2`` log files, but a capped VBR with
        multipass lands close to the target in a single run. Falls back
        (returns None) when NVENC isn't available for this codec, or on a
        GPU-specific failure, so the exact-size CPU two-pass still runs.
        """
        codec, is_gpu = self._resolve_video_codec(video_codec, True, self._color.is_10bit)
        if not is_gpu:
            return None
        video_args = [a for a in NVENC_TUNING] + [
            "-rc:v", "vbr", "-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.5)}k",
            "-bufsize", f"{video_kbps * 2}k",
        ] + self._pixel_args(codec, self._color)
        args = self._common_args(graph, chapters_args) + self._output_args(
            settings, graph, ext, codec, audio_codec, video_args, chapters_index
        )
        try:
            self._run_ffmpeg(args, total_duration, on_progress, progress_range=(0.0, 1.0), label="Rendering (GPU, target size)")
        except ExportError as exc:
            if self._cancelled or not self._is_gpu_specific_failure(exc):
                raise
            if on_progress:
                on_progress(0.0, "GPU encoding failed -- falling back to CPU two-pass...")
            return None
        return settings.output_path

    def _run_ffmpeg(
        self,
        args: List[str],
        total_duration: float,
        on_progress: Optional[ProgressCallback],
        progress_range: tuple,
        label: str,
    ) -> List[str]:
        """Run one FFmpeg pass; returns its last log lines (progress lines excluded).

        With GPU decoding enabled (``VIDEO_EDITOR_HWACCEL``, see
        :func:`core.ffmpeg_utils.decode_hwaccel_modes`) the pass is tried with
        each decode mode in turn, ending in plain software decoding. A failed
        attempt is retried with the next mode only when it failed before
        producing output or its log blames the hardware decoder -- a media,
        disk or *encoder* problem fails straight away (the NVENC -> CPU
        encoder fallback is the caller's job, as before). Every attempt
        overwrites the same output (``-y``) and progress restarts at the
        range's start, so nothing is counted twice; the exporter only renames
        the ``.partial`` file into place after a successful attempt.
        """
        modes = decode_hwaccel_modes()
        run_args, graph_file = externalize_filter_graph(args)
        try:
            for attempt, mode in enumerate(modes):
                if self._cancelled:
                    raise ExportCancelled()
                if attempt and on_progress:
                    on_progress(progress_range[0], f"{label}: GPU decoding failed -- retrying with "
                                                   f"{mode or 'software'} decoding...")
                returncode, log_tail, encoded_seconds = self._run_ffmpeg_once(
                    apply_decode_hwaccel(run_args, mode), total_duration, on_progress, progress_range, label
                )
                if returncode == 0:
                    return log_tail
                error = ExportError(
                    f"FFmpeg exited with code {returncode} ({label}).",
                    log_tail=log_tail,
                    encoded_seconds=encoded_seconds,
                )
                retry = mode is not None and (encoded_seconds <= 0.0 or is_hw_decode_failure(log_tail))
                if not retry or attempt == len(modes) - 1:
                    raise error
        finally:
            remove_quietly(graph_file)
        return []  # unreachable: the last mode always returns or raises

    def _run_ffmpeg_once(
        self,
        args: List[str],
        total_duration: float,
        on_progress: Optional[ProgressCallback],
        progress_range: tuple,
        label: str,
    ) -> Tuple[int, List[str], float]:
        """One FFmpeg process: ``(returncode, log tail, encoded seconds)``. Raises ExportCancelled."""
        process = popen(args)
        self._process = process
        log_tail: deque = deque(maxlen=ERROR_LOG_TAIL_LINES)
        encoded_seconds = 0.0
        speed = 0.0
        try:
            if self._cancelled:  # cancel() ran before _process was set
                process.terminate()
                raise ExportCancelled()
            assert process.stdout is not None
            for line in process.stdout:
                if self._cancelled:
                    process.terminate()
                    raise ExportCancelled()
                match = _TIME_RE.search(line)
                speed_match = _SPEED_RE.match(line)
                if speed_match:
                    try:
                        speed = float(speed_match.group(1))
                    except ValueError:
                        pass
                elif match:
                    out_time_s = int(match.group(1)) / 1_000_000
                    encoded_seconds = max(encoded_seconds, out_time_s)
                    if on_progress:
                        on_progress(*progress_message(label, out_time_s, total_duration, speed, progress_range))
                elif not _PROGRESS_LINE_RE.match(line):
                    stripped = line.rstrip()
                    if stripped:
                        log_tail.append(stripped)
            process.wait()
        finally:
            if process.stdout:
                process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
            self._process = None

        if self._cancelled:
            raise ExportCancelled()
        return process.returncode, list(log_tail), encoded_seconds


def progress_message(
    label: str, out_time_s: float, total_duration: float, speed: float, progress_range: tuple
) -> Tuple[float, str]:
    """``(overall fraction, status text)`` for one progress report, with speed and time left once known."""
    range_start, range_end = progress_range
    local_fraction = min(1.0, out_time_s / total_duration) if total_duration > 0 else 0.0
    fraction = range_start + local_fraction * (range_end - range_start)
    message = f"{label}... {local_fraction * 100:.0f}%"
    if speed > 0:
        remaining = max(0.0, total_duration - out_time_s) / speed
        message += f" ({speed:.1f}x realtime, ~{format_eta(remaining)} left)"
    return fraction, message


def project_settings_duration(project: Project, trim_range: Optional[Tuple[float, float]]) -> float:
    """Length of what an export renders: the trim window, or the whole project."""
    if trim_range is not None:
        return trim_range[1] - trim_range[0]
    return project.total_duration
