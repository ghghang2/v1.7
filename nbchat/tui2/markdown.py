"""Lightweight markdown rendering for the TUI v2 (Phase 2).

Turns the model's markdown into styled :class:`~nbchat.tui2.frame.Line`
sequences using only the SGR subset the theme system already uses
(bold / dim + the 16 ANSI colours).  No external dependencies — the
engine must stay importable without rich (see
``docs/prime_tui_port_tracker.md``).

Supported blocks: `````` fenced code, headings ``#``-``######``, bullets
``-``/``*``/``+``, numbered ``1.`` items and blockquotes ``>``.
Inline: ``**bold**``, ``*italic*`` and `` `code` `` spans.  Everything
else passes through verbatim, so malformed markdown degrades to plain
text instead of failing.

All widths are counted in *visible* cells (ANSI codes carry no width),
which keeps wrapping consistent with the renderer.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .frame import DEFAULT, Line, Segment, Style
from .theme import Theme

# ── visible-width helpers ──────────────────────────────────────────────

def visible_width(line: Line) -> int:
    """Number of terminal cells this line occupies."""
    return sum(len(seg.text) for seg in line.segments)


def truncate_line(line: Line, width: int) -> Line:
    """Truncate to at most ``width`` visible cells (style-aware)."""
    out: List[Segment] = []
    remaining = max(width, 0)
    for seg in line.segments:
        if remaining <= 0:
            break
        if len(seg.text) <= remaining:
            out.append(seg)
            remaining -= len(seg.text)
        else:
            out.append(Segment(seg.text[:remaining], seg.style))
            break
    return Line(out)


def blank(width: int, style: Style | None = None) -> Line:
    return Line([Segment(" " * max(width, 0), style)])


# ── inline markup ──────────────────────────────────────────────────────

_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_EM_RE = re.compile(r"\*([^*\n]+)\*")
_UNESCAPE_RE = re.compile("\x00(\\d+)\x00")


def _inline(text: str, theme: Theme) -> List[Segment]:
    """Parse inline markup in *text* into styled segments.

    Code spans are stashed away (replaced with ``\\x00idx\\x00``
    placeholders) before emphasis is parsed, so ``**`` and ``*``
    inside a code span can never trigger styling.  Placeholders are
    restored at the end with the ``theme.tool`` style.
    """
    stash: List[str] = []

    def stash_seg(chunk: str) -> str:
        stash.append(chunk)
        return f"\x00{len(stash) - 1}\x00"

    text = _CODE_RE.sub(lambda m: stash_seg(m.group(1)), text)

    segs: List[Segment] = []
    pos = 0
    for m in _BOLD_RE.finditer(text):
        if m.start() > pos:
            segs.append(Segment(text[pos:m.start()]))
        segs.append(Segment(m.group(1), theme.accent))
        pos = m.end()
    if pos < len(text):
        segs.append(Segment(text[pos:]))

    styled: List[Segment] = []
    for seg in segs:
        if seg.style.as_code():
            styled.append(seg)
            continue
        p = 0
        for m in _EM_RE.finditer(seg.text):
            if m.start() > p:
                styled.append(Segment(seg.text[p:m.start()], seg.style))
            styled.append(Segment(m.group(1), theme.muted))
            p = m.end()
        if p < len(seg.text):
            styled.append(Segment(seg.text[p:], seg.style))

    out: List[Segment] = []
    for seg in styled:
        # A stash placeholder is contiguous in the source, so it can
        # never be split across segments — splitting per-segment is safe.
        parts = _UNESCAPE_RE.split(seg.text)
        # parts = [pre, id, mid, id, ...]
        for i, part in enumerate(parts):
            if i % 2 == 0:
                if part:
                    out.append(Segment(part, seg.style))
            else:
                out.append(Segment(stash[int(part)], theme.tool))
    return out


# ── wrapping ──────────────────────────────────────────────────────────

def wrap_segments(segs: List[Segment], width: int) -> List[Line]:
    """Word-wrap styled segments to at most ``width`` visible cells.

    Wraps at spaces only; each word keeps the style of the segment it
    came from, so mixed inline styling survives the wrap.  Words longer
    than the line are hard-broken.
    """
    if width <= 0:
        return []
    words: List[Tuple[str, Optional[Style]]] = []
    for seg in segs:
        if not seg.text:
            continue
        parts = seg.text.split(" ")
        for i, part in enumerate(parts):
            if part:
                words.append((part, seg.style))
            if i < len(parts) - 1:
                words.append(("", None))  # the separating space

    lines: List[List[Segment]] = [[]]
    col = 0
    for word, code in words:
        if not word:  # a space — dropped at end of line / before a wrap
            continue
        if col and col + 1 + len(word) > width:
            lines.append([])
            col = 0
        elif col:
            # the separating space, now inside the line — attach it to
            # the previous segment so it actually gets printed
            lines[-1][-1].text += " "
            col += 1
        while len(word) > width - col:  # hard-break an unbreakable word
            lines[-1].append(Segment(word[: width - col], code or DEFAULT))
            col = width
            lines.append([])
            word = word[len(lines[-2][-1].text):]
            col = 0
        lines[-1].append(Segment(word, code or DEFAULT))
        col += len(word)
    return [Line(s) for s in lines if s]


def wrap_with_prefix(prefix: Segment, content: List[Segment],
                     width: int) -> List[Line]:
    """Wrap *content* to ``width`` cells with every line prefixed by
    *prefix* (e.g. a bullet marker) — continuation lines align under
    the first word, not under the marker."""
    p = len(prefix.text)
    lines = wrap_segments(content, max(width - p, 1))
    return [Line([prefix] + list(ln.segments)) for ln in lines]


# ── document rendering ─────────────────────────────────────────────────

_FENCE_OPEN_RE = re.compile(r"^(\s*)(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUM_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")


def render_markdown(text: str, width: int, theme: Theme) -> List[Line]:
    """Render *text* (markdown) into ``Line`` objects of at most
    ``width`` visible cells.  Never raises on odd input."""
    width = max(width, 1)
    lines: List[Line] = []
    para: List[str] = []

    def flush_para() -> None:
        joined = " ".join(s.strip() for s in para if s.strip())
        if joined:
            lines.extend(wrap_segments(_inline(joined, theme), width))
        para.clear()

    code_lines: List[str] = []
    in_code = False
    fence = ""

    for raw in text.split("\n"):
        if in_code:
            if re.match(rf"^\s*{re.escape(fence)}\s*$", raw):
                in_code = False
                for cl in code_lines:
                    lines.append(Line([Segment("│ ", theme.border),
                                       Segment(cl.rstrip(), theme.muted)]))
                code_lines = []
            else:
                code_lines.append(raw.expandtabs(4))
            continue

        m = _FENCE_OPEN_RE.match(raw)
        if m:
            flush_para()
            in_code = True
            fence = m.group(2)
            continue

        m = _HEADING_RE.match(raw)
        if m:
            flush_para()
            title = m.group(2).strip()
            style = theme.accent if len(m.group(1)) <= 2 else theme.primary
            for ln in wrap_segments([Segment(title, style)], width):
                lines.append(ln)
            continue

        m = _BULLET_RE.match(raw)
        if m:
            flush_para()
            lines.extend(wrap_with_prefix(Segment("  · ", theme.status),
                                          _inline(m.group(1), theme), width))
            continue

        m = _NUM_RE.match(raw)
        if m:
            flush_para()
            prefix = f"{m.group(1)}. "
            lines.extend(wrap_with_prefix(Segment(prefix, theme.status),
                                          _inline(m.group(2), theme), width))
            continue

        m = _QUOTE_RE.match(raw)
        if m:
            flush_para()
            lines.extend(wrap_with_prefix(Segment("│ ", theme.border),
                                          _inline(m.group(1), theme), width))
            continue

        if not raw.strip():
            flush_para()
            continue
        para.append(raw)

    if in_code:  # unterminated fence — render what we have
        for cl in code_lines:
            lines.append(Line([Segment("│ ", theme.border),
                               Segment(cl.rstrip(), theme.muted)]))
    flush_para()
    return lines


def styled_lines(text: str, width: int, style: Style) -> List[Line]:
    """Wrap *text* with a uniform *style* (plain text, no markdown)."""
    if not text:
        return []
    return wrap_segments([Segment(text, style)], max(width, 1))


def style_for(role: str, theme: Theme) -> Style:
    """Role → base style for transcript bodies."""
    if role == "user":
        return theme.user
    if role == "assistant":
        return theme.assistant
    if role == "tool":
        return theme.tool
    if role == "error":
        return theme.error
    if role == "mail":
        return theme.accent
    return theme.primary
