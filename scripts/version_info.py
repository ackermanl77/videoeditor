"""Render the Windows version resource (version_info.txt) from ``core.__version__``.

build.spec calls :func:`write_version_file` so the exe's Properties > Details
always show the app's real version; ``version_info.txt`` in the repo is only
the template (``{filevers}`` / ``{version}`` placeholders).
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "version_info.txt")


def app_version() -> str:
    """``__version__`` read from core/__init__.py without importing the package."""
    with open(os.path.join(ROOT, "core", "__init__.py"), "r", encoding="utf-8") as f:
        match = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.MULTILINE)
    if not match:
        raise RuntimeError("core/__init__.py defines no __version__")
    return match.group(1)


def version_tuple(version: str) -> tuple:
    """``"0.2.0"`` -> ``(0, 2, 0, 0)`` (four numeric parts, as VSVersionInfo needs)."""
    parts = [int(p) for p in re.findall(r"\d+", version)[:4]]
    return tuple(parts + [0] * (4 - len(parts)))


def render(version: str, template_path: str = TEMPLATE) -> str:
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()
    numbers = version_tuple(version)
    return template.replace("{filevers}", repr(numbers)).replace("{version}", ".".join(map(str, numbers)))


def write_version_file(out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "version_info.generated.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(app_version()))
    return path


def check_tag(tag: str) -> int:
    """Release guard: ``v0.2.0`` must match ``core.__version__``; prints and returns an exit code."""
    expected = "v" + app_version()
    if tag != expected:
        print(f"Tag {tag!r} doesn't match core.__version__ ({expected!r}). Bump core/__init__.py or retag.")
        return 1
    print(f"Tag {tag} matches core.__version__.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check-tag":
        sys.exit(check_tag(sys.argv[2]))
    print(app_version())
