"""Interactive Phase 1 demo for the TUI v2 engine (``python -m nbchat.tui2``).

A self-contained, dependency-free application that exercises every Phase 1
building block against a *real* terminal:

* ``RawTerminal``           raw mode, alt screen, bracketed paste, restore
* ``TUIApp`` + ``KeyReader`` event loop with key dispatch
* ``Box`` / ``Text`` / ``StatusLine`` / ``Loader`` (animated spinner)
* the differential renderer (only changed lines are rewritten)
* ``EventQueue``            background threads push events; the UI reacts

Keys: arrows / PgUp / PgDn scroll the box, ``r`` forces a re-render,
``q`` / Esc / Ctrl+C quits.  The spinner and the "events" line update
from two background threads to prove the render/event pipeline end to end.
"""
from __future__ import annotations

import threading
import time

from .components import Box, Container, Loader, Spacer, StatusLine, Text
from .frame import Frame, Line
from .keys import KeyReader
from .raw import EventQueue, RawTerminal, TUIApp
from .theme import DARK

# (text, style) pairs for the box body.  ``Spacer`` rows are marked ``None``.
_HELP = [
    ("TUI v2 — Phase 1 demo engine", DARK.accent),
    ("A live test of raw mode, the differential renderer and the", DARK.muted),
    ("render/event loop.  No model is involved; everything below is", DARK.muted),
    ("driven by the engine itself.", DARK.muted),
    (None, None),
    ("Keys: arrows / PgUp / PgDn scroll this box, r re-renders,", DARK.muted),
    ("q / Esc / Ctrl+C quit.  The spinner and the event counter are", DARK.muted),
    ("updated by two background threads via the EventQueue.", DARK.muted),
    (None, None),
    ("The differential renderer only rewrites lines that changed:", DARK.user),
    ("watch this box scroll — only the moved line is touched.", DARK.user),
    (None, None),
    ("If you are reading this, the engine works: raw mode, alt", DARK.ok),
    ("screen, bracketed paste and clean terminal restore are all on.", DARK.ok),
]


def _to_lines(pairs, width: int) -> List[Line]:  # type: ignore[name-defined]
    """Render the (text, style) help pairs into :class:`Line` objects."""
    from typing import List  # noqa: F401  (runtime import guard)

    out: "List[Line]" = []
    for text, style in pairs:
        if text is None:
            out.append(Spacer(1).render(width)[0])
        else:
            out.extend(Text(text, style, width=width).render(width))
    return out


class DemoApp:
    """The demo application (state + frame builder)."""

    def __init__(self, terminal: RawTerminal, events: EventQueue) -> None:
        self.term = terminal
        self.events = events
        self.scroll = 0
        self.tick = int(time.time()) % 8
        self.events_seen = 0
        self.renders = 1
        # One persistent spinner so its tick state survives across frames.
        self.loader = Loader("spinner worker (background thread)", active=True)
        self._threads_done = False
        self.app: "TUIApp" = None  # type: ignore[assignment]

    # -- input ----------------------------------------------------------
    def on_input(self, key) -> None:
        if key.name in ("q", "escape", "ctrl+c"):
            self._quit()
        elif key.name in ("up", "pageup"):
            self.scroll = max(self.scroll - 1, -3)
        elif key.name in ("down", "pagedown"):
            self.scroll = min(self.scroll + 1, 3)
        elif key.name == "r":
            self.renders += 1

    def _quit(self) -> None:
        if self.app is not None:
            self.app.stop()

    # -- background workers (EventQueue consumers) ----------------------
    def _spinner_worker(self) -> None:
        while not self._threads_done:
            time.sleep(0.2)
            self.tick += 1
            self.events.put("render")

    def _event_worker(self) -> None:
        while not self._threads_done:
            time.sleep(0.7)
            self.events_seen += 1
            self.events.put("render")

    def start_workers(self) -> None:
        for fn in (self._spinner_worker, self._event_worker):
            t = threading.Thread(target=fn, daemon=True)
            t.start()

    # -- rendering ------------------------------------------------------
    def build_frame(self) -> Frame:
        w, h = self.term.width, self.term.height
        inner_w = max(w - 2, 1)
        lines = _to_lines(_HELP, inner_w)
        # Scroll window: at most ``h - 6`` visible body rows (box borders,
        # loader, spacer and status line take the rest).
        visible = max(h - 6, 4)
        start = min(max(self.scroll, 0), max(len(lines) - visible, 0))
        box = Box(
            title="nbchat · TUI v2 · Phase 1 demo",
            lines=lines[start:start + visible],
            clip=True,
            rows=visible + 2,
        )
        # Reuse the persistent spinner; its internal _tick carries over so
        # step() advances the glyph each frame instead of resetting to 0.
        loader = self.loader.step(self.tick)
        content = Container([
            box,
            Spacer(1),
            loader,
            Spacer(1),
            StatusLine(
                left=f"events from background: {self.events_seen}   renders: {self.renders}",
                right="q quit",
            ),
        ])
        rows = content.render(w)[:h]
        return Frame(lines=rows, width=w, height=h)


def run() -> int:
    import sys

    term = RawTerminal(sys.stdin, sys.stdout)
    events = EventQueue()
    app = DemoApp(term, events)
    tui = TUIApp(term, events)
    tui.set_frame_provider(app.build_frame)
    tui.set_key_reader(KeyReader())
    tui.on_input = app.on_input
    app.app = tui
    try:
        with term:
            app.start_workers()
            tui.start()
    finally:
        app._threads_done = True
        events.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
