"""Render one exact frame of the timeline with the export engine (WYSIWYG preview stills).

The preview player shows only the top clip through QMediaPlayer; this
renders what the *export* would show at a time -- every track composited,
transitions, text, keyframes, effects -- by building the same filter graph
for a one-frame window (``trim_range=(t, t + 1/fps)``, which the graph
builder turns into seeked inputs for just the clips under the playhead) at
preview resolution and piping one PNG back.

Qt-free; ``ui/preview_player.py`` runs it on a worker thread and drops
results that a newer request has superseded.
"""

from __future__ import annotations

import copy
import subprocess
from typing import List, Optional, Tuple

from core.ffmpeg_utils import find_binary, run_binary
from core.filter_graph import build_filter_graph
from core.project import Project, ProjectSettings

PREVIEW_FRAME_SIZE = (960, 540)
FRAME_RENDER_TIMEOUT_S = 20.0


class FrameRenderError(RuntimeError):
    pass


def preview_size(project: Project, max_width: int = PREVIEW_FRAME_SIZE[0], max_height: int = PREVIEW_FRAME_SIZE[1]) -> Tuple[int, int]:
    """The project's aspect ratio fitted inside ``max_width`` x ``max_height`` (even dimensions)."""
    w, h = max(1, project.settings.width), max(1, project.settings.height)
    scale = min(max_width / w, max_height / h, 1.0)
    return max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)


def build_frame_args(project: Project, t: float, width: int, height: int) -> Tuple[List[str], object]:
    """``(ffmpeg args, graph)`` rendering the frame at ``t`` as one PNG on stdout.

    The caller must ``graph.cleanup()`` (text overlay temp files).
    """
    fps = project.settings.fps if project.settings.fps > 0 else 30.0
    frame = 1.0 / fps
    total = project.total_duration
    t = max(0.0, min(t, max(0.0, total - frame)))
    settings = ProjectSettings(width=width, height=height, fps=fps, sample_rate=project.settings.sample_rate)
    # Stabilization shows only once an export has cached its motion analysis.
    graph = build_filter_graph(project, settings, trim_range=(t, t + frame), video_only=True,
                               stabilize_cached_only=True)
    args = [find_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin"]
    for spec in graph.input_specs:
        args += spec.pre_args + ["-i", spec.path]
    args += [
        "-filter_complex", f"{graph.filter_complex};[{graph.audio_out_label}]anullsink",
        "-map", f"[{graph.video_out_label}]",
        "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "pipe:1",
    ]
    return args, graph


def render_frame(
    project: Project,
    t: float,
    size: Optional[Tuple[int, int]] = None,
    timeout: float = FRAME_RENDER_TIMEOUT_S,
) -> bytes:
    """PNG bytes of the composited frame at timeline time ``t``. Raises :class:`FrameRenderError`.

    Works on a deep copy, so the UI may keep editing while a worker renders.
    """
    snapshot = copy.deepcopy(project)
    width, height = size or preview_size(snapshot)
    try:
        args, graph = build_frame_args(snapshot, t, width, height)
    except FileNotFoundError as exc:
        raise FrameRenderError(str(exc)) from exc
    try:
        result = run_binary(args, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FrameRenderError(f"Rendering the frame took longer than {timeout:.0f} s.") from exc
    finally:
        graph.cleanup()
    if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
        detail = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise FrameRenderError(detail[-1] if detail else "FFmpeg produced no frame.")
    return result.stdout
