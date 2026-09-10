"""In-TUI notification stack (herdr-style toasts + terminal BEL + an
optional, best-effort sound ladder).

A *toast* is a small bordered card rendered in the frame just above the
input box for a few seconds.  Auto-dismiss is timestamp-based and checked
in the per-frame refresh pass — no timers or background threads.  Side
channels are best-effort and dependency-free by default:

* **terminal BEL** (``\\a``) — always available, the classic "done / needs
  attention" signal;
* **.wav sound** — played on a daemon thread via ``aplay``/``afplay`` only
  when ``NBCHAT_SOUND_DIR`` points at a directory of ``<kind>.wav`` files
  and a player exists.  Kill switch: ``NBCHAT_NO_SOUND=1``.

The stack is intentionally tiny and self-contained (``notify.py``) so it
adds no architectural risk to the tui2 engine.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .components import Box, Text
from .frame import Line
from .theme import DARK

# A terminal writer (str -> None); ``None`` in tests disables side channels.
Writer = Optional[Callable[[str], None]]

_KIND_STYLE = {
    "ok": DARK.ok,
    "warn": DARK.warn,
    "error": DARK.error,
}


def _wrap(text: str, width: int) -> List[Line]:
    return Text(text or " ").render(max(width, 4))


@dataclass
class Toast:
    title: str
    body: str
    kind: str  # "info" | "ok" | "warn" | "error"
    ts: float = field(default_factory=time.monotonic)


class NotifyStack:
    """A bounded queue of transient toast cards plus a delivery policy."""

    def __init__(self, max_age: float = 5.0, max_visible: int = 3,
                 toasts: bool = True, bel: bool = True, sound: bool = False):
        self.max_age = max_age
        self.max_visible = max_visible
        self.toasts = toasts
        self.bel = bel
        self.sound = sound
        self._queue: List[Toast] = []

    # -- policy -------------------------------------------------------
    def push(self, title: str, body: str = "", kind: str = "info",
             write: Writer = None, *, force_sound: bool = False,
             force_bel: bool = False) -> None:
        """Enqueue a toast and fire the side channels (BEL / sound).

        ``write`` is a str-writer used for the BEL (``None`` in tests).
        ``force_sound`` / ``force_bel`` override the policy for one event
        (e.g. an approval prompt always rings even if the user muted the
        main-turn chime).
        """
        if self.toasts:
            self._queue.append(Toast(title, body, kind))
            if len(self._queue) > self.max_visible:
                self._queue = self._queue[-self.max_visible:]
        if write is None:
            return
        if self.bel or force_bel:
            try:
                write("\a")
            except Exception:
                pass
        if (self.sound or force_sound) and kind in ("ok", "warn", "error"):
            play_sound(kind)

    def prune(self) -> None:
        """Drop expired toasts (call once per frame)."""
        now = time.monotonic()
        self._queue = [t for t in self._queue if now - t.ts < self.max_age]

    @property
    def active(self) -> List[Toast]:
        return list(self._queue)

    # -- render -------------------------------------------------------
    def _card(self, t: "Toast", width: int) -> List[Line]:
        inner = width - 4
        lines = _wrap(t.title, inner)
        if t.body:
            lines = lines + _wrap(t.body, inner)
        lines = lines[:4]  # keep each card compact
        return Box(
            title=t.kind, lines=lines, clip=True,
            border_style=_KIND_STYLE.get(t.kind, DARK.border),
        ).render(width)

    def render_one(self, width: int) -> List[Line]:
        """Render only the most recent toast (single compact card)."""
        if not self._queue:
            return []
        return self._card(self._queue[-1], width)

    def render(self, width: int) -> List[Line]:
        if not self._queue:
            return []
        out: List[Line] = []
        for t in self._queue:
            out.extend(self._card(t, width))
        return out


def play_sound(kind: str) -> None:
    """Best-effort .wav playback on a daemon thread.  Never raises."""
    if os.environ.get("NBCHAT_NO_SOUND"):
        return
    snd_dir = os.environ.get("NBCHAT_SOUND_DIR")
    if not snd_dir:
        return
    path = os.path.join(snd_dir, f"{kind}.wav")
    if not os.path.exists(path):
        return
    player = shutil.which("aplay") or shutil.which("afplay")
    if not player:
        return

    def _run() -> None:
        try:
            subprocess.run([player, path], capture_output=True, timeout=6)
        except Exception:
            pass

    threading.Thread(target=_run, name="nbchat-sound", daemon=True).start()
