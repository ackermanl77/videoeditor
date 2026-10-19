"""Workspaces (F30): named window layouts -- which docks are open and how big the panels are.

Four built-in workspaces (Window > Workspaces, Alt+Shift+1..4):

- **Editing** -- the default: Media Bin, Preview, Properties, a roomy timeline; no extra docks.
- **Color** -- the Video Scopes dock open beside a larger Properties panel (colour correction lives
  there) and preview.
- **Audio** -- the Audio Mixer open under a taller timeline.
- **Captions** -- the Markers panel open, the timeline taller (captions are timed against it) and
  the Properties panel wider for the caption text.

"Save Workspace As..." stores the current layout (QMainWindow dock state plus both splitters) under
a name in QSettings; saved workspaces appear in the same menu and can be deleted. The workspace in
use is remembered for the next start.
"""

from __future__ import annotations

from typing import Dict, List

from PySide6.QtCore import QByteArray, QSettings

DOCKS = ("render_queue_dock", "markers_dock", "scopes_dock", "mixer_dock")

# name -> (open docks, top splitter proportions [bin, preview, properties], main proportions [top, timeline])
BUILT_IN: Dict[str, tuple] = {
    "Editing": ((), (2, 4, 2), (3, 2)),
    "Color": (("scopes_dock",), (1, 4, 3), (3, 2)),
    "Audio": (("mixer_dock",), (2, 4, 2), (1, 1)),
    "Captions": (("markers_dock",), (2, 4, 3), (2, 3)),
}
SETTINGS_PREFIX = "workspaces/"
CURRENT_KEY = "workspaces_current"


def _proportional(total: int, weights) -> List[int]:
    weights = list(weights)
    whole = sum(weights)
    sizes = [max(60, int(total * w / whole)) for w in weights]
    return sizes


def apply_built_in(window, name: str) -> None:
    open_docks, top, main = BUILT_IN[name]
    for dock in DOCKS:
        widget = getattr(window, dock, None)
        if widget is not None:
            widget.setVisible(dock in open_docks)
    window.top_splitter.setSizes(_proportional(max(600, window.top_splitter.width()), top))
    window.main_splitter.setSizes(_proportional(max(400, window.main_splitter.height()), main))


def saved_names() -> List[str]:
    settings = QSettings()
    settings.beginGroup(SETTINGS_PREFIX.rstrip("/"))
    names = sorted(settings.childGroups())
    settings.endGroup()
    return names


def save_current(window, name: str) -> None:
    settings = QSettings()
    key = SETTINGS_PREFIX + name
    settings.setValue(f"{key}/state", window.saveState())
    settings.setValue(f"{key}/top", window.top_splitter.saveState())
    settings.setValue(f"{key}/main", window.main_splitter.saveState())


def apply_saved(window, name: str) -> bool:
    settings = QSettings()
    key = SETTINGS_PREFIX + name
    state = settings.value(f"{key}/state", type=QByteArray)
    if not state:
        return False
    window.restoreState(state)
    for splitter, part in ((window.top_splitter, "top"), (window.main_splitter, "main")):
        value = settings.value(f"{key}/{part}", type=QByteArray)
        if value:
            splitter.restoreState(value)
    return True


def delete_saved(name: str) -> None:
    QSettings().remove(SETTINGS_PREFIX + name)


def apply(window, name: str) -> bool:
    """Apply a built-in or saved workspace by name and remember it; False when unknown."""
    if name in BUILT_IN:
        apply_built_in(window, name)
    elif not apply_saved(window, name):
        return False
    QSettings().setValue(CURRENT_KEY, name)
    return True


def current() -> str:
    return str(QSettings().value(CURRENT_KEY, "Editing") or "Editing")
