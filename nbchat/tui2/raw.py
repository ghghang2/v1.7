"""Raw-mode terminal handling and the render/event loop (TUI v2).

:class:`RawTerminal` puts the terminal into raw mode (no line
buffering, no signal generation by Ctrl+C so the UI can handle it),
switches to the alternate screen buffer, and enables bracketed paste.
It restores everything on exit — the context-manager form is the
supported way to use it::

    with RawTerminal(sys.stdin, sys.stdout) as term:
        app = TUIApp(term)
        app.start()          # runs the event loop until quit

:class:`EventQueue` is a thread-safe bridge: worker threads (the LLM
client, ``/team`` supervisor, voice) push events and the render loop
drains them.  Events are tuples ``(kind, payload)`` where ``kind`` is
one of ``"render"``, ``"quit"`` or a custom string.

:class:`TUIApp` is the minimal application skeleton used in Phase 1:
it owns the current frame, runs the loop (read input → drain events →
render diff → repeat), and guarantees terminal restoration.
"""
from __future__ import annotations

import os
import queue
import sys
import termios
import tty
from typing import IO, List, Optional, Tuple

from .frame import Frame, Line, Segment, Style, diff_frames, render_frame, sync_out

# ── Raw terminal ──────────────────────────────────────────────────────────

_ENTER_ALT_SCREEN = "\033[?1049h"   # alt screen + save cursor
_LEAVE_ALT_SCREEN = "\033[?1049l"   # restore cursor + main screen
_SHOW_CURSOR = "\033[?25h"
_HIDE_CURSOR = "\033[?25l"
_BRACKETED_PASTE_ON = "\033[?2004h"
_BRACKETED_PASTE_OFF = "\033[?2004l"


class RawTerminal:
    """Context manager for raw mode + alternate screen buffer.

    ``restore`` is idempotent and safe to call multiple times; the
    ``finally`` in :meth:`__exit__` ensures the user is never left
    without a prompt.
    """

    def __init__(self, stdin: IO, stdout: IO) -> None:
        self.stdin = stdin
        self.stdout = stdout
        try:
            self.fd = stdin.fileno()
        except Exception:  # pragma: no cover - non-file streams (tests)
            self.fd = -1
        self._saved: Optional[termios.Termios] = None
        self._restored = False
        self._passthrough = False
        self.width, self.height = self._probe_size()

    # -- lifecycle ----------------------------------------------------
    def __enter__(self) -> "RawTerminal":
        self.enter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.restore()

    def enter(self) -> None:
        if self._saved is not None:
            return
        if (
            os.environ.get("NBCHAT_TUI_FORCE") or self.stdout.isatty()
        ) and self.fd >= 0:
            self._saved = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
            self._write(
                _ENTER_ALT_SCREEN + _HIDE_CURSOR + _BRACKETED_PASTE_ON
            )
            # Park the cursor at row 1, col 1 \u2014 the differential
            # writer assumes every update starts there.
            self._write("\033[2J")
            self._write("\033[0;0H")
        else:
            # Not a TTY (tests, pipes): raw mode is unavailable, so run
            # in a no-op passthrough mode — rendering still works
            # against a fake stdout and restore() is a no-op.
            self._saved = None
            self._passthrough = True

    def restore(self) -> None:
        if self._restored:
            return
        self._restored = True
        if getattr(self, "_passthrough", False):
            return
        if self._saved is not None:
            out = (
                _BRACKETED_PASTE_OFF
                + _SHOW_CURSOR
                + _LEAVE_ALT_SCREEN
            )
            self._write(out)
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
        self._saved = None

    # -- helpers -------------------------------------------------------
    def _write(self, data: str) -> None:
        self.stdout.write(data)
        self.stdout.flush()

    def _probe_size(self) -> Tuple[int, int]:
        if self.fd < 0:
            return (80, 24)
        try:
            import fcntl
            import struct

            buf = fcntl.ioctl(self.fd, termios.TIOCGWINSZ, b"\0" * 8)
            height, width = struct.unpack("hh", buf[:4])
            if width > 0 and height > 0:
                return width, height
        except OSError:
            pass
        return (80, 24)

    def resize(self) -> bool:
        """Re-probe the terminal size; returns True when it changed."""
        w, h = self._probe_size()
        if (w, h) == (self.width, self.height):
            return False
        self.width, self.height = w, h
        return True


# ── Event queue ───────────────────────────────────────────────────────────

Event = Tuple[str, object]


class EventQueue:
    """Thread-safe event bridge between worker threads and the render
    loop.  Use :meth:`put` from any thread."""

    def __init__(self) -> None:
        self._q: "queue.Queue[Optional[Event]]" = queue.Queue()
        self._closed_flag = False

    def put(self, kind: str, payload: object = None) -> None:
        self._q.put((kind, payload))

    def drain(self) -> List[Event]:
        """Pop all currently-queued events (non-blocking)."""
        out: List[Event] = []
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                return out
            if item is None:
                self._q.put(None)  # keep the sentinel so ``closed`` stays true
                return out
            out.append(item)

    def close(self) -> None:
        self._closed_flag = True
        self._q.put(None)

    @property
    def closed(self) -> bool:
        # The sentinel stays in the queue for drain() to consume; the
        # flag makes closed a cheap, race-free check.
        return self._closed_flag


# ── Application skeleton ──────────────────────────────────────────────────

class TUIApp:
    """Minimal TUI application: frame + event loop + clean exit.

    Subclasses (or callers) set :attr:`build_frame` to a callable that
    produces the next :class:`Frame` from the current state.  The loop:

    1. reads one chunk of keyboard input (blocking with a short
       timeout via :meth:`select`);
    2. drains the event queue (``"render"`` forces a re-render,
       ``"quit"`` exits);
    3. renders the diff of the new frame.
    """

    def __init__(self, terminal: RawTerminal, events: Optional[EventQueue] = None) -> None:
        self.term = terminal
        self.events = events or EventQueue()
        self.frame: Optional[Frame] = None
        self._build_frame: object = None
        self._running = False

    def set_frame_provider(self, fn) -> None:
        self._build_frame = fn

    def start(self) -> None:
        self._running = True
        try:
            self._render_first()
            import select

            while self._running:
                try:
                    r, _, _ = select.select([self.term.stdin], [], [], 0.1)
                except (OSError, ValueError):
                    # Non-selectable stream (tests, pipes): read directly.
                    r = True
                if r:
                    data = self.term.stdin.read(64)
                    if not data:
                        break
                    if self._handle_input(data):
                        break
                    # Keystrokes (or paste) may change UI state: force a
                    # rebuild and diff of the screen.
                    self._render_first(force=True)
                for kind, payload in self.events.drain():
                    if kind == "quit":
                        self.stop()
                        break
                    if kind == "render":
                        self._render_first(force=True)
        finally:
            self._running = False
            self.term.restore()

    def stop(self) -> None:
        self._running = False

    def _handle_input(self, data: str) -> bool:
        """Return True when the input requests an exit."""
        if data in ("\x03", "\x04"):  # Ctrl+C / Ctrl+D
            return True
        return False

    def _render_first(self, force: bool = False) -> None:
        if self._build_frame is None:
            return
        new_frame: Frame = self._build_frame()
        if self.frame is not None and new_frame is self.frame and not force:
            return
        if self.frame is not None and new_frame == self.frame:
            return
        update = render_frame(self.frame or Frame([]), new_frame)
        if update:
            self.term._write(sync_out(update))
        self.frame = new_frame

    def quit_event(self) -> None:
        """Convenience for worker threads: request an exit."""
        self.events.put("quit")
