"""Text overlays in the export filter graph (R9: split out of core/filter_graph.py, which
re-exports every name): drawtext filters scaled to the output height."""

from __future__ import annotations

import os

from core.clip import Clip
from core.output_color import SDR_8BIT, OutputColor
from core.filter_graph_base import _escape_filter_value, _Window


# drawtext text_align flag per TextStyle.alignment.
_TEXT_ALIGN_FLAGS = {"left": "L", "center": "C", "right": "R"}


# TextStyle sizes (font size, outline width, shadow offset, box padding) are
# "pixels at 1080p": scaled by output height / REFERENCE_TEXT_HEIGHT so a
# title covers the same share of the frame at 720p, 1080p, 4K or vertical.
REFERENCE_TEXT_HEIGHT = 1080


def _scaled_text_px(value: float, height: int, minimum: int = 0) -> int:
    return max(minimum, int(round(value * height / REFERENCE_TEXT_HEIGHT)))


def _drawtext_filter(
    style, text_path: str, clip: Clip, window: _Window, height: int, color: OutputColor = SDR_8BIT
) -> str:
    """One text overlay's ``drawtext`` filter (unbracketed), enabled over the clip's span of output time.

    The box position (``position_x``/``position_y``) keeps its long-standing
    meaning: 0 = flush left/top, 1 = flush right/bottom of the frame.
    ``alignment`` aligns lines *within* the text block (``text_align``), so
    single-line titles in existing projects don't move.
    """
    from core.fonts import resolve_font_file

    x_expr = f"(w-text_w)*{style.position_x:.3f}"
    y_expr = f"(h-text_h)*{style.position_y:.3f}"
    start = clip.timeline_start - window.start
    end = clip.timeline_end - window.start
    parts = [f"drawtext=textfile={_escape_filter_value(text_path.replace(os.sep, '/'))}:expansion=none"]
    font_file = resolve_font_file(style.font_family, style.bold, style.italic)
    if font_file:
        parts.append(f":fontfile={_escape_filter_value(font_file.replace(os.sep, '/'))}")
    parts.append(f":fontcolor={color.text_color(style.color)}:fontsize={_scaled_text_px(style.font_size, height, 1)}")
    parts.append(f":bordercolor={color.text_color(style.outline_color)}:borderw={_scaled_text_px(style.outline_width, height)}")
    parts.append(f":text_align={_TEXT_ALIGN_FLAGS.get(style.alignment, 'C')}")
    if style.shadow_enabled:
        offset = _scaled_text_px(style.shadow_offset, height)
        parts.append(f":shadowcolor={color.text_color(style.shadow_color)}:shadowx={offset}:shadowy={offset}")
    if style.box_enabled:
        opacity = max(0.0, min(1.0, style.box_opacity))
        parts.append(
            f":box=1:boxcolor={color.text_color(style.box_color)}@{opacity:.2f}:boxborderw={_scaled_text_px(style.box_padding, height)}"
        )
    parts.append(f":x={x_expr}:y={y_expr}")
    parts.append(f":enable='between(t,{start:.3f},{end:.3f})'")
    return "".join(parts)
