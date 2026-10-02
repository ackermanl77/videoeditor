"""Two-pass vid.stab stabilization: the motion-analysis pass and its cache.

The ``stabilize`` effect (core/effects.py) renders with ``vidstabtransform``,
which needs the camera motion that ``vidstabdetect`` measured on *exactly
the same frames*. The export graph (core/filter_graph.py) therefore applies
the transform on the decoded source frames, before speed changes and
scaling, and records one :class:`StabilizeJob` per stabilized clip (or per
speed-ramp segment): the source range it decodes plus the analysis
settings. :meth:`core.exporter.Exporter.export` runs the jobs whose result
file doesn't exist yet (``vidstabdetect`` on the same ``-ss``/``-t``/``trim``
input) before rendering.

Results are cached under ``~/.video_editor/stabilize`` (or
``$VIDEO_EDITOR_STABILIZE_DIR``), keyed by the source file (path, size,
modification time), the range and the detection settings -- so the
analysis runs once per clip, and changing only the smoothing/zoom
(transform-pass settings) re-renders without re-analysing.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import List, Optional

ANALYSIS_DIR_ENV = "VIDEO_EDITOR_STABILIZE_DIR"


def analysis_dir() -> str:
    path = os.environ.get(ANALYSIS_DIR_ENV) or os.path.join(os.path.expanduser("~"), ".video_editor", "stabilize")
    os.makedirs(path, exist_ok=True)
    return path


@dataclass(frozen=True)
class StabilizeJob:
    """One motion analysis: ``length`` seconds of ``source_path`` from ``start`` (input seek point)."""

    source_path: str
    start: float
    length: float
    shakiness: int
    accuracy: int
    result_path: str

    @property
    def is_done(self) -> bool:
        return os.path.isfile(self.result_path)


def _source_signature(path: str) -> str:
    try:
        stat = os.stat(path)
        return f"{os.path.normcase(os.path.abspath(path))}|{stat.st_size}|{int(stat.st_mtime)}"
    except OSError:
        return os.path.normcase(os.path.abspath(path))


def make_job(source_path: str, start: float, length: float, shakiness: int, accuracy: int,
             directory: Optional[str] = None) -> StabilizeJob:
    """The job (and its deterministic cache path) for one source range and detection setting.

    ``start``/``length`` are rounded exactly as the export graph writes them
    (``-ss`` with 3 decimals, ``trim=duration`` with 3), so the key matches
    the frames the transform pass will see.
    """
    start, length = round(max(0.0, start), 3), round(length, 3)
    key = f"{_source_signature(source_path)}|{start:.3f}|{length:.3f}|{shakiness}|{accuracy}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    folder = directory or analysis_dir()
    return StabilizeJob(source_path, start, length, int(shakiness), int(accuracy),
                        os.path.join(folder, f"{digest}.trf"))


#: Extra seconds of source each analysis input may decode (matches the export's INPUT_DURATION_MARGIN).
INPUT_MARGIN = 0.5


def detect_args(ffmpeg: str, job: StabilizeJob, result_path: str) -> List[str]:
    """``vidstabdetect`` over the job's range: same seek, duration bound and trim as the export input."""
    from core.filter_graph import _escape_filter_value

    result = _escape_filter_value(result_path.replace("\\", "/"))
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-y",
        "-ss", f"{job.start:.3f}", "-t", f"{job.length + INPUT_MARGIN:.3f}", "-i", job.source_path,
        "-filter_complex",
        f"[0:v]trim=duration={job.length:.3f},setpts=PTS-STARTPTS,"
        f"vidstabdetect=shakiness={job.shakiness}:accuracy={job.accuracy}:result={result}[vout]",
        "-map", "[vout]", "-an", "-f", "null", "-progress", "pipe:1", "-nostats", os.devnull,
    ]


def pending_jobs(jobs: List[StabilizeJob]) -> List[StabilizeJob]:
    """Unique jobs whose analysis file doesn't exist yet."""
    seen, out = set(), []
    for job in jobs:
        if job.result_path not in seen and not job.is_done:
            seen.add(job.result_path)
            out.append(job)
    return out


def partial_path(job: StabilizeJob) -> str:
    """Where a running analysis writes (renamed over ``result_path`` only once it completes)."""
    return job.result_path + ".partial"
