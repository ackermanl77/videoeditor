"""Screen-reader support for the timeline (F13).

The timeline is a ``QGraphicsView`` full of ``QGraphicsObject`` items, which
Qt exposes to assistive technology as one opaque picture. This module
installs a ``QAccessible`` factory that gives it a real tree instead:

* the view ("Timeline", a pane) has one child per track row, top to bottom;
* each track (a list, named e.g. "Video track Video 1, 3 clips, muted")
  has one child per clip on it, in time order;
* each clip (a list item) is named by its spoken description
  ("Video clip Arbol.mp4, 00:00:12:00 to 00:00:20:00, track Video 1"), with
  its effects and link/offline state as the description, and reports
  selected/focused states and its on-screen rectangle.

Interfaces are created through the factory for the items themselves (they
are QObjects), so Qt's accessibility cache owns and deletes them with their
item -- nothing here keeps Python references to interfaces.
:func:`notify_focus` tells a running screen reader which clip the keyboard
selection moved to and has it announced.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QObject, QPoint, QRect
from PySide6.QtGui import QAccessible, QAccessibleEvent, QAccessibleInterface, QAccessibleObject
from PySide6.QtWidgets import QAccessibleWidget, QGraphicsView

try:  # Qt 6.8+
    from PySide6.QtGui import QAccessibleAnnouncementEvent
except ImportError:  # pragma: no cover - older Qt: focus events only
    QAccessibleAnnouncementEvent = None

_installed = False


def _timeline_of(view: QGraphicsView):
    return getattr(view, "_timeline_widget", None)


def _view_of_item(item) -> Optional[QGraphicsView]:
    scene = item.scene()
    views = scene.views() if scene is not None else []
    return views[0] if views else None


def _scene_rect_to_screen(view: QGraphicsView, scene_rect) -> QRect:
    polygon = view.mapFromScene(scene_rect)
    rect = polygon.boundingRect()
    top_left = view.viewport().mapToGlobal(rect.topLeft())
    return QRect(top_left, rect.size())


def _headers(view: QGraphicsView) -> List:
    timeline = _timeline_of(view)
    return list(getattr(timeline, "_header_items", []) or [])


def _clips_in_row(view: QGraphicsView, row: int) -> List:
    timeline = _timeline_of(view)
    items = [i for i in (getattr(timeline, "_clip_items", {}) or {}).values() if i.row == row]
    return sorted(items, key=lambda i: i.x())


def _iface(obj: Optional[QObject]) -> Optional[QAccessibleInterface]:
    return QAccessible.queryAccessibleInterface(obj) if obj is not None else None


class TimelineViewAccessible(QAccessibleWidget):
    """The timeline view: a pane whose children are the track rows."""

    def __init__(self, view: QGraphicsView):
        super().__init__(view, QAccessible.Pane, "Timeline")

    def text(self, kind):
        if kind == QAccessible.Name:
            return "Timeline"
        if kind == QAccessible.Description:
            return ("Tracks and clips. Tab and Shift+Tab select the next or previous clip, "
                    "Alt+Left and Alt+Right nudge it, Enter edits its properties.")
        return super().text(kind)

    def childCount(self) -> int:
        return len(_headers(self.widget()))

    def child(self, index: int):
        headers = _headers(self.widget())
        return _iface(headers[index]) if 0 <= index < len(headers) else None

    def indexOfChild(self, child) -> int:
        obj = child.object() if child is not None else None
        for i, header in enumerate(_headers(self.widget())):
            if header is obj:
                return i
        return -1

    def childAt(self, x: int, y: int):
        for i in range(self.childCount()):
            child = self.child(i)
            if child is not None and child.rect().contains(QPoint(x, y)):
                return child
        return None


class _ItemAccessible(QAccessibleObject):
    """Shared plumbing for track and clip items (QGraphicsObjects inside the timeline view)."""

    def item(self):
        return self.object()

    def view(self) -> Optional[QGraphicsView]:
        item = self.item()
        return _view_of_item(item) if item is not None else None

    def isValid(self) -> bool:
        return self.item() is not None and self.view() is not None

    def window(self):
        view = self.view()
        return _iface(view.window()) if view is not None else None

    def setText(self, kind, text) -> None:
        pass


class TrackAccessible(_ItemAccessible):
    """A track row (its header item): a list of the clips on that track."""

    def role(self):
        return QAccessible.List

    def text(self, kind):
        header = self.item()
        if header is None:
            return ""
        if kind == QAccessible.Name:
            return track_accessible_name(header, len(_clips_in_row(self.view(), header.row)) if self.view() else 0)
        if kind == QAccessible.Description:
            return "The header's M, L, H and D buttons mute, lock, hide and duck the track."
        return ""

    def state(self):
        state = QAccessible.State()
        header = self.item()
        state.selectable = True
        state.selected = bool(header is not None and getattr(header, "selected", False))
        return state

    def rect(self) -> QRect:
        view = self.view()
        header = self.item()
        if view is None or header is None:
            return QRect()
        row_rect = header.sceneBoundingRect()
        row_rect.setWidth(max(row_rect.width(), view.sceneRect().width()))
        return _scene_rect_to_screen(view, row_rect)

    def parent(self):
        return _iface(self.view())

    def childCount(self) -> int:
        view, header = self.view(), self.item()
        return len(_clips_in_row(view, header.row)) if view is not None and header is not None else 0

    def child(self, index: int):
        view, header = self.view(), self.item()
        clips = _clips_in_row(view, header.row) if view is not None and header is not None else []
        return _iface(clips[index]) if 0 <= index < len(clips) else None

    def indexOfChild(self, child) -> int:
        obj = child.object() if child is not None else None
        view, header = self.view(), self.item()
        clips = _clips_in_row(view, header.row) if view is not None and header is not None else []
        return next((i for i, clip in enumerate(clips) if clip is obj), -1)

    def childAt(self, x: int, y: int):
        for i in range(self.childCount()):
            child = self.child(i)
            if child is not None and child.rect().contains(QPoint(x, y)):
                return child
        return None


class ClipAccessible(_ItemAccessible):
    """One clip: a selectable, focusable list item named by its spoken description."""

    def role(self):
        return QAccessible.ListItem

    def text(self, kind):
        clip = self.item()
        if clip is None:
            return ""
        if kind == QAccessible.Name:
            return clip.accessible_name
        if kind == QAccessible.Description:
            return clip.accessible_description
        return ""

    def state(self):
        state = QAccessible.State()
        clip = self.item()
        state.selectable = True
        state.focusable = True
        if clip is not None:
            state.selected = clip.isSelected()
            state.focused = clip.hasFocus()
        return state

    def rect(self) -> QRect:
        view, clip = self.view(), self.item()
        if view is None or clip is None:
            return QRect()
        return _scene_rect_to_screen(view, clip.sceneBoundingRect())

    def parent(self):
        view, clip = self.view(), self.item()
        if view is None or clip is None:
            return None
        for header in _headers(view):
            if header.row == clip.row:
                return _iface(header)
        return _iface(view)

    def childCount(self) -> int:
        return 0

    def child(self, index: int):
        return None

    def indexOfChild(self, child) -> int:
        return -1

    def childAt(self, x: int, y: int):
        return None


def track_accessible_name(header, clip_count: int) -> str:
    kind = "Video" if header.track_kind == "video" else "Audio"
    text = f"{kind} track {header.name}, {clip_count} clip{'s' if clip_count != 1 else ''}"
    states = [label for button, label in (
        (header.mute_btn, "muted"), (header.lock_btn, "locked"), (header.hide_btn, "hidden"),
        (header.duck_btn, "ducked"),
    ) if button.active]
    return text + (", " + ", ".join(states) if states else "")


def _factory(_class_name: str, obj: QObject):
    from ui.widgets.timeline_items import ClipItem, TrackHeaderItem
    from ui.widgets.timeline_widget import TimelineView

    if isinstance(obj, TimelineView):
        return TimelineViewAccessible(obj)
    if isinstance(obj, ClipItem):
        return ClipAccessible(obj)
    if isinstance(obj, TrackHeaderItem):
        return TrackAccessible(obj)
    return None


def install() -> None:
    """Register the factory once per process (QAccessible keeps it for the application's life)."""
    global _installed
    if not _installed:
        QAccessible.installFactory(_factory)
        _installed = True


def notify_focus(clip_item, announce: bool = True) -> None:
    """Tell assistive technology the keyboard focus/selection moved to ``clip_item``, and announce it.

    A no-op unless a screen reader (or another accessibility client) is active.
    """
    if clip_item is None or not QAccessible.isActive():
        return
    QAccessible.updateAccessibility(QAccessibleEvent(clip_item, QAccessible.Focus))
    QAccessible.updateAccessibility(QAccessibleEvent(clip_item, QAccessible.Selection))
    view = _view_of_item(clip_item)
    if announce and view is not None and QAccessibleAnnouncementEvent is not None:
        QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(view, clip_item.accessible_name))
