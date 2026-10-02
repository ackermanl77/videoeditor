"""Resolve a text overlay's font family/bold/italic to a real font file.

FFmpeg's ``drawtext`` needs a ``fontfile=`` on Windows: the essentials build
has no Fontconfig configuration there, so without one it prints
"Fontconfig error: Cannot load default config file" and renders a fallback
monospace face whatever family the user picked (and drawtext has no
``bold``/``italic`` options at all -- style comes only from the file).

Resolution happens at export time on the machine doing the export, so a
project moved to another PC still picks that PC's copy of the font. Stdlib
only (``winreg`` on Windows, a directory scan elsewhere): ``core/`` stays
free of Qt and third-party imports.
"""

from __future__ import annotations

import os
import re
import sys
from functools import lru_cache
from typing import Dict, List, Optional

# Tried, in order, when the requested family isn't installed -- a
# proportional sans face that exists on practically every system, rather
# than drawtext's own monospace fallback.
FALLBACK_FAMILIES = ("Arial", "Segoe UI", "Helvetica", "DejaVu Sans", "Liberation Sans", "Noto Sans")

_FONT_EXTENSIONS = (".ttf", ".otf", ".ttc")
_REGISTRY_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _style_suffix(bold: bool, italic: bool) -> str:
    return {(False, False): "", (True, False): " Bold", (False, True): " Italic", (True, True): " Bold Italic"}[
        (bool(bold), bool(italic))
    ]


def _windows_font_index() -> Dict[str, str]:
    """Normalized face name (e.g. ``georgiabold``) -> absolute font path, from the registry."""
    index: Dict[str, str] = {}
    try:
        import winreg
    except ImportError:
        return index
    system_fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    user_fonts = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")
    # Per-user installs first, so a machine-wide font of the same name wins.
    for hive, base_dir in ((winreg.HKEY_CURRENT_USER, user_fonts), (winreg.HKEY_LOCAL_MACHINE, system_fonts)):
        try:
            key = winreg.OpenKey(hive, _REGISTRY_KEY)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name, value, _type = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if not isinstance(value, str) or not value.lower().endswith(_FONT_EXTENSIONS):
                    continue
                path = value if os.path.isabs(value) else os.path.join(base_dir, value)
                face = re.sub(r"\s*\((TrueType|OpenType)\)\s*$", "", name, flags=re.IGNORECASE)
                # "Cambria & Cambria Math" -> both names map to the same .ttc
                for part in face.split("&"):
                    index[_normalize(part)] = path
    return index


def _scanned_font_index() -> Dict[str, str]:
    """Best-effort index for macOS/Linux: file stems like ``DejaVuSans-Bold`` -> path."""
    dirs = [
        "/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts"),
        os.path.expanduser("~/.local/share/fonts"), "/Library/Fonts", "/System/Library/Fonts",
        os.path.expanduser("~/Library/Fonts"),
    ]
    index: Dict[str, str] = {}
    for base in dirs:
        for root, _dirs, files in os.walk(base):
            for name in files:
                stem, ext = os.path.splitext(name)
                if ext.lower() not in _FONT_EXTENSIONS:
                    continue
                key = _normalize(stem.replace("Regular", "").replace("Oblique", "Italic"))
                index.setdefault(key, os.path.join(root, name))
    return index


@lru_cache(maxsize=1)
def _font_index() -> Dict[str, str]:
    index = _windows_font_index() if sys.platform.startswith("win") else {}
    if not index:
        index = _scanned_font_index()
    return {k: v for k, v in index.items() if os.path.isfile(v)}


def _candidates(family: str, bold: bool, italic: bool) -> List[str]:
    suffix = _style_suffix(bold, italic)
    names = [family + suffix]
    if bold and italic:
        names += [family + " Bold Oblique"]
    elif italic:
        names += [family + " Oblique"]
    return [_normalize(n) for n in names]


def resolve_font_file(family: str, bold: bool = False, italic: bool = False) -> Optional[str]:
    """Path of the installed font file for ``family`` in the requested style, or ``None``.

    Order: the exact styled face of ``family``; that family's regular face
    (the style is then lost rather than switching family); then the styled
    and regular faces of each :data:`FALLBACK_FAMILIES` entry. ``None`` only
    when none of those exist (drawtext then uses its own default).
    """
    index = _font_index()
    families = [family] if family else []
    families += [f for f in FALLBACK_FAMILIES if f.lower() != (family or "").lower()]
    for fam in families:
        for key in _candidates(fam, bold, italic) + [_normalize(fam)]:
            if key in index:
                return index[key]
    return None
