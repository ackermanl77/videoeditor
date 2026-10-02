"""Generic per-clip video effect stack: a registry of effect kinds plus instances.

Each registry entry declares its parameters (type, range, default) and how to
build its FFmpeg filter, so adding an effect is one entry here: the export
graph (``core/filter_graph.py``) emits ``Clip.effects`` in order, the
Properties panel renders a form from the parameter schema, and the timeline's
"FX" badge reads the same list.

Effects whose FFmpeg filter has no alpha-capable pixel format (``hqdn3d``,
``vignette``) are ``stage="pre"``: they run right after the clip is scaled,
before it gains its alpha channel, so they never strip the transparency a
chroma key or fade adds later. ``stage="post"`` effects run after keying and
colour correction, on the ``yuva420p`` frame.

The older hard-coded fields (``Clip.brightness``..., ``chroma_key_*``) stay as
they are -- they are not migrated into this list -- so existing projects
render unchanged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ParamSpec:
    """One effect parameter: ``kind`` is float, int, bool, color, file or choice."""

    name: str
    label: str
    kind: str
    default: Any
    minimum: float = 0.0
    maximum: float = 1.0
    step: float = 0.1
    choices: Tuple[str, ...] = ()


@dataclass(frozen=True)
class EffectKind:
    kind: str
    label: str
    params: Tuple[ParamSpec, ...]
    build: Callable[[Dict[str, Any]], str]
    stage: str = "post"  # "pre" (before alpha exists) or "post"
    description: str = ""


def _escape(value: str) -> str:
    from core.filter_graph import _escape_filter_value

    return _escape_filter_value(value.replace("\\", "/"))


def _hex(color: str, fallback: str) -> str:
    value = (color or "").lstrip("#")
    return value if len(value) == 6 else fallback


def _build_lut(p: Dict[str, Any]) -> str:
    # lut3d negotiates an RGB(A) format; convert straight back so every later
    # filter (geq opacity keyframes, overlay) sees the usual yuva420p.
    return f"lut3d=file={_escape(str(p['file']))}:interp={p['interp']},format=yuva420p"


def _build_chromakey(p: Dict[str, Any]) -> str:
    color = _hex(p["color"], "00FF00")
    out = f"chromakey=0x{color}:{float(p['similarity']):.3f}:{float(p['blend']):.3f}"
    if p.get("despill"):
        r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
        out += f",despill=type={'blue' if b > g else 'green'},format=yuva420p"
    return out


def _build_sharpen(p: Dict[str, Any]) -> str:
    return f"unsharp=5:5:{float(p['amount']):.3f}:5:5:0"


def _build_blur(p: Dict[str, Any]) -> str:
    return f"gblur=sigma={float(p['sigma']):.3f}"


def _build_vignette(p: Dict[str, Any]) -> str:
    return f"vignette=angle=PI*{float(p['angle']):.3f}/180"


def _build_denoise(p: Dict[str, Any]) -> str:
    s = float(p["strength"])
    return f"hqdn3d={s:.3f}:{s * 0.75:.3f}:{s * 1.5:.3f}:{s * 1.125:.3f}"


REGISTRY: Dict[str, EffectKind] = {
    e.kind: e
    for e in (
        EffectKind(
            "lut3d", "LUT (.cube)",
            (
                ParamSpec("file", "LUT file", "file", ""),
                ParamSpec("interp", "Interpolation", "choice", "tetrahedral",
                          choices=("tetrahedral", "trilinear", "nearest")),
            ),
            _build_lut,
            description="Apply a 3D colour lookup table: camera log to Rec.709, or a creative look.",
        ),
        EffectKind(
            "chromakey", "Chroma Key (YUV) + Despill",
            (
                ParamSpec("color", "Key color", "color", "#00FF00"),
                ParamSpec("similarity", "Similarity", "float", 0.15, 0.01, 1.0, 0.01),
                ParamSpec("blend", "Softness", "float", 0.05, 0.0, 1.0, 0.01),
                ParamSpec("despill", "Remove spill", "bool", True),
            ),
            _build_chromakey,
            description="Keys in YUV (cleaner edges than the RGB Chroma Key group) and removes colour spill.",
        ),
        EffectKind(
            "sharpen", "Sharpen",
            (ParamSpec("amount", "Amount", "float", 0.8, -2.0, 5.0, 0.1),),
            _build_sharpen,
            description="Unsharp mask (negative values soften).",
        ),
        EffectKind(
            "blur", "Gaussian Blur",
            (ParamSpec("sigma", "Radius", "float", 4.0, 0.1, 50.0, 0.5),),
            _build_blur,
        ),
        EffectKind(
            "vignette", "Vignette",
            (ParamSpec("angle", "Strength (degrees)", "float", 36.0, 1.0, 90.0, 1.0),),
            _build_vignette,
            stage="pre",
        ),
        EffectKind(
            "video_denoise", "Video Denoise",
            (ParamSpec("strength", "Strength", "float", 4.0, 0.0, 20.0, 0.5),),
            _build_denoise,
            stage="pre",
            description="hqdn3d spatial/temporal noise reduction.",
        ),
    )
}


def default_params(kind: str) -> Dict[str, Any]:
    spec = REGISTRY.get(kind)
    return {p.name: p.default for p in spec.params} if spec else {}


def _clamped(spec: ParamSpec, value: Any) -> Any:
    if spec.kind in ("float", "int"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = float(spec.default)
        number = max(spec.minimum, min(spec.maximum, number))
        return int(round(number)) if spec.kind == "int" else number
    if spec.kind == "bool":
        return bool(value)
    if spec.kind == "choice":
        return value if value in spec.choices else spec.default
    return "" if value is None else str(value)


@dataclass
class EffectInstance:
    """One effect applied to a clip. ``kind`` names a :data:`REGISTRY` entry."""

    kind: str
    params: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @classmethod
    def create(cls, kind: str, **params: Any) -> "EffectInstance":
        merged = default_params(kind)
        merged.update(params)
        return cls(kind=kind, params=merged)

    @property
    def spec(self) -> Optional[EffectKind]:
        return REGISTRY.get(self.kind)

    def resolved_params(self) -> Dict[str, Any]:
        """Defaults filled in and every value clamped to its schema."""
        spec = self.spec
        if spec is None:
            return dict(self.params)
        return {p.name: _clamped(p, self.params.get(p.name, p.default)) for p in spec.params}

    def is_renderable(self) -> bool:
        """Enabled, known, and (for file parameters) given a file."""
        spec = self.spec
        if not self.enabled or spec is None:
            return False
        params = self.resolved_params()
        return all(params.get(p.name) for p in spec.params if p.kind == "file")

    def filter(self) -> str:
        return self.spec.build(self.resolved_params()) if self.spec else ""

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "params": dict(self.params), "enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EffectInstance":
        # Unknown kinds (from a newer version) are kept so a save doesn't
        # drop them, and simply skipped when rendering.
        return cls(
            kind=str(data.get("kind", "")),
            params=dict(data.get("params") or {}),
            enabled=bool(data.get("enabled", True)),
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
        )


def effect_filters(effects: List[EffectInstance], stage: str) -> str:
    """Comma-prefixed filter text for every renderable effect of ``stage``, in list order."""
    out = ""
    for effect in effects:
        if effect.is_renderable() and effect.spec.stage == stage:
            out += "," + effect.filter()
    return out


def effect_files(effects: List[EffectInstance]) -> List[str]:
    """Every file an enabled effect reads (LUTs), for the export's missing-file check."""
    files = []
    for effect in effects:
        if effect.is_renderable():
            params = effect.resolved_params()
            files += [params[p.name] for p in effect.spec.params if p.kind == "file"]
    return files
