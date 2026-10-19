"""Per-clip audio effects (F26): noise gate, three-band parametric EQ and compressor.

Each effect is off by default, so an unedited clip never emits its filter (the same neutral-default
convention as noise reduction). The values are plain Clip fields listed in ``FIELDS`` -- name,
default and the range a loaded project is clamped to -- and ``audio_effect_filters()`` turns them
into FFmpeg filters, in signal-chain order:

1. ``agate`` -- the noise gate: attenuates the clip by ``gate_range_db`` while it is quieter than
   ``gate_threshold_db`` (room tone between words, hum between notes);
2. the EQ -- ``lowshelf`` / ``equalizer`` (a peaking band with its own Q) / ``highshelf``; a band
   at 0 dB gain is left out;
3. ``acompressor`` -- evens out loud and quiet passages above ``comp_threshold_db`` by
   ``comp_ratio``, with attack/release in milliseconds and make-up gain.

The EQ uses the biquad shelf/peaking filters rather than ``anequalizer``: they are
channel-agnostic (anequalizer's bands are written per channel, c0/c1...) and support timeline
commands, at the same quality for three bands. They run after noise reduction and before
fades/volume, on the clip's own level -- both in the export graph and the preview.
"""
from __future__ import annotations

import math

# name -> (default, minimum, maximum)
FIELDS = {
    "gate_enabled": (False, None, None),
    "gate_threshold_db": (-45.0, -80.0, 0.0),
    "gate_range_db": (24.0, 0.0, 60.0),
    "eq_enabled": (False, None, None),
    "eq_low_gain_db": (0.0, -24.0, 24.0),
    "eq_low_freq": (120.0, 20.0, 1000.0),
    "eq_mid_gain_db": (0.0, -24.0, 24.0),
    "eq_mid_freq": (1000.0, 100.0, 10000.0),
    "eq_mid_q": (1.0, 0.1, 10.0),
    "eq_high_gain_db": (0.0, -24.0, 24.0),
    "eq_high_freq": (8000.0, 1000.0, 20000.0),
    "comp_enabled": (False, None, None),
    "comp_threshold_db": (-18.0, -60.0, 0.0),
    "comp_ratio": (4.0, 1.0, 20.0),
    "comp_attack_ms": (20.0, 0.1, 2000.0),
    "comp_release_ms": (250.0, 1.0, 9000.0),
    "comp_makeup_db": (0.0, 0.0, 24.0),
}
TOGGLES = ("gate_enabled", "eq_enabled", "comp_enabled")


def clean_value(name: str, value):
    """A loaded value made safe: booleans stay booleans, numbers are clamped, junk is the default."""
    default, low, high = FIELDS[name]
    if low is None:
        return value if isinstance(value, bool) else default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return float(min(max(value, low), high))


def from_dict(data: dict) -> dict:
    """The Clip keyword arguments for a project file's clip dict (absent keys: off / defaults)."""
    return {name: clean_value(name, data.get(name, FIELDS[name][0])) for name in FIELDS}


def to_dict(clip) -> dict:
    return {name: getattr(clip, name) for name in FIELDS}


def has_audio_effects(clip) -> bool:
    return bool(audio_effect_filters(clip))


def _linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


def audio_effect_filters(clip) -> str:
    """``,filter,filter...`` for the clip's enabled audio effects (empty when none changes the sound)."""
    v = {name: clean_value(name, getattr(clip, name, FIELDS[name][0])) for name in FIELDS}
    filters = []
    if v["gate_enabled"] and v["gate_range_db"] > 0:
        filters.append(
            f"agate=threshold={_linear(v['gate_threshold_db']):.6f}:range={_linear(-v['gate_range_db']):.6f}"
            ":ratio=9000:attack=10:release=150"
        )
    if v["eq_enabled"]:
        if abs(v["eq_low_gain_db"]) > 1e-3:
            filters.append(f"lowshelf=f={v['eq_low_freq']:.1f}:g={v['eq_low_gain_db']:.2f}")
        if abs(v["eq_mid_gain_db"]) > 1e-3:
            filters.append(f"equalizer=f={v['eq_mid_freq']:.1f}:t=q:w={v['eq_mid_q']:.3f}:g={v['eq_mid_gain_db']:.2f}")
        if abs(v["eq_high_gain_db"]) > 1e-3:
            filters.append(f"highshelf=f={v['eq_high_freq']:.1f}:g={v['eq_high_gain_db']:.2f}")
    if v["comp_enabled"] and (v["comp_ratio"] > 1.0 or v["comp_makeup_db"] > 0):
        filters.append(
            f"acompressor=threshold={_linear(v['comp_threshold_db']):.6f}:ratio={v['comp_ratio']:.2f}"
            f":attack={v['comp_attack_ms']:.2f}:release={v['comp_release_ms']:.2f}:makeup={_linear(v['comp_makeup_db']):.4f}"
        )
    return "".join("," + f for f in filters)
