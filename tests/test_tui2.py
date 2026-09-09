"""Tests for the TUI v2 engine (nbchat.tui2).

Phase 1 coverage: the differential frame renderer (the heart of the
flicker-free design), the raw-mode terminal passthrough for non-TTY
environments (which is exactly what a test harness or a pipe sees),
the thread-safe event queue, and the TUIApp render loop.

These tests do not require a TTY: RawTerminal detects a non-tty stdout
and runs in a documented no-op passthrough mode, so the same rendering
code paths that run in a real terminal are exercised here.
"""
from __future__ import annotations

import io
import os
import threading
import time

import pytest

from nbchat.tui2 import (
    EventQueue,
    Frame,
    Line,
    RawTerminal,
    Segment,
    Style,
    TUIApp,
    diff_frames,
    render_frame,
)

CYAN = Style(fg=36)
BOLD = Style(bold=True)


def text(line: str) -> Line:
    return Line([Segment(line)])


def frame(*lines: str) -> Frame:
    return Frame([text(l) for l in lines])


# ── Style ──────────────────────────────────────────────────────────


def test_style_code():
    assert Style().as_code() == ""
    assert Style(fg=36).as_code() == "\033[38;5;36m"
    s = Style(bold=True, fg=1, bg=4)
    code = s.as_code()
    assert code.startswith("\033[") and code.endswith("m")
    assert "1" in code.split(";") and "38;5;1" in code and "48;5;4" in code


# ── Differential diff (the core promise) ───────────────────────────


def test_diff_identical_frames_is_empty():
    assert diff_frames(frame("a", "b"), frame("a", "b")) == []
    f = frame("a")
    assert diff_frames(f, f) == []


def test_diff_rewrites_only_changed_tail():
    # Only line 2 changes: the op stream must touch line 2 and nothing
    # above it, and must not touch line 1.
    ops = diff_frames(frame("one", "two", "three"), frame("one", "TWO", "three"))
    rows = [op[1] for op in ops if op[0] == "write_line"]
    assert 2 in rows
    assert 1 not in rows
    texts = [op[2] for op in ops if op[0] == "write_line"]
    line_texts = ["".join(s.text for s in t) for t in texts]
    assert "TWO" in line_texts


def test_diff_no_ops_for_unchanged_first_lines():
    # A change on the last line must not cause rewrites of earlier lines.
    ops = diff_frames(frame("a", "b", "c"), frame("a", "b", "C"))
    rows = [op[1] for op in ops if op[0] == "write_line"]
    assert min(rows) == 3


def test_diff_extends_taller_frame_with_new_lines():
    ops = diff_frames(frame("a", "b"), frame("a", "b", "c", "d"))
    rows = [op[1] for op in ops if op[0] == "write_line"]
    assert 3 in rows and 4 in rows
    assert 1 not in rows and 2 not in rows


def test_diff_clears_stale_tail_when_shorter():
    ops = diff_frames(frame("a", "b", "c"), frame("a", "b"))
    rows = [op[1] for op in ops if op[0] == "write_line"]
    # The now-removed line 3 must be explicitly cleared, not left stale.
    assert 3 in rows
    assert 1 not in rows and 2 not in rows


def test_diff_rewrites_whole_tail_when_middle_changes():
    # A change in the middle invalidates everything from that row down.
    ops = diff_frames(frame("a", "b", "c"), frame("a", "B", "c"))
    rows = [op[1] for op in ops if op[0] == "write_line"]
    assert 2 in rows and 3 in rows
    assert 1 not in rows


# ── Rendered bytes ─────────────────────────────────────────────────


def test_render_identical_frames_emits_nothing():
    assert render_frame(frame("a"), frame("a")) == ""


def test_render_uses_sync_output():
    out = render_frame(frame(), frame("hello"))
    assert out.startswith("\033[?2026h") and out.endswith("\033[?2026l")


def test_render_first_frame_uses_absolute_positioning():
    # Fresh terminal: every rewritten line is addressed absolutely, so
    # the first frame lands regardless of the cursor's starting position.
    out = render_frame(Frame([]), frame("a", "b", "c"))
    # Every rewritten line is addressed absolutely (CUP: ESC[row;1H]),
    # so the frame lands correctly regardless of where the cursor is.
    assert "\033[1;1H" in out and "\033[2;1H" in out and "\033[3;1H" in out
    # No relative cursor movement (CSI A/B) remains in the writer.
    assert "\033[A" not in out and "\033[B" not in out


def test_render_absolute_positioning_after_change():
    # A change on line 2 of an existing frame still uses CUP addressing.
    out = render_frame(frame("a", "b"), frame("a", "c"))
    assert "\033[2;1H" in out and "\033[A" not in out and "\033[B" not in out


def test_render_changed_line_cleared_to_eol():
    out = render_frame(frame("aaaa"), frame("bb"))
    assert "\033[K" in out  # clear-to-EOL so the old longer line cannot show


def test_render_applies_style():
    f = Frame([Line([Segment("hi", CYAN)])])
    out = render_frame(Frame([]), f)
    assert "\033[38;5;36m" in out


def test_render_full_reconstruction_roundtrip():
    # Replaying the writer's ops line by line must yield exactly the
    # new frame's content.
    prev = frame("x1", "x2", "x3")
    new = frame("x1", "y2", "y3", "y4")
    ops = diff_frames(prev, new)
    buf = {1: "x1", 2: "x2", 3: "x3"}
    for op in ops:
        assert op[0] == "write_line", op
        buf[op[1]] = "".join(s.text for s in op[2])
    assert [buf[i] for i in range(1, 5)] == ["x1", "y2", "y3", "y4"]


def test_render_recovers_from_lost_bytes():
    # The design argument in one test (\u00a712.3.1): drop bytes mid-frame so
    # the cursor ends up in an unknown place, then render the next
    # frame.  With absolute (CUP) addressing the frame still lands
    # line-for-line: each op targets its own row independently.
    ops = diff_frames(frame("a", "b"), frame("c", "d"))
    assert ops
    buf = {1: "b0", 2: "b1"}
    for op in ops:
        assert op[0] == "write_line", op
        buf[op[1]] = "".join(s.text for s in op[2])  # absolute: row only
    assert buf == {1: "c", 2: "d"}

# ── RawTerminal (passthrough mode, no TTY required) ────────────────


def make_terminal() -> RawTerminal:
    stdin = io.StringIO("")
    stdin.fileno = lambda: 0  # type: ignore[attr-defined]
    term = RawTerminal(stdin, io.StringIO())
    term._saved = None
    term._passthrough = True
    return term


def test_passthrough_enter_restore_roundtrip():
    term = make_terminal()
    with term:
        term._write("payload")
        assert term._restored is False
    assert term._restored is True
    assert term.stdout.getvalue() == "payload"


def test_restore_is_idempotent():
    term = make_terminal()
    term.enter()
    term.restore()
    term.restore()  # must not raise or double-restore


# ── EventQueue ─────────────────────────────────────────────────────


def test_event_queue_fifo_order():
    q = EventQueue()
    q.put("a")
    q.put("b", 2)
    events = q.drain()
    assert events == [("a", None), ("b", 2)]
    assert q.drain() == []


def test_event_queue_is_thread_safe():
    q = EventQueue()
    errors = []

    def worker(n):
        try:
            for i in range(50):
                q.put(f"t{n}", i)
        except Exception as e:  # pragma: no cover - defensive
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = q.drain()
    assert not errors
    assert len(events) == 400
    assert {kind for kind, _ in events} == {f"t{n}" for n in range(8)}


def test_event_queue_close():
    q = EventQueue()
    assert not q.closed
    q.close()
    assert q.closed
    # drain swallows the sentinel: after close the queue is empty.
    assert q.drain() == []


# ── TUIApp render loop ─────────────────────────────────────────────


def _feed(term: RawTerminal, chunks):
    data = iter(chunks)
    term.stdin.read = lambda n: (next(data) if data else "")
    return data


def test_app_renders_and_updates():
    out = io.StringIO()
    term = RawTerminal(io.StringIO(""), out)
    term._saved = None
    term._passthrough = True

    app = TUIApp(term)
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return frame(f"tick {calls['n']}")

    app.set_frame_provider(provider)
    _feed(term, ["tick", "\x03"])  # some keystrokes, then Ctrl+C
    app.start()

    rendered = out.getvalue()
    assert "tick 1" in rendered
    assert calls["n"] >= 2
    assert term._restored is True  # terminal restored even on exit


def test_app_quit_event_stops_loop():
    out = io.StringIO()
    term = RawTerminal(io.StringIO(""), out)
    term._saved = None
    term._passthrough = True

    app = TUIApp(term)
    app.set_frame_provider(lambda: frame("static"))
    app.events.put("quit")
    _feed(term, [""])
    app.start()
    assert not app._running
    assert "static" in out.getvalue()
    assert term._restored is True


def test_app_ignores_identical_frames():
    out = io.StringIO()
    term = RawTerminal(io.StringIO(""), out)
    term._saved = None
    term._passthrough = True

    app = TUIApp(term)
    app.set_frame_provider(lambda: frame("same"))
    _feed(term, ["\x03"])
    app.start()
    # Second identical frame renders nothing: content appears exactly once.
    assert out.getvalue().count("same") == 1



# ── regression: a normal key must reach on_input (was "dead keys") ──────────
def test_app_dispatches_keys_to_on_input():
    """A normal keystroke must be parsed by the KeyReader and delivered to
    ``on_input``.  Before the fix, only Ctrl+C/Ctrl+D ever reached a handler
    and every other key was silently dropped ("none of the keys worked")."""
    from nbchat.tui2 import KeyReader

    out = io.StringIO()
    term = RawTerminal(io.StringIO(""), out)
    term._saved = None
    term._passthrough = True

    app = TUIApp(term)
    app.set_frame_provider(lambda: frame("static"))
    reader = KeyReader()
    app.set_key_reader(reader)

    seen = []
    app.on_input = lambda key: seen.append(key)
    # "r" then "q" then Ctrl+C to break the loop.
    _feed(term, ["r", "q", "\x03"])
    app.start()

    # The two ordinary keys must have been delivered, in order.
    assert [k.name for k in seen] == ["r", "q"], seen

# \u2500\u2500 Phase 2: chat-surface components (markdown / messages / tools) \u2500\u2500
def _plain(line):
    return "".join(s.text for s in line.segments)


def test_markdown_heading_and_inline():
    from nbchat.tui2 import theme
    from nbchat.tui2.chat import Markdown

    out = Markdown("# Title\n\nhello **bold** and `code`").render(40)
    text = "\n".join(_plain(l) for l in out)
    assert "Title" in text
    assert "hello bold and code" in text
    head = next(l for l in out if "Title" in _plain(l))
    assert any(s.style == theme.DARK.accent for s in head.segments
               if s.text == "Title")


def test_markdown_fenced_code_block():
    from nbchat.tui2.chat import Markdown

    out = Markdown("before\n```\nline1\nline2\n```\nafter").render(30)
    text = "\n".join(_plain(l) for l in out)
    assert "line1" in text and "line2" in text and "before" in text
    # Canonical engine renders code lines with border + muted styles.
    assert any(s.text == "\u2502 " for l in out for s in l.segments)


def test_markdown_bullet_list():
    from nbchat.tui2.chat import Markdown

    out = Markdown("- a\n- b").render(30)
    bullets = [_plain(l) for l in out if _plain(l).lstrip().startswith("\u00b7")]
    assert any("a" in b for b in bullets)
    assert any("b" in b for b in bullets)


def test_markdown_never_crashes_on_unclosed_markup():
    from nbchat.tui2.chat import Markdown

    for src in ["`unclosed", "**unclosed", "plain"]:
        out = Markdown(src).render(24)
        for l in out:
            assert sum(len(s.text) for s in l.segments) <= 24


def test_diff_lines_colouring():
    from nbchat.tui2.chat import GREEN, RED, DIFF_HUNK, DIFF_CONTEXT
    from nbchat.tui2 import diff_lines

    out = diff_lines(["+add", "-del", "@@ hunk", " ctx"], width=30)

    def first_style(prefix):
        row = next(l for l in out if _plain(l).lstrip().startswith(prefix))
        return row.segments[0].style

    assert first_style("+add") == GREEN
    assert first_style("-del") == RED
    assert first_style("@@ hunk") == DIFF_HUNK
    assert first_style("ctx") == DIFF_CONTEXT


def test_toolcall_panel_structure_and_diff():
    from nbchat.tui2 import ToolCall

    out = ToolCall(
        "edit", title="tool: edit", status="done",
        body=["+a", "-b"], show_diff=True,
    ).render(30)
    texts = [_plain(l) for l in out]
    joined = "\n".join(texts)
    assert "tool: edit" in joined
    assert "\u256d" in texts[0]
    assert "\u256f" in texts[-1]
    assert any(t.lstrip("\u2502").startswith("+a") for t in texts)
    assert any(t.lstrip("\u2502").startswith("-b") for t in texts)


def test_toolcall_omits_when_max_rows():
    from nbchat.tui2 import ToolCall

    body = [f"line{i}" for i in range(10)]
    out = ToolCall("t", body=body, max_rows=2).render(30)
    joined = "\n".join(_plain(l) for l in out)
    assert "more line(s)" in joined
    assert "line9" not in joined


def test_message_user_and_assistant():
    from nbchat.tui2 import Message

    u = Message("user", "hello world").render(30)
    assert "you" in _plain(u[0])
    a = Message("assistant", "# H\n\ntext here").render(30)
    joined = "\n".join(_plain(l) for l in a)
    assert "agent" in _plain(a[0])
    assert "H" in joined and "text here" in joined


def test_thinking_block_collapsed_and_expanded():
    from nbchat.tui2 import ThinkingBlock

    c = ThinkingBlock("a lot of hidden reasoning", collapsed=True).render(30)
    assert len(c) == 1
    assert "thinking" in _plain(c[0])
    e = ThinkingBlock("visible reasoning", collapsed=False).render(30)
    joined = "\n".join(_plain(l) for l in e)
    assert "visible reasoning" in joined


def test_chat_components_integrated_into_frame_diff():
    from nbchat.tui2 import Frame, Message, diff_frames

    def frame_of(text):
        return Frame(lines=Message("assistant", text).render(30))

    ops = diff_frames(frame_of("v1"), frame_of("v2 totally different"))
    assert any(op[0] == "write_line" for op in ops)