"""Generic per-clip video effect stack: a registry of effect kinds plus instances.

Each registry entry declares its parameters (type, range, default, whether
it can be animated) and how to build its FFmpeg filter, so adding an effect
is one entry here: the export graph (``core/filter_graph.py``) emits
``Clip.effects`` in order, the Properties panel renders a form from the
parameter schema, and the timeline's badges read the same list.

Stages, i.e. where in a clip's chain an effect runs:

* ``"source"`` -- on the decoded source frames, before speed changes and
  scaling (stabilization: its motion analysis is made on those same frames).
* ``"pre"`` -- right after the clip is scaled, before it gains its alpha
  channel. For filters with no alpha-capable pixel format (``hqdn3d``,
  ``vignette``), so they never strip the transparency a key or fade adds.
* ``"post"`` -- after scaling, on the ``yuva420p`` frame (keys, colour, looks).

Animatable parameters (``ParamSpec.animatable``) can carry keyframes
(``EffectInstance.keyframes``): 2+ ``(clip-local seconds, value)`` pairs
become a per-frame, piecewise-linear FFmpeg expression -- the same
semantics as the opacity/volume keyframes -- so only parameters the
underlying filter evaluates per frame are marked animatable.

Projects saved before file format 3 kept colour correction and the RGB
chroma key in fixed ``Clip`` fields. Those are now the ``color_correction``
and ``colorkey`` effect kinds; :func:`legacy_effect_dicts` converts the old
fields (``core.project`` migration 2 -> 3, and ``Clip``'s compatibility
attributes) so old projects render exactly as before.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

Number = Union[int, float]


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
    #: The filter evaluates this parameter per frame, so it can be keyframed.
    animatable: bool = False


@dataclass(frozen=True)
class EffectKind:
    kind: str
    label: str
    params: Tuple[ParamSpec, ...]
    build: Callable[[Dict[str, Any]], str]
    stage: str = "post"  # "source", "pre" (before alpha exists) or "post"
    description: str = ""
    #: Optional "does this change the picture at all" test on the resolved
    #: (non-animated) parameters; an inactive effect emits no filter, so a
    #: neutral colour correction renders exactly like none.
    active: Optional[Callable[[Dict[str, Any]], bool]] = None


def _escape(value: str) -> str:
    from core.filter_graph import _escape_filter_value

    return _escape_filter_value(value.replace("\\", "/"))


def _hex(color: str, fallback: str) -> str:
    value = (color or "").lstrip("#")
    return value if len(value) == 6 else fallback


def _num(value: Any, fmt: str = ".3f") -> str:
    """A parameter as filter text: a number, or a quoted per-frame expression (animated)."""
    if isinstance(value, str):
        return f"'{value}'"
    return format(float(value), fmt)


def _is_expr(*values: Any) -> bool:
    return any(isinstance(v, str) for v in values)


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


def _build_colorkey(p: Dict[str, Any]) -> str:
    color = _hex(p["color"], "00FF00")
    return f"colorkey=0x{color}:{float(p['similarity']):.3f}:{float(p['blend']):.3f}"


def _build_color_correction(p: Dict[str, Any]) -> str:
    values = [p["brightness"], p["contrast"], p["saturation"], p["gamma"]]
    out = (
        f"eq=brightness={_num(values[0])}:contrast={_num(values[1])}"
        f":saturation={_num(values[2])}:gamma={_num(values[3])}"
    )
    # eq evaluates its expressions once at init unless told otherwise.
    return out + (":eval=frame" if _is_expr(*values) else "")


def _color_correction_active(p: Dict[str, Any]) -> bool:
    values = (p["brightness"], p["contrast"], p["saturation"], p["gamma"])
    return _is_expr(*values) or tuple(float(v) for v in values) != (0.0, 1.0, 1.0, 1.0)


def _build_sharpen(p: Dict[str, Any]) -> str:
    return f"unsharp=5:5:{float(p['amount']):.3f}:5:5:0"


def _build_blur(p: Dict[str, Any]) -> str:
    return f"gblur=sigma={float(p['sigma']):.3f}"


def _build_vignette(p: Dict[str, Any]) -> str:
    angle = p["angle"]
    if isinstance(angle, str):
        return f"vignette=angle='PI*({angle})/180':eval=frame"
    return f"vignette=angle=PI*{float(angle):.3f}/180"


def _build_denoise(p: Dict[str, Any]) -> str:
    s = float(p["strength"])
    return f"hqdn3d={s:.3f}:{s * 0.75:.3f}:{s * 1.5:.3f}:{s * 1.125:.3f}"


#: Key under which core.filter_graph passes a stabilized clip's motion
#: analysis file (see core/stabilize.py) to the ``stabilize`` builder.
ANALYSIS_FILE_PARAM = "_analysis_file"


def _build_stabilize(p: Dict[str, Any]) -> str:
    """Pass 2 of vid.stab; pass 1 (``vidstabdetect``) is run by core/stabilize.py before the export."""
    return (
        f"vidstabtransform=input={_escape(str(p[ANALYSIS_FILE_PARAM]))}"
        f":smoothing={int(p['smoothing'])}:optzoom={1 if p['auto_zoom'] else 0}"
        f":zoom={float(p['zoom']):.2f}:crop={p['borders']}:interpol=bicubic"
    )


#: Effect kinds that replaced the pre-format-3 hard-coded clip fields; the
#: timeline shows them with their own "C"/"K" badges rather than "FX".
LEGACY_KINDS = ("colorkey", "color_correction")


REGISTRY: Dict[str, EffectKind] = {
    e.kind: e
    for e in (
        EffectKind(
            "color_correction", "Color Correction",
            (
                ParamSpec("brightness", "Brightness", "float", 0.0, -1.0, 1.0, 0.01, animatable=True),
                ParamSpec("contrast", "Contrast", "float", 1.0, 0.0, 2.0, 0.01, animatable=True),
                ParamSpec("saturation", "Saturation", "float", 1.0, 0.0, 3.0, 0.01, animatable=True),
                ParamSpec("gamma", "Gamma", "float", 1.0, 0.1, 3.0, 0.01, animatable=True),
            ),
            _build_color_correction,
            description="Brightness, contrast, saturation and gamma (FFmpeg eq); every value can be keyframed.",
            active=_color_correction_active,
        ),
        EffectKind(
            "colorkey", "Chroma Key (RGB)",
            (
                ParamSpec("color", "Key color", "color", "#00FF00"),
                ParamSpec("similarity", "Similarity", "float", 0.2, 0.0, 1.0, 0.01),
                ParamSpec("blend", "Softness", "float", 0.1, 0.0, 1.0, 0.01),
            ),
            _build_colorkey,
            description="Makes one colour transparent (FFmpeg colorkey, RGB). The YUV key below has cleaner edges.",
        ),
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
            (ParamSpec("angle", "Strength (degrees)", "float", 36.0, 1.0, 90.0, 1.0, animatable=True),),
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
        EffectKind(
            "stabilize", "Stabilize (vid.stab)",
            (
                ParamSpec("shakiness", "Shakiness (1 = little, 10 = very)", "int", 5, 1, 10, 1),
                ParamSpec("accuracy", "Accuracy", "int", 15, 1, 15, 1),
                ParamSpec("smoothing", "Smoothing (frames)", "int", 15, 0, 100, 1),
                ParamSpec("auto_zoom", "Zoom to hide borders", "bool", True),
                ParamSpec("zoom", "Extra zoom (%)", "float", 0.0, -20.0, 20.0, 0.5),
                ParamSpec("borders", "Borders", "choice", "black", choices=("black", "keep")),
            ),
            _build_stabilize,
            stage="source",
            description="Two-pass vid.stab stabilization. The motion analysis runs once per clip at export "
                        "and is cached.",
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


Keyframes = List[Tuple[float, float]]


@dataclass
class EffectInstance:
    """One effect applied to a clip. ``kind`` names a :data:`REGISTRY` entry.

    ``keyframes`` maps an animatable parameter's name to ``(clip-local
    timeline seconds, value)`` pairs; 2+ entries animate it (overriding the
    plain value in ``params``), fewer leave the plain value in charge.
    """

    kind: str
    params: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    keyframes: Dict[str, Keyframes] = field(default_factory=dict)
    #: Limit the effect to an area (core/effect_masks.py, F16); None = the whole frame.
    mask: Optional[Dict[str, Any]] = None

    @classmethod
    def create(cls, kind: str, **params: Any) -> "EffectInstance":
        merged = default_params(kind)
        merged.update(params)
        return cls(kind=kind, params=merged)

    @property
    def spec(self) -> Optional[EffectKind]:
        return REGISTRY.get(self.kind)

    def param_spec(self, name: str) -> Optional[ParamSpec]:
        spec = self.spec
        return next((p for p in spec.params if p.name == name), None) if spec else None

    def resolved_params(self) -> Dict[str, Any]:
        """Defaults filled in and every value clamped to its schema."""
        spec = self.spec
        if spec is None:
            return dict(self.params)
        return {p.name: _clamped(p, self.params.get(p.name, p.default)) for p in spec.params}

    def animated_params(self) -> Dict[str, Keyframes]:
        """Animatable parameters with 2+ keyframes, values clamped to the schema."""
        spec = self.spec
        if spec is None:
            return {}
        out = {}
        for p in spec.params:
            keys = self.keyframes.get(p.name) or []
            if p.animatable and len(keys) >= 2:
                out[p.name] = sorted((float(t), _clamped(p, v)) for t, v in keys)
        return out

    def is_renderable(self) -> bool:
        """Enabled, known, given its files, and actually changing the picture."""
        spec = self.spec
        if not self.enabled or spec is None:
            return False
        params = self.resolved_params()
        if not all(params.get(p.name) for p in spec.params if p.kind == "file"):
            return False
        if spec.active is not None and not self.animated_params():
            return bool(spec.active(params))
        return True

    def filter(self, t_var: str = "t", extra: Optional[Dict[str, Any]] = None, easing: str = "linear") -> str:
        """The FFmpeg filter text; animated parameters become expressions of ``t_var`` (clip-local time),
        shaped by the clip's effects easing (F25)."""
        if self.spec is None:
            return ""
        from core.filter_graph import _piecewise_linear_expr

        params = self.resolved_params()
        for name, keys in self.animated_params().items():
            params[name] = _piecewise_linear_expr(keys, t_var, easing)
        params.update(extra or {})
        return self.spec.build(params)

    def with_keyframes_mapped(self, fn: Callable[[Keyframes], Keyframes]) -> "EffectInstance":
        """A copy with ``fn`` applied to every parameter's keyframe list (trims and splits)."""
        clone = copy.deepcopy(self)
        clone.keyframes = {name: [tuple(k) for k in fn(list(keys))] for name, keys in self.keyframes.items()}
        return clone

    def to_dict(self) -> Dict[str, Any]:
        out = {"id": self.id, "kind": self.kind, "params": dict(self.params), "enabled": self.enabled}
        if self.keyframes:
            out["keyframes"] = {name: [list(k) for k in keys] for name, keys in self.keyframes.items()}
        if self.mask is not None:
            out["mask"] = dict(self.mask)
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EffectInstance":
        # Unknown kinds (from a newer version) are kept so a save doesn't
        # drop them, and simply skipped when rendering.
        keyframes: Dict[str, Keyframes] = {}
        raw_keys = data.get("keyframes") or {}
        if isinstance(raw_keys, dict):
            for name, keys in raw_keys.items():
                try:
                    keyframes[str(name)] = [(float(k[0]), float(k[1])) for k in keys]
                except (TypeError, ValueError, IndexError):
                    continue
        return cls(
            kind=str(data.get("kind", "")),
            params=dict(data.get("params") or {}),
            enabled=bool(data.get("enabled", True)),
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            keyframes=keyframes,
            mask=_clean_mask(data.get("mask")),
        )


def _clean_mask(mask: Any) -> Optional[Dict[str, Any]]:
    from core.effect_masks import clean_mask

    return clean_mask(mask)


def effect_filters(effects: List[EffectInstance], stage: str, t_offset: float = 0.0, aspect: float = 16 / 9,
                   easing: str = "linear") -> str:
    """Comma-prefixed filter text for every renderable effect of ``stage``, in list order.

    ``t_offset`` is the clip-local time of the stream's first frame where
    the stream's own clock doesn't start at the clip's start (a speed ramp's
    later segments), so animated parameters still follow clip-local time.
    A masked effect (F16) is wrapped to apply only inside its mask; ``aspect``
    is the frame's width/height at this stage, so the mask isn't stretched.
    """
    t_var = "t" if abs(t_offset) < 1e-9 else f"(t+{t_offset:.6f})"
    out = ""
    for effect in effects:
        if effect.is_renderable() and effect.spec.stage == stage:
            mask = _clean_mask(effect.mask)
            if mask is not None and effect.spec.stage != "source":
                from core.effect_masks import masked_filter

                out += masked_filter(effect.filter(t_var, easing=easing), mask, aspect, t_offset)
            else:
                out += "," + effect.filter(t_var, easing=easing)
    return out


def effect_files(effects: List[EffectInstance]) -> List[str]:
    """Every file an enabled effect reads (LUTs), for the export's missing-file check."""
    files = []
    for effect in effects:
        if effect.is_renderable():
            params = effect.resolved_params()
            files += [params[p.name] for p in effect.spec.params if p.kind == "file"]
    return files


def keyframe_times(effects: List[EffectInstance]) -> List[float]:
    """Clip-local times of every animated effect parameter's keyframes."""
    times: List[float] = []
    for effect in effects:
        for keys in effect.animated_params().values():
            times += [t for t, _v in keys]
    return times


# ---------------------------------------------------------------------------
# Pre-format-3 colour/key fields <-> effect instances
# ---------------------------------------------------------------------------
#: Old ``Clip`` field -> (effect kind, parameter name; None = the ``enabled`` flag).
LEGACY_FIELDS: Dict[str, Tuple[str, Optional[str]]] = {
    "brightness": ("color_correction", "brightness"),
    "contrast": ("color_correction", "contrast"),
    "saturation": ("color_correction", "saturation"),
    "gamma": ("color_correction", "gamma"),
    "chroma_key_enabled": ("colorkey", None),
    "chroma_key_color": ("colorkey", "color"),
    "chroma_key_similarity": ("colorkey", "similarity"),
    "chroma_key_blend": ("colorkey", "blend"),
}


def legacy_default(name: str) -> Any:
    kind, param = LEGACY_FIELDS[name]
    return False if param is None else default_params(kind)[param]


def legacy_insert_index(kinds: List[str], kind: str) -> int:
    """Where a legacy kind goes in the stack: the old fixed order was colorkey, then eq, then the rest."""
    if kind == "colorkey":
        return 0
    return next((i + 1 for i, k in enumerate(kinds) if k == "colorkey"), 0)


def legacy_effect_dicts(values: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Effect dicts (in render order) equivalent to old-style field values; [] when all are neutral.

    A chroma key that was configured but switched off is kept as a
    disabled instance, so turning it back on restores its settings.
    """
    out: List[Dict[str, Any]] = []
    key_params = {p: values[f] for f, (k, p) in LEGACY_FIELDS.items() if k == "colorkey" and p and f in values}
    key_enabled = bool(values.get("chroma_key_enabled", False))
    if key_enabled or any(key_params.get(p) != default_params("colorkey")[p] for p in key_params):
        out.append(EffectInstance(kind="colorkey", params={**default_params("colorkey"), **key_params},
                                  enabled=key_enabled).to_dict())
    eq = {p: values[f] for f, (k, p) in LEGACY_FIELDS.items() if k == "color_correction" and f in values}
    merged = {**default_params("color_correction"), **eq}
    if _color_correction_active(merged):
        out.append(EffectInstance(kind="color_correction", params=merged).to_dict())
    return out
