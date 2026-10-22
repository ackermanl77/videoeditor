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
can also be keyframed (below).

Keyframed masks (F16): ``mask["keyframes"]`` is a list of ``[clip-local seconds, cx, cy, w, h, angle,
feather]`` snapshots (2+ animate). Each value becomes a piecewise-linear expression of the stream time
and the mask is drawn per frame with ``geq``, exactly like a tracked mask; with a track too, the track
drives the centre and the keyframes the rest.

Tracked masks (F32): ``mask["track"]`` is a list of ``[clip-local seconds, cx, cy]`` -- the centre
following an object tracked in the clip's own footage (Edit the effect > Track Mask..., which runs
the same FFmpeg find_rect tracking as Track Motion). Such a mask is drawn per frame instead, with
FFmpeg's ``geq`` evaluating the shape around a centre that is a piecewise-linear expression of the
stream time (``tracked_mask_expr``), on a quarter-size copy scaled back up (about 60 fps at 1080p
instead of 5); everything else (feather, angle, invert, the merge) is the same.
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
#: The animatable mask values, in the order a keyframe stores them after its time.
KEYFRAME_PARAMS = ("cx", "cy", "w", "h", "angle", "feather")


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
    track = clean_track(mask.get("track"))
    if track:
        clean["track"] = track
    keyframes = clean_mask_keyframes(mask.get("keyframes"))
    if keyframes:
        clean["keyframes"] = keyframes
    return clean


def clean_mask_keyframes(value: Any) -> list:
    """Mask keyframes [[t, cx, cy, w, h, angle, feather], ...], values clamped, sorted, 2+ (else [])."""
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for entry in value:
        try:
            numbers = [float(v) for v in entry]
        except (TypeError, ValueError):
            continue
        if len(numbers) != 1 + len(KEYFRAME_PARAMS) or not all(math.isfinite(v) for v in numbers) or numbers[0] < 0:
            continue
        row = [round(numbers[0], 4)]
        for key, number in zip(KEYFRAME_PARAMS, numbers[1:]):
            low, high = _LIMITS[key]
            row.append(round(min(max(number, low), high), 5))
        out.append(row)
    out.sort(key=lambda k: k[0])
    return out if len(out) >= 2 else []


def is_animated(mask: Dict[str, Any]) -> bool:
    return bool(mask.get("track") or mask.get("keyframes"))


def mask_at(mask: Dict[str, Any], t: float) -> Dict[str, Any]:
    """The mask's values at clip-local time ``t`` (keyframes, then the track's centre) -- the preview's view."""
    from core.clip import interpolate_keyframes

    out = dict(mask)
    if mask.get("keyframes"):
        values = interpolate_keyframes([tuple(k) for k in mask["keyframes"]], t)
        out.update(dict(zip(KEYFRAME_PARAMS, values)))
    if mask.get("track"):
        out["cx"], out["cy"] = interpolate_keyframes([tuple(k) for k in mask["track"]], t)
    return out


def clean_track(value: Any) -> list:
    """A mask track: [[t, cx, cy], ...] sorted by time, 2+ entries, numbers only (else [])."""
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for entry in value:
        try:
            t, cx, cy = (float(v) for v in entry)
        except (TypeError, ValueError):
            continue
        if all(math.isfinite(v) for v in (t, cx, cy)) and t >= 0:
            out.append([round(t, 4), round(min(max(cx, -0.5), 1.5), 5), round(min(max(cy, -0.5), 1.5), 5)])
    out.sort(key=lambda k: k[0])
    return out if len(out) >= 2 else []


def tracked_mask_expr(mask: Dict[str, Any], t_var: str = "T") -> str:
    """``geq`` luma expression (0..255) of `mask` with its centre following ``mask["track"]``.
    The same geometry as mask_value(), in units of the frame's shorter side (from W/H)."""
    from core.filter_graph import _piecewise_linear_expr

    keyframes = mask.get("keyframes") or []

    def value(name: str) -> str:
        if keyframes:
            index = 1 + KEYFRAME_PARAMS.index(name)
            return f"({_piecewise_linear_expr([(k[0], k[index]) for k in keyframes], t_var)})"
        return f"{mask[name]:.6f}"

    track = mask.get("track")
    if track:
        cx = _piecewise_linear_expr([(t, x) for t, x, _y in track], t_var)
        cy = _piecewise_linear_expr([(t, y) for t, _x, y in track], t_var)
    else:
        cx, cy = value("cx"), value("cy")
    sx, sy = "max(W/H,1)", "max(H/W,1)"
    dx, dy = f"((X/W-({cx}))*{sx})", f"((Y/H-({cy}))*{sy})"
    if keyframes:
        cos_a, sin_a = f"cos({value('angle')}*PI/180)", f"sin({value('angle')}*PI/180)"
        feather = f"max({value('feather')},0.000001)"
    else:
        angle = math.radians(mask["angle"])
        cos_a, sin_a = f"{math.cos(angle):.6f}", f"{math.sin(angle):.6f}"
        feather = f"{max(mask['feather'], 1e-6):.6f}"
    if mask["shape"] == "linear":
        value = f"(0.5+({dx}*{cos_a}+{dy}*{sin_a})/{feather})"
    else:
        u, v = f"({dx}*{cos_a}+{dy}*{sin_a})", f"(-{dx}*{sin_a}+{dy}*{cos_a})"
        hw, hh = f"max({value('w')}*{sx}/2,0.000001)", f"max({value('h')}*{sy}/2,0.000001)"
        if mask["shape"] == "rectangle":
            outside = f"max(abs({u})-{hw},abs({v})-{hh})"
        else:
            outside = f"((hypot({u}/{hw},{v}/{hh})-1)*min({hw},{hh}))"
        value = f"(0.5-{outside}/{feather})"
    value = f"clip({value},0,1)"
    if mask["invert"]:
        value = f"(1-{value})"
    return f"255*{value}"


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


def masked_filter(effect_filter: str, mask: Dict[str, Any], aspect: float, t_offset: float = 0.0) -> str:
    """Comma-prefixed filter text applying ``effect_filter`` only where ``mask`` covers the frame.
    Inline labelled branches inside a chain: valid in -filter_complex and -vf alike. ``t_offset``:
    the clip-local time of the stream's first frame (a speed ramp's later segments), for tracked masks."""
    from core.effects import _escape

    n = next(_labels)
    if is_animated(mask):
        t_var = "T" if abs(t_offset) < 1e-9 else f"(T+{t_offset:.6f})"
        # The per-pixel geq runs on a copy a quarter of the size (16x fewer pixels; the mask is soft
        # anyway) and is scaled back onto the frame with scale2ref, so the sizes always match.
        return (
            f",format=yuva420p,split=4[mb{n}][me{n}][mg{n}][mr{n}];"
            f"[me{n}]{effect_filter},format=yuva420p[mf{n}];"
            f"[mg{n}]scale=trunc(iw/8)*2:trunc(ih/8)*2,format=gray,geq=lum='{tracked_mask_expr(mask, t_var)}'[ms{n}];"
            f"[ms{n}][mr{n}]scale2ref=flags=bilinear[m1{n}][mx{n}];[mx{n}]nullsink;"
            f"[m1{n}]split=2[my0{n}][mh{n}];[mh{n}]scale=iw/2:ih/2,split=2[mu{n}][mv{n}];"
            f"[my0{n}]split=2[my{n}][ma{n}];[my{n}][mu{n}][mv{n}][ma{n}]mergeplanes=0x00102030:yuva420p[mk{n}];"
            f"[mb{n}][mf{n}][mk{n}]maskedmerge"
        )
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
