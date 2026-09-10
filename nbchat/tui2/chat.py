"""Chat-surface components for the TUI v2 (Phase 2).

These render the actual conversation content that a streaming agent
produces: user/assistant message bubbles, tool-call panels (with
add/remove diff colouring) and collapsible thinking blocks.

Design follows the rest of :mod:`nbchat.tui2` \u2014 every component is a
pure function of its inputs that renders itself into a list of
:class:`~nbchat.tui2.frame.Line` objects for a given width.  Nothing
here touches the terminal; the differential renderer in
:mod:`nbchat.tui2.frame` turns the resulting lines into escape
sequences.

Markdown is *not* re-implemented here.  Assistant text is rendered by
the canonical, theme-aware engine in :mod:`nbchat.tui2.markdown`
(:func:`nbchat.tui2.markdown.render_markdown`), which supports headings,
inline ``code`` / ``**bold**`` / ``*italic*``, fenced code blocks, bullet
and numbered lists, and blockquotes.  :class:`Markdown` below is a thin
wrapper so callers have a stable object-oriented entry point.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from . import markdown
from .components import _clamp, _fit_row, blank
from .frame import Line, Segment, Style
from .theme import DARK

# \u2500\u2500 palette \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# Semantic colours for diffs and thinking.  Structural / role colours
# (accent, border, muted, status) come from the theme so that a future
# theme swap re-skins the whole chat surface.

USER_GLYPH = "\u276f"       # \u278f  (user)
ASSISTANT_GLYPH = "\u25b8"  # \u25b8  (assistant / agent)

GREEN = Style(fg=39)        # diff additions
RED = Style(fg=203)         # diff removals
DIFF_CONTEXT = Style(fg=244, dim=True)
DIFF_HUNK = Style(fg=245)   # @@ hunk header
THINKING = Style(fg=244, dim=True, italic=True)


# \u2500\u2500 markdown \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# Thin wrapper over the canonical markdown engine.  Kept as a class so
# callers read as ``Markdown(text).render(width)``.


class Markdown:
    """Render markdown text into ``Line`` objects via the shared engine."""

    def __init__(self, text: str) -> None:
        self.text = text

    def render(self, width: int) -> List[Line]:
        return markdown.render_markdown(self.text, width, DARK)


# \u2500\u2500 diff colouring \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# Colour a unified-diff-ish body: ``+`` lines green, ``-`` lines red,
# ``@@`` hunk headers dimmed, context plain.  Used by ToolCall.


def diff_lines(body: Sequence[str], width: int) -> List[Line]:
    out: List[Line] = []
    for raw in body:
        if not raw:
            out.append(blank(width))
            continue
        if raw.startswith("+"):
            style = GREEN
        elif raw.startswith("-"):
            style = RED
        elif raw.startswith("@@"):
            style = DIFF_HUNK
        elif raw.startswith(("diff ", "index ", "--- ", "+++ ")):
            style = DIFF_HUNK
        else:
            style = DIFF_CONTEXT
        out.append(_fit_row([Segment(_clamp(raw, width), style)], width))
    return out


# \u2500\u2500 messages \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
@dataclass
class Message:
    """A single user or assistant turn, rendered as a labelled block.

    ``role`` is ``"user"`` or ``"assistant"``.  Assistant text is run
    through the markdown engine; user text is wrapped verbatim in the
    accent colour.
    """
    role: str
    text: str

    def __post_init__(self) -> None:
        if self.role == "user":
            self._accent = DARK.accent
            self._label = USER_GLYPH + " you"
        elif self.role == "system":
            # Command output / notes: dimmed, no markdown.
            self._accent = DARK.muted
            self._label = "\u2139 note"
        else:
            self._accent = DARK.accent
            self._label = ASSISTANT_GLYPH + " agent"

    def render(self, width: int) -> List[Line]:
        out: List[Line] = []
        label = _clamp(self._label, width)
        out.append(_fit_row([Segment(label, self._accent)], width))
        indent = 2
        if self.role == "user":
            body = markdown.wrap_segments(
                [Segment(self.text, self._accent)], width - indent)
        elif self.role == "system":
            body = markdown.wrap_segments(
                [Segment(self.text, self._accent)], width - indent)
        else:
            body = Markdown(self.text).render(width - indent)
        indented: List[Line] = []
        for line in body:
            line.segments = [Segment(" " * indent, Style())] + list(line.segments)
            indented.append(line)
        out.extend(indented)
        return out


class ToolCall:
    """A bordered panel showing a tool invocation.

    ``title`` is e.g. ``"tool: read_file"``.  ``status`` is one of
    ``"running"``, ``"done"`` or ``"error"`` and colours the title.
    ``body`` is optional output; when ``show_diff`` is true the body is
    treated as a unified diff and coloured accordingly, otherwise it is
    rendered as plain (dimmed) lines.
    """

    _STATUS_STYLES = {
        "running": DARK.accent,
        "done": GREEN,
        "error": RED,
    }
    _STATUS_GLYPH = {
        "running": "\u2315",  # \u2328
        "done": "\u2713",    # \u2713
        "error": "\u2717",   # \u2717
    }

    def __init__(
        self,
        name: str,
        title: str = "",
        status: str = "running",
        body: Sequence[str] = (),
        show_diff: bool = False,
        max_rows: int = 0,
    ) -> None:
        self.name = name
        self.title = title or f"tool: {name}"
        self.status = status
        self.body = list(body)
        self.show_diff = show_diff
        self.max_rows = max_rows  # 0 = no cap

    def render(self, width: int) -> List[Line]:
        inner = max(width - 2, 0)
        status_style = self._STATUS_STYLES.get(self.status, DARK.accent)
        glyph = self._STATUS_GLYPH.get(self.status, "")
        title_txt = _clamp(f"{glyph} {self.title}", inner - 6) if inner > 6 else ""

        out: List[Line] = []
        # Top border with embedded title.
        top = [
            Segment("\u256d\u2500 ", DARK.border),
            Segment(title_txt, status_style),
            Segment("\u2500" * max(inner - 4 - len(title_txt), 0) + "\u256e",
                    DARK.border),
        ]
        out.append(_fit_row(top, width))

        body_lines: List[Line]
        if self.body:
            if self.show_diff:
                body_lines = diff_lines(self.body, inner)
            else:
                body_lines = [
                    _fit_row([Segment(_clamp(b, inner), DIFF_CONTEXT)], inner)
                    for b in self.body
                ]
            if self.max_rows and len(body_lines) > self.max_rows:
                omitted = len(body_lines) - self.max_rows
                body_lines = body_lines[:self.max_rows]
                body_lines.append(_fit_row(
                    [Segment(f"\u2026 {omitted} more line(s)", DIFF_HUNK)], inner))
        else:
            body_lines = [_fit_row(
                [Segment(" " + ("\u2026" if self.status == "running" else ""),
                         DIFF_CONTEXT)], inner)]

        for row in body_lines:
            segs = [Segment("\u2502", DARK.border)]
            segs.extend(row.segments[:inner])
            used = sum(len(s.text) for s in segs)
            if used < width:
                segs.append(Segment(" " * (width - used)))
            out.append(_fit_row(segs, width))

        out.append(_fit_row([Segment("\u2570" + "\u2500" * max(inner - 2, 0) +
                                     "\u256f", DARK.border)], width))
        return out


class ThinkingBlock:
    """A dimmed, italic "thinking" block.

    When ``collapsed`` the body is hidden behind a single summary line;
    otherwise the body is shown (dimmed) under a ``thinking`` label.
    """

    def __init__(
        self,
        text: str,
        collapsed: bool = False,
        title: str = "thinking",
        max_rows: int = 0,
    ) -> None:
        self.text = text
        self.collapsed = collapsed
        self.title = title
        self.max_rows = max_rows  # 0 = no cap

    def render(self, width: int) -> List[Line]:
        out: List[Line] = []
        if self.collapsed or not self.text:
            summary = _clamp(self.title, width)
            out.append(_fit_row([Segment(f"\u00b7 {summary}\u2026", THINKING)],
                                width))
            return out

        out.append(_fit_row([Segment(f"\u00b7 {self.title}", THINKING)], width))
        body = markdown.wrap_segments(
            [Segment(self.text, THINKING)], max(width - 2, 1))
        indented: List[Line] = []
        for line in body:
            line.segments = [Segment("  ", Style())] + list(line.segments)
            indented.append(line)
        if self.max_rows and len(indented) > self.max_rows:
            omitted = len(indented) - self.max_rows
            indented = indented[:self.max_rows]
            indented.append(_fit_row(
                [Segment(f"  \u2026 {omitted} more line(s)", DIFF_HUNK)], width))
        out.extend(indented)
        return out


# ── structured chat log ─────────────────────────────────────────────────


@dataclass
class ChatBlock:
    """One piece of an assistant message: tool output or thinking text.

    ``kind`` is ``"tool"`` or ``"thinking"``.  Tool blocks carry the
    tool *name*, a *title*, a *status* (``"running"`` / ``"done"`` /
    ``"error"``), an optional *body* and a *diff* flag.  Thinking
    blocks carry dimmed *text*.
    """
    kind: str
    name: str = ""
    title: str = ""
    status: str = "running"
    body: List[str] = field(default_factory=list)
    diff: bool = False
    text: str = ""


@dataclass
class ChatMessage:
    """A conversation turn in the structured log.

    Either ``text`` (rendered through the markdown engine) or
    ``blocks`` (a list of :class:`ChatBlock` for tool panels and
    thinking sections).  ``.render(width)`` is cached per width so
    streaming re-renders of unchanged messages stay cheap.
    """
    role: str
    text: str = ""
    blocks: Optional[List[ChatBlock]] = None
    _cache: "dict" = field(default_factory=dict, repr=False, compare=False)

    def invalidate(self) -> None:
        self._cache.clear()

    def render(self, width: int) -> List[Line]:
        key = (self.role, self.text, tuple(
            (b.kind, b.name, b.status, b.title, b.text, tuple(b.body))
            for b in (self.blocks or [])))
        hit = self._cache.get(width)
        if hit is not None and hit[0] == key:
            return hit[1]
        out: List[Line] = []
        if self.blocks:
            for b in self.blocks:
                if b.kind == "tool":
                    out.extend(ToolCall(
                        b.name, title=b.title, status=b.status,
                        body=b.body, show_diff=b.diff,
                    ).render(width))
                elif b.kind == "thinking":
                    out.extend(ThinkingBlock(
                        b.text, collapsed=False,
                        title=b.title or "thinking",
                    ).render(width))
        if self.text:
            # The final answer text renders *below* the thinking/tool
            # blocks, never instead of them: a turn with tool calls must
            # still show its reply.
            out.extend(Message(self.role, self.text).render(width))
        if len(self._cache) > 4:
            self._cache.clear()
        self._cache[width] = (key, out)
        return out


class ChatLog:
    """The scrolling conversation log.

    Holds messages in order and renders the tail that fits.
    ``rows`` caps the height (0 = grow to content); scrolling is
    bottom-anchored — :meth:`scroll` adjusts ``offset`` (lines up from
    the bottom).  *gutter* blank lines separate messages.
    """

    def __init__(self, rows: int = 0, gutter: int = 1) -> None:
        self.messages: List[ChatMessage] = []
        self.rows = rows
        self.gutter = gutter
        self.offset = 0

    def add(self, msg: "ChatMessage") -> None:
        self.messages.append(msg)

    def update_last(self, msg: "ChatMessage") -> None:
        """Replace the most recent message (streaming update)."""
        if self.messages:
            self.messages[-1] = msg
        else:
            self.messages.append(msg)

    def _all_rows(self, width: int) -> List[Line]:
        out: List[Line] = []
        for i, msg in enumerate(self.messages):
            if i:
                out.extend(blank(width) for _ in range(self.gutter))
            out.extend(msg.render(width))
        return out

    def render(self, width: int) -> List[Line]:
        rows = self._all_rows(width)
        cap = self.rows if self.rows > 0 else len(rows)
        start = max(0, len(rows) - cap - self.offset)
        return rows[start:start + cap]

    def scroll(self, delta: int) -> None:
        """Move the view up (*delta* > 0) or down (negative)."""
        total = len(self._all_rows(1))
        cap = self.rows if self.rows > 0 else total
        self.offset = max(0, min(self.offset + delta, max(0, total - cap)))

    def reset(self) -> None:
        self.messages = []
        self.offset = 0
