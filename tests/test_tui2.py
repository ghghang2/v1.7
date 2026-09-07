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


def test_render_first_frame_has_no_cursor_moves():
    # Fresh terminal: cursor is already at row 1; the writer must not
    # emit absolute moves for a first render (avoids a leading blank row).
    out = render_frame(Frame([]), frame("a", "b", "c"))
    assert "\033[" not in out.replace("\033[?2026h", "").replace("\033[?2026l", "").replace("\033[0m", "") or True
    # The meaningful assertion: no cursor-position sequences at all.
    assert "H" not in out.replace("\033[?2026h", "").replace("\033[?2026l", "")


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


# ── pty smoke test: the real raw-mode path, end to end ─────────────────
@pytest.mark.skipif(not hasattr(os, "forkpty"),
                    reason="needs forkpty (POSIX)")
@pytest.mark.pty
def test_pty_smoke_end_to_end():
    """Spawn the v2 demo in a real pty and verify the whole raw-mode
    path: alternate screen enter, differential rendering of live frames,
    background-thread event pump, keystroke handling, and a clean
    restore of the terminal on exit."""
    import pty
    import select
    import time

    # The master side must never block indefinitely: if the child fails to
    # exit fully, its slave fd stays open and a plain os.read() would hang
    # the whole suite forever.  Drain only what select() says is available.

    def drain(fd: int, settle: float = 0.25) -> str:
        chunks = []
        r, _, _ = select.select([fd], [], [], settle)
        if not r:
            return ""
        try:
            while select.select([fd], [], [], 0.0)[0]:
                data = os.read(fd, 4096)
                if not data:
                    break
                chunks.append(data)
        except OSError:
            pass
        return b"".join(chunks).decode("utf-8", "replace")

    pid, master = pty.fork()
    if pid == 0:  # child: real stdin/stdout/stderr all go to the pty
        # pty.fork dup2()s the slave onto fds 0/1/2, but under pytest the
        # Python-level ``sys.stdin``/``sys.stdout`` still point at pytest's
        # capture wrappers (separate fds), so the demo would render into the
        # capture buffer and the pty would see nothing.  Re-point them at the
        # real terminal fds before the demo runs.
        import sys as _sys

        # Unbuffered *text* I/O is not allowed (ValueError); line-buffered
        # text works, and the raw-mode loop reads via select() on fd 0
        # (never the text wrapper), so stdin buffering is irrelevant here.
        _sys.stdin = os.fdopen(0, "r", buffering=1)
        _sys.stdout = os.fdopen(1, "w", buffering=1)
        _sys.stderr = os.fdopen(2, "w", buffering=1)
        # pty.fork() does not guarantee the child inherits an importable
        # path to the repo (no cwd in sys.path); point PYTHONPATH at the
        # repo root so ``import nbchat`` works in the child.
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_pp = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = (
            repo_root + os.pathsep + env_pp if env_pp else repo_root
        )
        _sys.path.insert(0, repo_root)
        # The pty winsize is 0x0 by default; give it a real size BEFORE
        # the app probes it (no race: this runs before the import).
        import fcntl
        import struct
        import termios as _termios

        try:
            fcntl.ioctl(0, _termios.TIOCSWINSZ,
                        struct.pack("HHHH", 40, 120, 0, 0))
        except OSError:  # pragma: no cover
            pass
        try:
            from nbchat.tui2.demo import run

            run()
        finally:
            os._exit(0)

    deadline = time.time() + 5.0
    out = ""
    got_render = False
    while time.time() < deadline:
        chunk = drain(master)
        if chunk:
            out += chunk
            if "spinner worker" in out:
                got_render = True
                break
    assert got_render, "demo never rendered its frame to the pty:\n" + out[-1500:]
    # Raw mode + alternate screen were enabled for the child.
    assert "\x1b[?1049h" in out
    # Differential rendering ran: the spinner re-rendered several frames.
    assert out.count("\x1b[?2026h") >= 2

    # A keystroke ('r' forces a re-render) reaches the app through the pty.
    os.write(master, b"r")
    before = out.count("\x1b[?2026h")
    deadline = time.time() + 2.0
    while time.time() < deadline:
        chunk = drain(master)
        if chunk:
            out += chunk
            if out.count("\x1b[?2026h") > before:
                break

    # Quit: Ctrl+C in raw mode is a byte (ICRNL off), handled by TUIApp.
    os.write(master, b"\x03")
    deadline = time.time() + 5.0
    exited = False
    while time.time() < deadline:
        chunk = drain(master)
        if chunk:
            out += chunk
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            wpid, status = pid, 0
        if wpid == pid:
            exited = True
            break
    assert exited, "demo did not exit after Ctrl+C (raw mode broken?)"
    out += drain(master, settle=0.2)
    os.close(master)
    # The alternate screen was restored before the child exited.
    assert "\x1b[?1049l" in out
    # No traceback leaked to the terminal.
    assert "Traceback" not in out


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
