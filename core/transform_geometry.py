"""Where a clip's Transform puts it on the canvas, and how a drag changes it (F15
"on-canvas transform"). Qt-free, so the preview overlay's math is unit-tested.

The export (core/filter_graph.py) scales a video/image clip to fit the canvas
keeping its aspect ratio, multiplies that by ``transform_scale``, rotates it by
``transform_rotation`` and centres it at (``transform_x``, ``transform_y``) of
the canvas. These helpers work in canvas pixels.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Tuple

# The Properties panel's Scale slider range (10 % .. 300 %).
MIN_SCALE = 0.1
MAX_SCALE = 3.0


@dataclass(frozen=True)
class ClipBox:
    cx: float  # centre, canvas pixels
    cy: float
    width: float  # unrotated size, canvas pixels
    height: float
    rotation: float  # degrees, clockwise

    def corners(self) -> List[Tuple[float, float]]:
        """The four corners (top-left, top-right, bottom-right, bottom-left), rotated."""
        angle = math.radians(self.rotation)
        cos, sin = math.cos(angle), math.sin(angle)
        result = []
        for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            x, y = dx * self.width / 2, dy * self.height / 2
            result.append((self.cx + x * cos - y * sin, self.cy + x * sin + y * cos))
        return result

    def contains(self, x: float, y: float) -> bool:
        """Whether canvas point (x, y) is inside the (rotated) box."""
        angle = -math.radians(self.rotation)
        dx, dy = x - self.cx, y - self.cy
        local_x = dx * math.cos(angle) - dy * math.sin(angle)
        local_y = dx * math.sin(angle) + dy * math.cos(angle)
        return abs(local_x) <= self.width / 2 and abs(local_y) <= self.height / 2


def clip_box(transform_x: float, transform_y: float, transform_scale: float, transform_rotation: float,
             source_size: Tuple[int, int], canvas_size: Tuple[int, int]) -> ClipBox:
    """The clip's box on a ``canvas_size`` canvas for a ``source_size`` source."""
    src_w, src_h = max(1, source_size[0]), max(1, source_size[1])
    canvas_w, canvas_h = max(1, canvas_size[0]), max(1, canvas_size[1])
    fit = min(canvas_w / src_w, canvas_h / src_h)
    scale = max(0.0, transform_scale)
    return ClipBox(transform_x * canvas_w, transform_y * canvas_h, src_w * fit * scale, src_h * fit * scale, transform_rotation)


def moved(transform_x: float, transform_y: float, dx: float, dy: float, canvas_size: Tuple[int, int]) -> Tuple[float, float]:
    """The position after dragging the clip by (dx, dy) canvas pixels, kept on the canvas (0..1)."""
    canvas_w, canvas_h = max(1, canvas_size[0]), max(1, canvas_size[1])
    return (min(1.0, max(0.0, transform_x + dx / canvas_w)), min(1.0, max(0.0, transform_y + dy / canvas_h)))


def scaled(box: ClipBox, transform_scale: float, start: Tuple[float, float], end: Tuple[float, float]) -> float:
    """The scale after dragging a corner from ``start`` to ``end`` (canvas pixels):
    uniform, by how much farther from the centre the pointer now is."""
    before = math.hypot(start[0] - box.cx, start[1] - box.cy)
    after = math.hypot(end[0] - box.cx, end[1] - box.cy)
    if before <= 1e-6:
        return transform_scale
    return min(MAX_SCALE, max(MIN_SCALE, transform_scale * after / before))
