"""Video scopes (F24): waveform, RGB parade and vectorscope of the frame under the playhead.

The frame is the export engine's own (``core.frame_render.render_frame``: every track composited,
effects and colour correction applied), and the scopes are FFmpeg's ``waveform`` and
``vectorscope`` filters -- the same measurement a colourist gets from ``ffplay -vf waveform``,
with no image library needed in the app. Qt-free; ``ui/scopes_panel.py`` runs it on a worker
thread.

- **Waveform**: luma by column, 0-255 code values bottom to top (graticule lines at 16/128/235).
- **RGB Parade**: one waveform per channel, red / green / blue side by side.
- **Vectorscope**: chroma (Cb/Cr) as hue around the circle and saturation outwards, with the
  75 % colour-bar targets (R, Mg, B, Cy, G, Yl) drawn in.
"""

from __future__ import annotations

import subprocess
from typing import Dict, Optional, Tuple

from core.ffmpeg_utils import find_binary, run_binary

SCOPES: Dict[str, str] = {
    "waveform": "Waveform (luma)",
    "parade": "RGB Parade",
    "vectorscope": "Vectorscope",
}
# Frame size measured: wide enough for a readable trace, small enough to stay interactive.
SCOPE_FRAME_SIZE = (480, 270)
SCOPE_TIMEOUT_S = 20.0
# Each parade panel / the waveform is this many columns wide (the frame is scaled to it first).
_TRACE_WIDTH = 320


class ScopeError(RuntimeError):
    pass


def scope_filter(kind: str) -> str:
    """The FFmpeg filter chain turning one frame into the ``kind`` scope image."""
    if kind == "waveform":
        return (f"scale={_TRACE_WIDTH}:-2,format=yuv444p,"
                "waveform=components=1:intensity=0.3:graticule=green:flags=numbers+dots")
    if kind == "parade":
        # gbrp planes in R, G, B order of the parade's panels (components=7: all three).
        return (f"scale={_TRACE_WIDTH}:-2,format=gbrp,"
                "waveform=display=parade:components=7:intensity=0.3:graticule=green:flags=numbers+dots")
    if kind == "vectorscope":
        return "format=yuv444p,vectorscope=mode=color3:graticule=color:flags=name+white:opacity=0.6"
    raise ValueError(f"Unknown scope {kind!r} (choose from {', '.join(SCOPES)})")


def render_scope(frame_png: bytes, kind: str, timeout: float = SCOPE_TIMEOUT_S) -> bytes:
    """PNG bytes of the ``kind`` scope for one PNG frame. Raises :class:`ScopeError`."""
    args = [
        find_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "png_pipe", "-i", "pipe:0", "-vf", scope_filter(kind),
        "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "pipe:1",
    ]
    try:
        result = run_binary(args, timeout=timeout, input=frame_png)
    except subprocess.TimeoutExpired as exc:
        raise ScopeError(f"The scope took longer than {timeout:.0f} s.") from exc
    except FileNotFoundError as exc:
        raise ScopeError(str(exc)) from exc
    if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
        detail = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise ScopeError(detail[-1] if detail else "FFmpeg produced no scope image.")
    return result.stdout


def timeline_scope(project, t: float, kind: str, size: Optional[Tuple[int, int]] = None) -> bytes:
    """The ``kind`` scope of the composited frame at timeline time ``t``."""
    from core.frame_render import FrameRenderError, preview_size, render_frame

    try:
        frame = render_frame(project, t, size or preview_size(project, *SCOPE_FRAME_SIZE))
    except FrameRenderError as exc:
        raise ScopeError(str(exc)) from exc
    return render_scope(frame, kind)
