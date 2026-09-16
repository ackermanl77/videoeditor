"""Undo/redo support using the Command pattern.

Every mutation the UI makes to the project's tracks/clips should go through
a :class:`Command` executed via the :class:`History` stack, so it can be
transparently undone and redone. This keeps ``core`` free of any GUI
concerns while giving the UI a simple, reliable undo/redo story.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, List, Optional


class Command(ABC):
    """Base class for a single, undoable editing operation."""

    #: Short human-readable label shown in Edit menu ("Undo Trim Clip").
    label: str = "Action"

    @abstractmethod
    def do(self) -> None:
        """Apply the change."""

    @abstractmethod
    def undo(self) -> None:
        """Reverse the change applied by :meth:`do`."""

    def redo(self) -> None:
        """Re-apply the change. Defaults to calling :meth:`do` again."""
        self.do()


class CompositeCommand(Command):
    """Bundles several commands into one atomic undo step.

    Used for operations that touch more than one clip at once (e.g. ripple
    delete/trim shifting every downstream clip) but should still undo/redo
    as a single unit rather than one step per affected clip. Each
    sub-command is constructed as normal (capturing its own "before" state
    in ``__init__``, before anything has run) and this class just sequences
    their ``do``/``undo`` — ``undo`` runs them in reverse order, since a
    later command may depend on state an earlier one changed.
    """

    def __init__(self, commands: List[Command], label: str):
        self.commands = commands
        self.label = label

    def do(self) -> None:
        for command in self.commands:
            command.do()

    def undo(self) -> None:
        for command in reversed(self.commands):
            command.undo()

    def redo(self) -> None:
        for command in self.commands:
            command.redo()


class History:
    """A bounded undo/redo stack of :class:`Command` objects."""

    def __init__(self, max_size: int = 200, on_change: Optional[Callable[[], None]] = None) -> None:
        self._undo_stack: List[Command] = []
        self._redo_stack: List[Command] = []
        self._max_size = max_size
        self._on_change = on_change

    def execute(self, command: Command) -> None:
        command.do()
        self._undo_stack.append(command)
        if len(self._undo_stack) > self._max_size:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self._notify()

    def undo(self) -> Optional[str]:
        if not self._undo_stack:
            return None
        command = self._undo_stack.pop()
        command.undo()
        self._redo_stack.append(command)
        self._notify()
        return command.label

    def redo(self) -> Optional[str]:
        if not self._redo_stack:
            return None
        command = self._redo_stack.pop()
        command.redo()
        self._undo_stack.append(command)
        self._notify()
        return command.label

    def clear(self) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._notify()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    @property
    def undo_label(self) -> Optional[str]:
        return self._undo_stack[-1].label if self._undo_stack else None

    @property
    def redo_label(self) -> Optional[str]:
        return self._redo_stack[-1].label if self._redo_stack else None

    def _notify(self) -> None:
        if self._on_change:
            self._on_change()
