"""Masks for effects (F16): limit any effect to a rectangle, an ellipse or one side of a gradient.

A mask is a small dict on the effect (``EffectInstance.mask``)::

    {"shape": "ellipse", "cx": 0.5, "cy": 0.5, "w": 0.4, "h": 0.4, "angle": 0, "feather": 0.1, "invert": False}

Coordinates are fractions of the frame the effect runs on (centre, width, height), ``angle`` rotates
the shape (degrees; for "linear" it is the gradient's direction, the effect applying on the side the
arrow points to past the centre line), ``feather`` softens the edge (fraction of the frame's shorter
side), ``invert`` swaps inside and outside.

Rendering: the mask is drawn once (pure Python, no NumPy dependency) at a small size with the
frame's aspect, cached as a PGM file, and in the filter graph scaled to the frame with
``scale2ref``, split into a full four-plane ``yuva420p`` mask (``mergeplanes``: every plane, alpha
included, gets the mask -- a grey image converted to YUV would have neutral chroma and half-blend
the colour), and the effect's output merged over the unaffected frame with ``maskedmerge``. Masks
are static (not keyframed).
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import tempfile
from typing import Any, Dict, Optional

MASK_SHAPES = ("none", "rectangle", "ellipse", "linear")
MASK_LONG_EDGE = 384
DEFAULT_MASK: Dict[str, Any] = {"shape": "none", "cx": 0.5, "cy": 0.5, "w": 0.5, "h": 0.5, "angle": 0.0,
                                "feather": 0.05, "invert": False}
_LIMITS = {"cx": (-0.5, 1.5), "cy": (-0.5, 1.5), "w": (0.0, 2.0), "h": (0.0, 2.0), "angle": (-360.0, 360.0),
           "feather": (0.0, 0.5)}
_labels = itertools.count(1)


def clean_mask(mask: Any) -> Optional[Dict[str, Any]]:
    """A valid mask dict (defaults filled in, values clamped), or None for no mask."""
    if not isinstance(mask, dict) or mask.get("shape") not in MASK_SHAPES[1:]:
        return None
    clean = dict(DEFAULT_MASK)
    clean["shape"] = mask["shape"]
    for key, (low, high) in _LIMITS.items():
        try:
            clean[key] = min(max(float(mask.get(key, DEFAULT_MASK[key])), low), high)
        except (TypeError, ValueError):
            pass
    clean["invert"] = bool(mask.get("invert", False))
    return clean


def mask_value(mask: Dict[str, Any], nx: float, ny: float, aspect: float) -> float:
    """Coverage 0..1 at normalized frame position (nx, ny) on a frame of width/height ``aspect``."""
    shape, feather = mask["shape"], max(mask["feather"], 1e-6)
    # Work in units of the frame's shorter side, so feather and rotation aren't stretched.
    sx, sy = (aspect, 1.0) if aspect >= 1 else (1.0, 1.0 / aspect)
    dx, dy = (nx - mask["cx"]) * sx, (ny - mask["cy"]) * sy
    angle = math.radians(mask["angle"])
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    if shape == "linear":
        # Signed distance past the centre line, along the gradient's direction.
        distance = dx * cos_a + dy * sin_a
        value = 0.5 + distance / feather
    else:
        u = dx * cos_a + dy * sin_a
        v = -dx * sin_a + dy * cos_a
        half_w, half_h = max(mask["w"] * sx / 2, 1e-6), max(mask["h"] * sy / 2, 1e-6)
        if shape == "rectangle":
            outside = max(abs(u) - half_w, abs(v) - half_h)
        else:
            # Distance to the ellipse edge, approximated through its normalized radius.
            r = math.hypot(u / half_w, v / half_h)
            outside = (r - 1.0) * min(half_w, half_h)
        value = 0.5 - outside / feather
    value = min(max(value, 0.0), 1.0)
    return 1.0 - value if mask["invert"] else value


def render_mask(mask: Dict[str, Any], aspect: float, long_edge: int = MASK_LONG_EDGE) -> tuple:
    """(width, height, bytes) of an 8-bit grey mask with the frame's aspect."""
    aspect = max(aspect, 1e-3)
    width, height = (long_edge, max(2, round(long_edge / aspect))) if aspect >= 1 else (max(2, round(long_edge * aspect)), long_edge)
    data = bytearray(width * height)
    for y in range(height):
        ny = (y + 0.5) / height
        row = y * width
        for x in range(width):
            data[row + x] = int(mask_value(mask, (x + 0.5) / width, ny, aspect) * 255 + 0.5)
    return width, height, bytes(data)


def mask_file(mask: Dict[str, Any], aspect: float) -> str:
    """The cached PGM of ``mask`` at ``aspect`` (drawn on first use)."""
    key = hashlib.sha1(json.dumps([mask, round(aspect, 4), MASK_LONG_EDGE], sort_keys=True).encode()).hexdigest()[:16]
    folder = os.path.join(tempfile.gettempdir(), "video_editor_masks")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{key}.pgm")
    if not os.path.isfile(path):
        width, height, data = render_mask(mask, aspect)
        partial = f"{path}.{os.getpid()}.partial"
        with open(partial, "wb") as handle:
            handle.write(f"P5 {width} {height} 255\n".encode("ascii"))
            handle.write(data)
        os.replace(partial, path)
    return path


def masked_filter(effect_filter: str, mask: Dict[str, Any], aspect: float) -> str:
    """Comma-prefixed filter text applying ``effect_filter`` only where ``mask`` covers the frame.
    Inline labelled branches inside a chain: valid in -filter_complex and -vf alike."""
    from core.effects import _escape

    n = next(_labels)
    path = _escape(mask_file(mask, aspect))
    return (
        f",format=yuva420p,split=2[mb{n}][me{n}];"
        f"[me{n}]{effect_filter},format=yuva420p[mf{n}];"
        # Escaped like the LUT effect's file (core.effects._escape) and, like it, unquoted.
        f"movie=filename={path},format=gray[m0{n}];[m0{n}][mb{n}]scale2ref[m1{n}][mb1{n}];"
        f"[m1{n}]split=2[my0{n}][mh{n}];[mh{n}]scale=iw/2:ih/2,split=2[mu{n}][mv{n}];"
        f"[my0{n}]split=2[my{n}][ma{n}];[my{n}][mu{n}][mv{n}][ma{n}]mergeplanes=0x00102030:yuva420p[mk{n}];"
        f"[mb1{n}][mf{n}][mk{n}]maskedmerge"
    )
