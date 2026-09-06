"""Frame model and the differential renderer (TUI v2, Phase 1).

The screen is modelled as a grid of lines.  A :class:`Frame` is a full
render of the component tree (width/height + one line per screen row).
:class:`Line` is a list of :class:`Segment` s; each segment carries a
:class:`Style`.  :func:`diff_frames` compares two frames and returns a
list of write operations that update only the lines that changed,
minimising cursor movement.  :func:`sync_out` wraps an update in CSI 2026
synchronized-output markers so the terminal applies it atomically
(flicker-free), matching the behaviour of prime-agent's pi-tui engine.

This module is pure (no I/O, no terminal access) so it is trivially
unit-testable; the raw-terminal layer (``nbchat.tui2.raw``) handles the
real terminal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ── Style ─────────────────────────────────────────────────────────────────
# Phase 1 keeps one style per segment with a deliberately small attribute
# set.  The theme layer (Phase 2) maps named theme colours onto these.

@dataclass(frozen=True)
class Style:
    """Immutable style attached to a run of text."""
    fg: Optional[int] = None      # 0-255 ANSI colour
    bg: Optional[int] = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False

    def as_code(self) -> str:
        """Return the SGR escape for this style, or ``''`` if empty."""
        if self is DEFAULT:
            return ""
        parts: List[str] = []
        if self.bold:
            parts.append("1")
        if self.dim:
            parts.append("2")
        if self.italic:
            parts.append("3")
        if self.underline:
            parts.append("4")
        if self.reverse:
            parts.append("7")
        if self.fg is not None:
            parts.append(f"38;5;{self.fg}")
        if self.bg is not None:
            parts.append(f"48;5;{self.bg}")
        if not parts:
            return ""
        return "\033[" + ";".join(parts) + "m"


DEFAULT = Style()


# ── Line / Frame ──────────────────────────────────────────────────────────

@dataclass
class Segment:
    text: str
    style: Style = DEFAULT


@dataclass
class Line:
    """One screen row.  ``text`` is the plain-content projection used by
    the diff; ``segments`` carries the styling used by the renderer."""
    segments: List[Segment] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.segments:
            self.segments = [Segment("")]

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.segments)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Line):
            return NotImplemented
        return self.text == other.text and self.segments == other.segments

    def __hash__(self) -> int:
        return hash((self.text, tuple((s.text, s.style) for s in self.segments)))


@dataclass
class Frame:
    """A complete screen render: ``height`` lines for a terminal of
    ``width`` columns (short lines are padded with spaces by the
    renderer, never by the frame itself)."""
    lines: List[Line]
    width: int = 0
    height: int = 0

    def __post_init__(self) -> None:
        if not self.width:
            self.width = max((len(l.text) for l in self.lines), default=0)
        self.height = len(self.lines)


# ── Differential diff ─────────────────────────────────────────────────────
# Each operation is a tuple the writer turns into bytes:
#   ("move", row, col)            cursor absolute move (1-based)
#   ("write_line", row, segments) rewrite line, cursor ends at (row, 1)
#
# ``segments`` is the new line's :class:`Segment` list: the writer
# emits each segment with its own SGR so styling survives the diff.
#
# Only changed lines are written.  The cursor is assumed to start at
# row 1, column 1 of the screen: the raw layer initialises it there and
# the writer parks it back at row 1 after every update.

Operation = Tuple[str, ...]


def diff_frames(prev: Frame, new: Frame) -> List[Operation]:
    """Compute the minimal line-level operations to turn ``prev`` into
    ``new`` (see module docstring for the strategy).

    * lines present in both frames that are equal are left untouched;
    * the first differing line is rewritten in place;
    * a taller new frame has its extra tail lines written below;
    * a shorter new frame has the cursor moved up and the tail cleared.
    """
    if prev is new:
        return []

    common = min(prev.height, new.height)

    first_diff = -1
    for i in range(common):
        if prev.lines[i] != new.lines[i]:
            first_diff = i
            break

    if first_diff == -1 and new.height == prev.height:
        # Identical content and equal heights: nothing to redraw.
        return []

    ops: List[Operation] = []

    if first_diff == -1:
        if new.height > prev.height:
            # Frames identical for their common part; new frame is
            # taller: append the extra tail lines.
            for row in range(prev.height + 1, new.height + 1):
                ops.append(("write_line", row, new.lines[row - 1].segments))
        else:
            # New frame is shorter: clear the removed tail lines so no
            # stale content is left on screen.
            for row in range(new.height + 1, prev.height + 1):
                ops.append(("write_line", row, []))
        return ops

    # Rewrite the differing line and everything below it in the new
    # frame (the tail of a changed line can shift content down).
    row = first_diff + 1
    while row <= new.height:
        ops.append(("write_line", row, new.lines[row - 1].segments))
        row += 1
    # Clear any stale lines from a taller previous frame.
    for row in range(new.height + 1, prev.height + 1):
        ops.append(("write_line", row, []))
    return ops


# ── Writer helpers ────────────────────────────────────────────────────────

_SGR_RESET = "\033[0m"
_CLEAR_TO_EOL = "\033[K"

_SYNC_BEGIN = "\033[?2026h"
_SYNC_END = "\033[?2026l"
_CPL = "\033[G"  # cursor to column 1 (no row change)


def _cpr(row: int, col: int) -> str:
    """Cursor Position Report (1-based row/col → 0-based CSI)."""
    return f"\033[{max(row - 1, 0)};{max(col - 1, 0)}H"


def render_frame(prev: Frame, new: Frame) -> str:
    """Render the diff between ``prev`` and ``new`` as an escape
    sequence string.  Returns ``""`` when the frames are identical.

    Cursor movement is relative only (CSI A/B plus a cursor-to-column
    mark), so the output never contains an absolute cursor-position
    report.  The contract: the cursor starts every update at row 1,
    column 1 (the raw layer parks it there on entry, and this writer
    parks it back at row 1 before finishing).  Each rewritten line is
    cleared to end-of-line before being written, so stale content from
    a longer previous line never shows through.  The whole update is
    wrapped in synchronized-output (CSI 2026) markers so the terminal
    applies it atomically \u2014 no flicker.
    """
    ops = diff_frames(prev, new)
    if not ops:
        return ""
    out: List[str] = [_SYNC_BEGIN]
    cur = 1  # cursor guaranteed at row 1 at the start of the update
    for op in ops:
        if op[0] == "move":
            row = op[1]
            if row > cur:
                out.append(f"\033[{row - cur}B")
            elif row < cur:
                out.append(f"\033[{cur - row}A")
            cur = row
        elif op[0] == "write_line":
            row = op[1]
            if row > cur:
                out.append(f"\033[{row - cur}B")
            elif row < cur:
                out.append(f"\033[{cur - row}A")
            cur = row
            out.append(_CPL + _CLEAR_TO_EOL + _SGR_RESET)
            for seg in op[2]:  # each segment writes its own SGR code
                if seg.text:
                    out.append(seg.style.as_code() + seg.text)
            out.append(_SGR_RESET)
    if cur > 1:
        out.append(f"\033[{cur - 1}A")  # park the cursor back at row 1
    out.append(_SYNC_END)
    return "".join(out)


def sync_out(update: str) -> str:
    """Wrap ``update`` in synchronized-output (CSI 2026) markers.

    The terminal buffers everything between the begin and end markers
    and applies it atomically — no mid-frame redraw, no flicker.
    Returns ``update`` unchanged when it is empty.
    """
    if not update:
        return ""
    return "\033[?2026h" + update + "\033[?2026l"
