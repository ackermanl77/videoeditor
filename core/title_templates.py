"""Title styles (F23): built-in looks for text clips -- lower thirds, centred titles, captions --
plus the user's own saved styles in ``~/.video_editor/title_styles.json``.

A style is every ``TextStyle`` field except the text itself, stored at 1080p scale: applying one
scales the font size, outline, shadow offset and box padding to the project's height, so a lower
third looks the same in a 720p and a 4K project. Applying keeps the clip's own text.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, fields
from typing import Any, Dict, Optional

from core.clip import TextStyle
from core.project import write_json_atomic

REFERENCE_HEIGHT = 1080
_SCALED = ("font_size", "outline_width", "shadow_offset", "box_padding")
_STYLE_FIELDS = tuple(f.name for f in fields(TextStyle) if f.name != "content")

BUILTIN_STYLES: Dict[str, Dict[str, Any]] = {
    "Lower Third": {
        "font_family": "Arial", "font_size": 46, "color": "#FFFFFF", "outline_color": "#000000", "outline_width": 0,
        "bold": True, "italic": False, "position_x": 0.27, "position_y": 0.84, "alignment": "left",
        "shadow_enabled": False, "shadow_color": "#000000", "shadow_offset": 3,
        "box_enabled": True, "box_color": "#1E3A8A", "box_opacity": 0.85, "box_padding": 18,
    },
    "Centered Title": {
        "font_family": "Arial", "font_size": 96, "color": "#FFFFFF", "outline_color": "#000000", "outline_width": 3,
        "bold": True, "italic": False, "position_x": 0.5, "position_y": 0.5, "alignment": "center",
        "shadow_enabled": True, "shadow_color": "#000000", "shadow_offset": 4,
        "box_enabled": False, "box_color": "#000000", "box_opacity": 0.5, "box_padding": 12,
    },
    "Caption": {
        "font_family": "Arial", "font_size": 44, "color": "#FFFFFF", "outline_color": "#000000", "outline_width": 2,
        "bold": False, "italic": False, "position_x": 0.5, "position_y": 0.9, "alignment": "center",
        "shadow_enabled": False, "shadow_color": "#000000", "shadow_offset": 3,
        "box_enabled": True, "box_color": "#000000", "box_opacity": 0.55, "box_padding": 12,
    },
    "Chapter Heading": {
        "font_family": "Georgia", "font_size": 64, "color": "#F5F5F4", "outline_color": "#000000", "outline_width": 0,
        "bold": False, "italic": True, "position_x": 0.5, "position_y": 0.18, "alignment": "center",
        "shadow_enabled": True, "shadow_color": "#000000", "shadow_offset": 3,
        "box_enabled": False, "box_color": "#000000", "box_opacity": 0.5, "box_padding": 12,
    },
    "Corner Tag": {
        "font_family": "Arial", "font_size": 32, "color": "#111827", "outline_color": "#000000", "outline_width": 0,
        "bold": True, "italic": False, "position_x": 0.88, "position_y": 0.08, "alignment": "right",
        "shadow_enabled": False, "shadow_color": "#000000", "shadow_offset": 3,
        "box_enabled": True, "box_color": "#FACC15", "box_opacity": 1.0, "box_padding": 10,
    },
}


def style_of(text_style: TextStyle, canvas_height: int) -> Dict[str, Any]:
    """A text clip's style (everything but its text), converted to 1080p scale for saving."""
    data = {name: value for name, value in asdict(text_style).items() if name in _STYLE_FIELDS}
    factor = REFERENCE_HEIGHT / max(1, canvas_height)
    for name in _SCALED:
        data[name] = max(0, int(round(data[name] * factor)))
    return data


def apply_style(current: TextStyle, style: Dict[str, Any], canvas_height: int) -> TextStyle:
    """``current`` (its text kept) with ``style`` applied, scaled from 1080p to ``canvas_height``.
    Unknown or missing keys are ignored, so a style saved by another version still applies."""
    values = asdict(current)
    factor = max(1, canvas_height) / REFERENCE_HEIGHT
    for name in _STYLE_FIELDS:
        if name not in style:
            continue
        value = style[name]
        if name in _SCALED:
            try:
                value = max(1 if name == "font_size" else 0, int(round(float(value) * factor)))
            except (TypeError, ValueError):
                continue
        values[name] = value
    return TextStyle(**values)


def user_styles_path() -> str:
    path = os.path.join(os.path.expanduser("~"), ".video_editor")
    os.makedirs(path, exist_ok=True)
    return os.path.join(path, "title_styles.json")


def load_user_styles(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    try:
        with open(path or user_styles_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def all_styles(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Built-ins first, then the user's (a saved style with a built-in's name shadows it)."""
    return {**BUILTIN_STYLES, **load_user_styles(path)}


def save_user_style(name: str, style: Dict[str, Any], path: Optional[str] = None) -> None:
    name = name.strip()
    if not name:
        raise ValueError("A style needs a name.")
    styles = load_user_styles(path)
    styles[name] = {k: v for k, v in style.items() if k in _STYLE_FIELDS}
    write_json_atomic(path or user_styles_path(), styles, indent=2)


def delete_user_style(name: str, path: Optional[str] = None) -> bool:
    styles = load_user_styles(path)
    if name not in styles:
        return False
    del styles[name]
    write_json_atomic(path or user_styles_path(), styles, indent=2)
    return True
