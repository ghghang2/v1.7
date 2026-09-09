"""UI components for the TUI v2 (Phase 1).

A minimal, pure-Python port of the concepts in prime-agent's
``packages/tui`` component layer: components know nothing about the
terminal — each one simply renders itself into a list of
:class:`~nbchat.tui2.frame.Line` objects for a given width.  The
differential renderer then turns the resulting :class:`Frame` into
minimal escape sequences.

Included in Phase 1:

- :class:`Text` — one (or word-wrapped) styled line(s), clipped to width.
- :class:`Box` — bordered box with title, padding, child lines.
- :class:`Spacer` — N blank lines.
- :class:`Container` — vertical stack of children.
- :class:`StatusLine` — full-width status line (left text + ``|`` right).
- :class:`Loader` — animated spinner (agent working state).
- :func:`centered` / :func:`pad_row` — layout helpers.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .frame import Line, Segment, Style
from .theme import DARK, Theme

_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

_PLAIN = Style("")


# ── small helpers ──────────────────────────────────────────────────


def _clamp(text: str, width: int) -> str:
    """Truncate to *width* columns (simple codepoint counting — the
    spinner glyphs used here are single-width in practice; full
    East-Asian width handling is a Phase 2 refinement)."""
    if width <= 0:
        return ""
    return text[:width]


def _fit_row(segments: List[Segment], width: int) -> Line:
    """Trim or pad a list of segments so the row occupies exactly
    *width* columns.  Trailing spaces are plain (no SGR cost)."""
    used = 0
    out: List[Segment] = []
    for seg in segments:
        if used >= width:
            break
        take = seg.text[: width - used]
        if take:
            out.append(Segment(take, seg.style))
            used += len(take)
    if used < width:
        out.append(Segment(" " * (width - used)))
    return Line(out)


def pad_row(segments: List[Segment], width: int) -> Line:
    """Public wrapper of :func:`_fit_row` (trims to width, pads with
    spaces).  Used by components and by apps that build frames directly."""
    return _fit_row(list(segments), width)


def centered(text: str, width: int, style: Optional[Style] = None) -> Line:
    """A single line of *text* centred within *width* columns."""
    if width <= 0:
        return Line([Segment("")])
    text = _clamp(text, width)
    left = max((width - len(text)) // 2, 0)
    right = width - left - len(text)
    segs: List[Segment] = []
    if left:
        segs.append(Segment(" " * left))
    if text:
        segs.append(Segment(text, style or _PLAIN))
    if right:
        segs.append(Segment(" " * right))
    return Line(segs)


def blank(width: int) -> Line:
    return Line([Segment(" " * max(width, 0))])


# ── components ─────────────────────────────────────────────────────


class Text:
    """Styled text, word-wrapped and clipped to the available width."""

    def __init__(
        self,
        text: str,
        style: Optional[Style] = None,
        width: int = 0,
    ) -> None:
        self.text = text
        self.style = style or _PLAIN
        self.width = width  # 0 = wrap at the width given at render time

    def wrap(self, text: str, width: int) -> List[str]:
        if width <= 0:
            return [text]
        rows: List[str] = []
        for raw in text.split("\n"):
            words = raw.split(" ")
            cur = ""
            for word in words:
                while len(word) > width:  # force-split over-long words
                    if cur:
                        rows.append(cur)
                        cur = ""
                    rows.append(word[:width])
                    word = word[width:]
                if not word:
                    continue
                if not cur:
                    cur = word
                elif len(cur) + 1 + len(word) <= width:
                    cur = cur + " " + word
                else:
                    rows.append(cur)
                    cur = word
            rows.append(cur)
        return rows

    def render(self, width: int) -> List[Line]:
        w = self.width if self.width > 0 else width
        return [
            _fit_row([Segment(row, self.style)], w)
            for row in self.wrap(self.text, w)
        ]


class Box:
    """Bordered box with a title and optional inner padding.

    Child *lines* may be supplied directly; when omitted the box is
    sized ``rows + 2`` (top + bottom border) by ``cols + 2``.
    """

    def __init__(
        self,
        title: str = "",
        border_style: Optional[Style] = None,
        title_style: Optional[Style] = None,
        lines: Optional[List[Line]] = None,
        cols: int = 0,
        rows: int = 0,
        padding: int = 1,
        clip: bool = False,
    ) -> None:
        self.title = title
        self.border_style = border_style or DARK.border
        self.title_style = title_style or DARK.accent
        self.lines = lines
        self.cols = cols
        self.rows = rows
        self.padding = max(int(padding), 0)
        self.clip = clip

    # -- border drawing -------------------------------------------------
    def _inner_width(self, width: int) -> int:
        return max(width - 2, 0)

    def _top_line(self, inner_w: int) -> Line:
        title_txt = _clamp(self.title, inner_w - 4) if inner_w > 4 else ""
        if title_txt:
            segs = [
                Segment("╭─ ", self.border_style),
                Segment(title_txt, self.title_style),
                Segment(
                    "─" * max(inner_w - 4 - len(title_txt), 0) + "╮",
                    self.border_style,
                ),
            ]
        else:
            segs = [Segment("╭" + "─" * max(inner_w - 2, 0) + "╮",
                            self.border_style)]
        return Line(segs)

    def render(self, width: int) -> List[Line]:
        inner_w = self._inner_width(width)
        if self.lines is None:
            content: List[Line] = []
            rows = max(self.rows - 2, 0)
            if self.title:
                content.append(centered(self.title, inner_w, self.title_style))
            content.extend(blank(inner_w) for _ in range(max(rows, 0)))
        else:
            content = list(self.lines)
            if self.clip and self.rows > 2:
                content = content[: self.rows - 2]
            elif not self.clip:
                pass  # grow the box to fit its content
        # Uniform inner row width (trailing spaces are plain).
        content = [_fit_row(row.segments, inner_w) for row in content]

        out: List[Line] = [self._top_line(inner_w)]
        for row in content:
            segs = [Segment("│", self.border_style)]
            segs.extend(row.segments)
            segs.append(Segment("│", self.border_style))
            out.append(_fit_row(segs, width))
        out.append(Line([Segment("╰" + "─" * max(inner_w - 2, 0) + "╯",
                                 self.border_style)]))
        return out


class Spacer:
    """N blank lines (or one when ``n <= 0``)."""

    def __init__(self, n: int = 1) -> None:
        self.n = max(int(n), 0)

    def render(self, width: int) -> List[Line]:
        return [blank(width) for _ in range(self.n)]


class Container:
    """Vertical stack of child components (or pre-rendered lines)."""

    def __init__(self, children: Sequence[object] = ()) -> None:
        self.children: List[object] = list(children)

    def render(self, width: int) -> List[Line]:
        out: List[Line] = []
        for child in self.children:
            if hasattr(child, "render"):
                out.extend(child.render(width))
            elif isinstance(child, Line):
                out.append(_fit_row(child.segments, width))
        return out


class StatusLine:
    """Full-width status line: ``left`` on the left, optional ``right``
    pushed to the right edge (separated by ``|`` when both are set)."""

    def __init__(
        self,
        left: str,
        right: str = "",
        left_style: Optional[Style] = None,
        right_style: Optional[Style] = None,
        width: int = 0,
    ) -> None:
        self.left = left
        self.right = right
        self.left_style = left_style or DARK.status
        self.right_style = right_style
        self.width = width

    def render(self, width: int) -> List[Line]:
        w = self.width if self.width > 0 else width
        if self.right:
            left = _clamp(self.left, w - len(self.right) - 3)
            right = _clamp(self.right, w - len(left) - 3)
            gap = max(w - len(left) - len(right) - 1, 1)
            segs = [
                Segment(self.left, self.left_style),
                Segment(" " * (gap - 1)),
                Segment("|", DARK.border),
                Segment(right, self.right_style or self.left_style),
            ]
        else:
            segs = [Segment(_clamp(self.left, w), self.left_style)]
        return [_fit_row(segs, w)]


class Loader:
    """Animated spinner for the "agent is working" state.

    Call :meth:`step` with an increasing tick (typically wall-clock
    seconds) and re-render; the glyph advances through the Braille
    spinner frames at one step per tick.
    """

    def __init__(
        self,
        message: str = "thinking…",
        style: Optional[Style] = None,
        active: bool = True,
    ) -> None:
        self.message = message
        self.style = style or DARK.accent
        self.active = active
        self._tick = -1
        self._frame_index = 0

    def step(self, tick: int) -> "Loader":
        """Advance the animation up to *tick* (monotonically increasing)."""
        if self._tick < 0:
            delta = 0
        else:
            delta = max(tick - self._tick, 0)
        self._tick = tick
        self._frame_index = (self._frame_index + delta) % len(_SPINNER_FRAMES)
        return self

    def glyph(self) -> str:
        return _SPINNER_FRAMES[self._frame_index] if self.active else "·"

    def render(self, width: int) -> List[Line]:
        text = (
            f" {self.glyph()} {self.message} " if self.message
            else f" {self.glyph()} "
        )
        return [centered(text, width, self.style)]


# ── Select list ────────────────────────────────────────────────────────────
# A modal selector panel: a title row, a list of selectable items with a
# marker on the selected one, and a footer hint.  Used by the /sessions and
# /history commands; filtering is fuzzy (the caller passes pre-ranked items,
# see :func:`nbchat.tui2.fuzzy.fuzzy_rank`).


class SelectList:
    """A bordered list with one highlighted row (the selector modal)."""

    def __init__(
        self,
        title: str = "",
        items: Optional[List[str]] = None,
        selected: int = 0,
        hint: str = "",
        footer: str = "",
    ) -> None:
        self.title = title
        self.items: List[str] = list(items or [])
        self.selected = selected
        self.selected = max(0, min(self.selected, max(len(self.items) - 1, 0)))
        self.hint = hint
        self.footer = footer

    @property
    def selected_item(self) -> Optional[str]:
        if self.items:
            return self.items[self.selected]
        return None

    def select(self, index: int) -> None:
        if 0 <= index < len(self.items):
            self.selected = index

    def render(self, width: int) -> List[Line]:
        inner_w = max(width - 2, 0)
        out: List[Line] = []
        # Top border with the modal title.
        title_txt = _clamp(self.title, inner_w - 4) if inner_w > 4 else ""
        if title_txt:
            out.append(Line([
                Segment("\u256d\u2500 ", DARK.border),
                Segment(title_txt, DARK.accent),
                Segment("\u2500" * max(inner_w - 4 - len(title_txt), 0) + "\u256e",
                        DARK.border),
            ]))
        else:
            out.append(Line([Segment(
                "\u256d" + "\u2500" * max(inner_w - 2, 0) + "\u256e",
                DARK.border)]))
        # Body rows.
        if self.items:
            for i, item in enumerate(self.items):
                is_sel = i == self.selected
                if is_sel:
                    marker, style = "\u25b8", DARK.accent
                    text = _clamp(f" {item}", inner_w - 2)
                else:
                    marker, style = " ", DARK.muted
                    text = _clamp(f" {item}", inner_w - 2)
                segs = [Segment("\u2502", DARK.border),
                        Segment(marker, style),
                        Segment(text, style)]
                out.append(_fit_row(segs, width))
        else:
            empty = _clamp(self.hint or "(empty)", inner_w)
            out.append(_fit_row(
                [Segment("\u2502", DARK.border),
                 Segment(" " + empty, DARK.muted)], width))
        if self.footer:
            foot = _clamp(self.footer, inner_w)
            out.append(_fit_row(
                [Segment("\u2502", DARK.border),
                 Segment(" " + foot, DARK.muted)], width))
        out.append(Line([Segment(
            "\u2570" + "\u2500" * max(inner_w - 2, 0) + "\u256f",
            DARK.border)]))
        return out
