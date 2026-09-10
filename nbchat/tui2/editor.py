"""Line editor component for the TUI v2 (Phase 3).

A multi-line text editor with a cursor, basic vi-free editing
(emacs-style kill ring), word motion, undo/redo, and trailing-backslash
line continuation.  Pure state + rendering: keys come in via
:meth:`handle`, the frame is produced by :meth:`render`.  Nothing here
touches the terminal.

Keymap (single-character ``name`` values from
:class:`~nbchat.tui2.keys.Key`):

+----------------------+------------------------------------------+
| key                  | action                                   |
+======================+==========================================+
| char / space         | insert                                   |
| enter                | new line (or submit when not multi)      |
| shift+enter          | explicit newline in multi-line mode      |
| backspace / delete   | delete at / after cursor                 |
| left / right         | move cursor                              |
| up / down            | move line                                |
| home / end           | line start / end                         |
| ctrl+a / ctrl+e      | line start / end                         |
| ctrl+b / ctrl+f      | move cursor                              |
| ctrl+u               | kill to line start                       |
| ctrl+k               | kill to line end                         |
| ctrl+w               | kill previous word                       |
| ctrl+y               | yank (insert) kill ring                  |
| ctrl+z / ctrl+r      | undo / redo                              |
| ctrl+c / ctrl+d      | submit (or clear when empty)             |
| ctrl+l               | (ignored here — app owns redraw)         |
+----------------------+------------------------------------------+

A trailing ``\\`` at end of line joins the next line on render
(continuation) but the buffer keeps the ``\\`` so editing stays
predictable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .frame import Line, Segment, Style
from .theme import DARK


@dataclass
class _Edit:
    """One undo/redo operation: (line, col, before, after)."""
    line: int
    col: int
    before: str
    after: str


@dataclass
class LineEditor:
    """Multi-line editor state.

    ``placeholder`` is shown dimmed when the buffer is empty and the
    editor is not focused.  ``rows`` caps the visible height (0 =
    unbounded); longer content is scrolled via ``scroll`` (lines from
    the bottom).  ``multiline`` controls whether ``enter`` inserts a
    newline or submits (the app inspects ``submitted`` after
    :meth:`handle` returns ``True``).
    """
    placeholder: str = "Type a message…"
    multiline: bool = True
    rows: int = 0
    focused: bool = True

    lines: List[str] = field(default_factory=lambda: [""])
    cursor_line: int = 0
    cursor_col: int = 0
    scroll: int = 0
    submitted: bool = False

    _kill: str = ""
    _undo: List[_Edit] = field(default_factory=list)
    _redo: List[_Edit] = field(default_factory=list)

    # ── internals ──────────────────────────────────────────────────────
    def _record(self, edit: _Edit) -> None:
        self._undo.append(edit)
        if len(self._undo) > 200:
            self._undo.pop(0)
        self._redo.clear()

    def _clamp_cursor(self) -> None:
        self.cursor_line = max(0, min(self.cursor_line, len(self.lines) - 1))
        self.cursor_col = max(0, min(self.cursor_col, len(self.lines[self.cursor_line])))

    def _apply(self, fn) -> None:
        """Run *fn* on the buffer, recording an undo entry if it changed."""
        before = list(self.lines)
        fn()
        after = list(self.lines)
        if before != after:
            self._record(_Edit(self.cursor_line, self.cursor_col,
                               "\n".join(before), "\n".join(after)))

    def _insert_text(self, text: str) -> None:
        def do() -> None:
            line = self.lines[self.cursor_line]
            self.lines[self.cursor_line] = (
                line[:self.cursor_col] + text + line[self.cursor_col:])
            self.cursor_col += len(text)
        self._apply(do)

    def _delete_left(self) -> None:
        def do() -> None:
            if self.cursor_col > 0:
                line = self.lines[self.cursor_line]
                self.lines[self.cursor_line] = (
                    line[:self.cursor_col - 1] + line[self.cursor_col:])
                self.cursor_col -= 1
            elif self.cursor_line > 0:
                prev = self.lines.pop(self.cursor_line - 1)
                cur = self.lines.pop(self.cursor_line)
                merged = prev + cur
                self.lines.insert(self.cursor_line - 1, merged)
                self.cursor_line -= 1
                self.cursor_col = len(prev)
        self._apply(do)

    def _delete_right(self) -> None:
        def do() -> None:
            line = self.lines[self.cursor_line]
            if self.cursor_col < len(line):
                self.lines[self.cursor_line] = (
                    line[:self.cursor_col] + line[self.cursor_col + 1:])
            elif self.cursor_line < len(self.lines) - 1:
                self.lines[self.cursor_line] = line + self.lines.pop(
                    self.cursor_line + 1)
        self._apply(do)

    def _kill_to_line_end(self) -> None:
        def do() -> None:
            line = self.lines[self.cursor_line]
            self._kill = line[self.cursor_col:]
            self.lines[self.cursor_line] = line[:self.cursor_col]
        self._apply(do)

    def _kill_to_line_start(self) -> None:
        def do() -> None:
            line = self.lines[self.cursor_line]
            self._kill = line[:self.cursor_col]
            self.lines[self.cursor_line] = line[self.cursor_col:]
            self.cursor_col = 0
        self._apply(do)

    def _kill_previous_word(self) -> None:
        def do() -> None:
            line = self.lines[self.cursor_line]
            pos = self.cursor_col
            # skip spaces
            while pos > 0 and line[pos - 1] == " ":
                pos -= 1
            # skip word chars
            while pos > 0 and line[pos - 1] != " ":
                pos -= 1
            if pos < self.cursor_col:
                self._kill = line[pos:self.cursor_col]
                self.lines[self.cursor_line] = (
                    line[:pos] + line[self.cursor_col:])
                self.cursor_col = pos
        self._apply(do)

    def _newline(self) -> None:
        def do() -> None:
            line = self.lines[self.cursor_line]
            self.lines[self.cursor_line] = line[:self.cursor_col]
            self.lines.insert(self.cursor_line + 1, line[self.cursor_col:])
            self.cursor_line += 1
            self.cursor_col = 0
        self._apply(do)

    def _move(self, dl: int, dc: int) -> None:
        self.cursor_line += dl
        self.cursor_col += dc
        if not (0 <= self.cursor_line < len(self.lines)):
            self.cursor_line = max(0, min(self.cursor_line, len(self.lines) - 1))
            self.cursor_col = 0
        self._clamp_cursor()

    # ── undo / redo ────────────────────────────────────────────────────
    def undo(self) -> None:
        if not self._undo:
            return
        edit = self._undo.pop()
        self.lines = edit.before.split("\n") or [""]
        self.cursor_line, self.cursor_col = edit.line, edit.col
        self._redo.append(edit)
        self._clamp_cursor()

    def redo(self) -> None:
        if not self._redo:
            return
        edit = self._redo.pop()
        self.lines = edit.after.split("\n") or [""]
        self.cursor_line, self.cursor_col = edit.line, edit.col
        self._undo.append(edit)
        self._clamp_cursor()

    # ── key handling ───────────────────────────────────────────────────
    def handle(self, key_name: str, text: str = "") -> bool:
        """Handle one parsed key.

        Returns ``True`` when a submit was requested (``enter`` in
        single-line mode, ``ctrl+c``/``ctrl+d`` with content); the
        caller should then read :meth:`text` and reset or keep the
        buffer.  ``ctrl+d`` on an empty buffer returns ``False`` and
        does nothing (the app maps that to quit).
        """
        if key_name == "ctrl+enter":
            return False
        if key_name in ("enter", "shift+enter", "ctrl+j"):
            if self.multiline:
                self._newline()
                return False
            self.submitted = True
            return True
        if key_name in ("ctrl+c", "ctrl+d"):
            if self.text():
                self.submitted = True
                return True
            return False
        if key_name == "backspace":
            self._delete_left()
            return False
        if key_name == "delete":
            self._delete_right()
            return False
        if key_name in ("left", "ctrl+b"):
            self._move(0, -1)
            return False
        if key_name in ("right", "ctrl+f"):
            self._move(0, +1)
            return False
        if key_name == "up":
            self._move(-1, 0)
            return False
        if key_name == "down":
            self._move(+1, 0)
            return False
        if key_name in ("home", "ctrl+a"):
            self.cursor_col = 0
            return False
        if key_name in ("end", "ctrl+e"):
            self.cursor_col = len(self.lines[self.cursor_line])
            return False
        if key_name == "ctrl+k":
            self._kill_to_line_end()
            return False
        if key_name == "ctrl+u":
            self._kill_to_line_start()
            return False
        if key_name == "ctrl+w":
            self._kill_previous_word()
            return False
        if key_name == "ctrl+y":
            if self._kill:
                self._insert_text(self._kill)
            return False
        if key_name == "ctrl+z":
            self.undo()
            return False
        if key_name == "ctrl+r":
            self.redo()
            return False
        if text and not key_name.startswith(("ctrl", "alt")):
            # printable character (or an escape key mapped to a char)
            if key_name in ("escape", "tab"):
                return False
            self._insert_text(text)
            return False
        return False

    # ── value ──────────────────────────────────────────────────────────
    @property
    def cursor(self):
        """Return ``(line_no, col)`` of the cursor."""
        self._clamp_cursor()
        return self.cursor_line, self.cursor_col

    def text(self) -> str:
        return "\n".join(self.lines).strip()

    def clear(self) -> None:
        if self.lines == [""] and not self._undo:
            return
        self._undo.append(_Edit(self.cursor_line, self.cursor_col,
                                "\n".join(self.lines), ""))
        self.lines = [""]
        self.cursor_line = self.cursor_col = 0
        self.scroll = 0
        self._redo.clear()

    # ── rendering ──────────────────────────────────────────────────────
    def render(self, width: int) -> List[Line]:
        from .components import _fit_row, blank
        from .theme import DARK

        # Backslash continuation: fold ``\\`` + newline for display.
        display: List[str] = []
        for i, ln in enumerate(self.lines):
            if (ln.endswith("\\") and i < len(self.lines) - 1
                    and not ln.endswith("\\\\")):
                display[-1] = display[-1][:-1] + " " + self.lines[i + 1] \
                    if display else " " + self.lines[i + 1]
            else:
                display.append(ln)
        if not display:
            display = [""]

        text_style = DARK.accent if self.focused else DARK.muted
        empty = not any(l for l in self.lines)
        cursor_style = DARK.accent

        # Visible window (bottom-anchored).
        cap = self.rows if self.rows > 0 else len(display)
        start = max(0, len(display) - cap - self.scroll)
        shown = display[start:start + cap]

        out: List[Line] = []
        for i, ln in enumerate(shown):
            is_cursor = self.focused and (start + i == self.cursor_line)
            if is_cursor:
                if empty and self.placeholder:
                    # Buffer is empty: draw the placeholder dimmed and put
                    # the cursor block over its first character (a focused,
                    # empty editor must still show *something* to type in).
                    segs = [Segment("█", cursor_style),
                            Segment(self.placeholder[1:], DARK.muted)]
                else:
                    col = min(self.cursor_col, len(ln))
                    segs = [Segment(ln[:col], text_style),
                            Segment(" ", cursor_style),
                            Segment(ln[col:], text_style)]
            elif empty and i == 0 and self.placeholder:
                segs = [Segment(self.placeholder, DARK.muted)]
            else:
                segs = [Segment(ln, text_style)]
            out.append(_fit_row(segs, width))
        return out
