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
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .keys import KeyReader

from .frame import Frame, Line, Segment, Style, diff_frames, render_frame

# ── Raw terminal ──────────────────────────────────────────────────────────

_ENTER_ALT_SCREEN = "\033[?1049h"   # alt screen + save cursor
_LEAVE_ALT_SCREEN = "\033[?1049l"   # restore cursor + main screen
_SHOW_CURSOR = "\033[?25h"
_HIDE_CURSOR = "\033[?25l"
_BRACKETED_PASTE_ON = "\033[?2004h"
_BRACKETED_PASTE_OFF = "\033[?2004l"
# SGR-extended mouse reporting (wheel + button press/release).  Opt out
# with NBCHAT_NO_MOUSE=1 (e.g. over some SSH/serial links that mangle it).
_MOUSE_SGR_ON = "\033[?1000h\033[?1006h"
_MOUSE_SGR_OFF = "\033[?1000l\033[?1006l"


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
            if not os.environ.get("NBCHAT_NO_MOUSE"):
                self._write(_MOUSE_SGR_ON)
            # Clear the screen and hide the cursor.  The differential
            # writer addresses every line absolutely (CUP), so the
            # cursor's initial position no longer matters; the clear
            # just prevents stale content on the first frame.
            self._write("\033[2J")
            self._write("\033[H")
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
                _MOUSE_SGR_OFF
                + _BRACKETED_PASTE_OFF
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

    def __init__(self, terminal: RawTerminal, events: Optional[EventQueue] = None,
                 bg: bool = False) -> None:
        self.term = terminal
        self.events = events or EventQueue()
        # Background / headless mode (tui3 wave 6): a stdin EOF does NOT
        # end the loop.  The app keeps running on its heartbeat and is
        # driven / stopped through the external control socket.
        self._bg = bool(bg)
        self.frame: Optional[Frame] = None
        self._build_frame: object = None
        self._running = False
        self._key_reader: Optional["KeyReader"] = None
        self.on_input = None  # type: ignore[assignment]
        # >0: force a frame rebuild roughly every *clock_interval* seconds
        # even with no events, so animation (spinners) advances while the
        # model is thinking between streamed tokens.  0 disables it (the
        # Phase 1 demo drives its own animation via render events).
        self.clock_interval: float = 0.0

    def set_frame_provider(self, fn) -> None:
        self._build_frame = fn

    def set_key_reader(self, reader: "KeyReader") -> None:
        """Attach a :class:`~nbchat.tui2.keys.KeyReader`; when set, each
        completed key is passed to :attr:`on_input` (``fn(key)``)."""
        self._key_reader = reader

    def handle_input_events(self, data: str) -> None:
        """Parse *data* into keys and dispatch each to :attr:`on_input`."""
        if self._key_reader is None or self.on_input is None:
            return
        for key in self._key_reader.feed(data):
            self.on_input(key)

    def start(self) -> None:
        self._running = True
        try:
            import time as _time

            self._render_first()
            import select

            _clock_due = 0.0
            while self._running:
                try:
                    r, _, _ = select.select([self.term.stdin], [], [], 0.1)
                except (OSError, ValueError):
                    # Non-selectable stream (tests, pipes): read directly.
                    r = True
                if self.clock_interval > 0 and _time.monotonic() >= _clock_due:
                    # Heartbeat tick: advance spinners even with no input or
                    # events pending.
                    _clock_due = _time.monotonic() + self.clock_interval
                    self._render_first(force=True)
                if r:
                    data = self._read_input()
                    if not data:
                        if not self._bg:
                            break
                        # Headless / background: stdin is /dev/null or a
                        # closed pipe (always "ready", always EOF).  Do NOT
                        # quit; sleep briefly to avoid a busy spin, then fall
                        # through so queued events (e.g. a control-socket
                        # "quit") are still drained below.
                        _time.sleep(0.05)
                    else:
                        action = self._handle_input(data)
                        if action is True:
                            break
                        # A ``None`` result means the app consumed the chunk
                        # itself (e.g. an interrupt or a Ctrl+D submit) —
                        # refresh the screen but do NOT re-dispatch the raw
                        # bytes as keys.  ``False`` is the classic contract:
                        # dispatch every key in the chunk to the app handler
                        # (KeyReader feed).
                        if action is False:
                            self.handle_input_events(data)
                        self._render_first(force=True)
                # Coalesce: collapse N "render" events drained in this
                # tick into a single rebuild (streaming tokens fire
                # constantly; one rebuild per tick bounds the rate).
                quit_requested = False
                render_requested = False
                for kind, payload in self.events.drain():
                    if kind == "quit":
                        quit_requested = True
                    elif kind == "render":
                        render_requested = True
                    elif kind == "call" and callable(payload):
                        # A closure to run on the UI thread (e.g. an
                        # external control-socket command).  Never let a
                        # bad closure take the render loop down.
                        try:
                            payload()
                        except Exception:
                            pass
                if quit_requested:
                    self.stop()
                    break
                if render_requested:
                    self._render_first(force=True)
        finally:
            self._running = False
            self.term.restore()

    def _read_input(self) -> str:
        """Read one chunk of available keyboard input.

        Uses ``os.read`` on the file descriptor: ``select()`` has just
        said data is ready, so a raw fd read returns exactly the bytes
        that are available and never blocks.  A text-stream ``read(n)``
        would instead block until *n* bytes accumulate — under a real
        terminal a lone keystroke (e.g. Ctrl+C) would therefore sit
        unread until 64 more bytes arrived.
        """
        fd = self.term.fd
        if fd is not None and fd >= 0:
            try:
                raw = os.read(fd, 4096)
            except OSError:
                raw = b""
            return raw.decode("utf-8", "replace")
        data = self.term.stdin.read(64)
        return data or ""

    def stop(self) -> None:
        self._running = False

    def _handle_input(self, data: str) -> "bool | None":
        """Decide how a raw input chunk is handled before key parsing.

        Returns ``True`` when the chunk requests an exit, ``None`` when the
        app has consumed the chunk itself (the loop must not re-dispatch it
        as keys), or ``False`` to dispatch the chunk to the key reader (the
        default behaviour; preserves the Phase 1 demo's contract).
        """
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
            # render_frame already wraps the update in CSI 2026 markers;
            # it is the single emission entry point (no sync_out double-wrap).
            self.term._write(update)
        self.frame = new_frame

    def quit_event(self) -> None:
        """Convenience for worker threads: request an exit."""
        self.events.put("quit")
