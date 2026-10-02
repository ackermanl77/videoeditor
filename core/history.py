"""Undo/redo support using the Command pattern.

Every mutation the UI makes to the project's tracks/clips should go through
a :class:`Command` executed via the :class:`History` stack, so it can be
transparently undone and redone. This keeps ``core`` free of any GUI
concerns while giving the UI a simple, reliable undo/redo story.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Callable, Hashable, List, Optional

#: How long after the last merged edit a new edit with the same
#: ``merge_key`` still joins it (seconds). A continuous slider drag, wheel
#: spin or held arrow key produces edits far closer together than this, so
#: one gesture becomes one undo step; a separate edit a moment later is its
#: own step. Callers can also end a gesture explicitly (History.end_merge).
MERGE_WINDOW_SECONDS = 0.7


class Command(ABC):
    """Base class for a single, undoable editing operation."""

    #: Short human-readable label shown in Edit menu ("Undo Trim Clip").
    label: str = "Action"

    #: Commands with an equal, non-None key may be merged into one undo step
    #: (Qt's ``QUndoCommand::mergeWith`` semantics) -- see History.execute.
    merge_key: Optional[Hashable] = None

    def merge(self, other: "Command") -> bool:
        """Absorb ``other`` (already applied) into this command; return False to refuse.

        On success this command must undo straight back to its *own*
        original state and redo to ``other``'s result.
        """
        return False

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


class InvalidEditError(ValueError):
    """Raised in strict mode when a command would leave the project invalid (see ``History``)."""

    def __init__(self, problems: List[str]):
        self.problems = list(problems)
        super().__init__("Edit rejected: " + "; ".join(self.problems))


class History:
    """A bounded undo/redo stack of :class:`Command` objects.

    ``validator`` (strict mode, used by the tests and when the
    ``VIDEO_EDITOR_STRICT`` environment variable is set) is called before and
    after each new command; a command that introduces a problem the project
    didn't already have (e.g. two clips overlapping on one track) is undone
    again and :class:`InvalidEditError` is raised. Problems that existed
    before the edit (an old project loaded with overlaps) don't block edits.

    The clean index (:meth:`mark_clean` / :attr:`is_clean`) records which undo
    position matches the file on disk, so undoing back to the saved state
    clears the window's "unsaved changes" star.
    """

    def __init__(
        self,
        max_size: int = 200,
        on_change: Optional[Callable[[], None]] = None,
        validator: Optional[Callable[[], List[str]]] = None,
    ) -> None:
        self._undo_stack: List[Command] = []
        self._redo_stack: List[Command] = []
        self._max_size = max_size
        self._on_change = on_change
        self._validator = validator
        self._merge_open = False
        self._last_merge_time = 0.0
        # Identity of the command on top of the undo stack when the project
        # was last saved (None = the empty stack), and whether that saved
        # state is still reachable by undo/redo at all.
        self._clean_top: Optional[Command] = None
        self._clean_reachable = True

    def execute(self, command: Command) -> None:
        before = set(self._validator()) if self._validator is not None else None
        command.do()
        if before is not None:
            new_problems = [p for p in self._validator() if p not in before]
            if new_problems:
                command.undo()
                raise InvalidEditError(new_problems)
        now = time.monotonic()
        top = self._undo_stack[-1] if self._undo_stack else None
        if (
            top is not None
            and self._merge_open
            and command.merge_key is not None
            and top.merge_key == command.merge_key
            and now - self._last_merge_time <= MERGE_WINDOW_SECONDS
            and top.merge(command)
        ):
            self._last_merge_time = now
            self._redo_stack.clear()
            self._notify()
            return
        self._undo_stack.append(command)
        if len(self._undo_stack) > self._max_size:
            dropped = self._undo_stack.pop(0)
            if dropped is self._clean_top or (self._clean_top is None and self._clean_reachable):
                self._clean_reachable = False  # the saved state fell off the bottom
        if any(c is self._clean_top for c in self._redo_stack):
            self._clean_reachable = False  # the saved state was in the discarded redo branch
        self._redo_stack.clear()
        self._merge_open = command.merge_key is not None
        self._last_merge_time = now
        self._notify()

    def end_merge(self) -> None:
        """Close the current mergeable gesture (e.g. on slider release): the next edit is a new step."""
        self._merge_open = False

    def mark_clean(self) -> None:
        """Record the current undo position as the saved state.

        Also ends any open merge, so an edit after saving becomes a new undo
        step instead of silently changing the command that marks "saved".
        """
        self._clean_top = self._undo_stack[-1] if self._undo_stack else None
        self._clean_reachable = True
        self._merge_open = False
        self._notify()

    @property
    def is_clean(self) -> bool:
        """True when the undo position is exactly the one last marked clean."""
        if not self._clean_reachable:
            return False
        top = self._undo_stack[-1] if self._undo_stack else None
        return top is self._clean_top

    def undo(self) -> Optional[str]:
        if not self._undo_stack:
            return None
        command = self._undo_stack.pop()
        command.undo()
        self._merge_open = False
        self._redo_stack.append(command)
        self._notify()
        return command.label

    def redo(self) -> Optional[str]:
        if not self._redo_stack:
            return None
        command = self._redo_stack.pop()
        command.redo()
        self._merge_open = False
        self._undo_stack.append(command)
        self._notify()
        return command.label

    def clear(self) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._merge_open = False
        self._clean_top = None
        self._clean_reachable = True
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
