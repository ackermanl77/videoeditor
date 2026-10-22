"""Regenerate the Windows icons from assets/icons/app_icon_256.png (K8, a developer step -- needs
Pillow, which the app itself doesn't; the generated .ico files are checked in). The artwork's master
is assets/icons/app_icon.svg (a clapperboard over timeline clips); app_icon.png, app_icon_256.png
and splash.png are rendered from it:

- assets/icons/app_icon.ico: every size Explorer and the taskbar ask for, 16-256 including 20, 24
  and 40 (the taskbar's 24 px used to be scaled down from 32 and looked blurry);
- assets/icons/vproj.ico: the .vproj document icon (a page with a folded corner and the app icon),
  embedded as the exe's second icon group and named by the installer's DefaultIcon (VideoEditor.exe,1).

    python scripts/make_icons.py
"""

from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICONS = os.path.join(ROOT, "assets", "icons")
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _sizes():
    return [(size, size) for size in SIZES]


def make_app_icon(source: str, out_path: str) -> str:
    from PIL import Image

    with Image.open(source) as art:
        art.convert("RGBA").save(out_path, format="ICO", sizes=_sizes())
    return out_path


def make_document_icon(source: str, out_path: str) -> str:
    from PIL import Image, ImageDraw

    scale = 4
    side = 256 * scale
    page = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(page)
    left, top, right, bottom = int(side * 0.16), int(side * 0.04), int(side * 0.84), int(side * 0.96)
    fold = int(side * 0.2)
    outline, width = (90, 98, 112, 255), max(2, side // 64)
    draw.polygon([(left, top), (right - fold, top), (right, top + fold), (right, bottom), (left, bottom)],
                 fill=(250, 251, 253, 255), outline=outline, width=width)
    draw.polygon([(right - fold, top), (right - fold, top + fold), (right, top + fold)], fill=(214, 220, 230, 255), outline=outline, width=width)
    with Image.open(source) as art:
        badge = int(side * 0.52)
        page.alpha_composite(art.convert("RGBA").resize((badge, badge), Image.LANCZOS), ((side - badge) // 2, int(side * 0.36)))
    page.resize((256, 256), Image.LANCZOS).save(out_path, format="ICO", sizes=_sizes())
    return out_path


if __name__ == "__main__":
    source = os.path.join(ICONS, "app_icon_256.png")
    print(make_app_icon(source, os.path.join(ICONS, "app_icon.ico")))
    print(make_document_icon(source, os.path.join(ICONS, "vproj.ico")))
