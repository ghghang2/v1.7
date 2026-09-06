"""Keyboard input parsing for the TUI v2 (Phase 1).

:class:`KeyReader` is a small state machine that consumes raw terminal
bytes (as ``str``) — potentially split across chunk boundaries — and
yields :class:`Key` objects.  It understands:

- plain printable characters and Enter / Backspace / Tab
- Ctrl+letter combinations (Ctrl+C = ``"ctrl+c"`` …)
- arrows, Home/End, PgUp/PgDn, F1-F4 via CSI escape sequences
- 0x7f and 0x08 as backspace
- bracketed paste: text between ``ESC[200~`` and ``ESC[201~`` is
  delivered as a single ``Key("paste", text)`` even when the escape
  sequences arrive in separate chunks

The reader is *re-entrant per chunk*: call :meth:`feed` with each chunk
of raw input and it returns the keys completed by that chunk.  Any
partial escape sequence is buffered until more input arrives.

Only the xterm/VT100 subset is decoded — this matches what prime-agent's
TUI relies on (arrows, Ctrl+C, Enter, paste).  Mouse reports and other
exotic sequences are swallowed and reported as ``Key("unknown", raw)``
so they can never corrupt the input buffer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"

# CSI sequences (without the leading ESC[) mapped to key names.
# Order matters: longer prefixes are tried first when matching.
_CSI_KEYS = (
    ("A", "up"),
    ("B", "down"),
    ("C", "right"),
    ("D", "left"),
    ("H", "home"),
    ("F", "end"),
    ("5~", "pageup"),
    ("6~", "pagedown"),
    ("1~", "home"),
    ("4~", "end"),
    ("7~", "home"),
    ("8~", "end"),
    ("11~", "f1"),
    ("12~", "f2"),
    ("13~", "f3"),
    ("14~", "f4"),
    ("200~", None),  # bracketed paste open — handled by _PASTE_START
    ("201~", None),  # bracketed paste close — handled by _PASTE_END
)


@dataclass(frozen=True)
class Key:
    """A parsed keystroke.

    ``name`` is one of the semantic names (``"up"``, ``"ctrl+c"``,
    ``"enter"``, ``"paste"``, …) or the single character itself.
    ``payload`` carries the pasted text for ``"paste"`` keys and the raw
    sequence for ``"unknown"`` keys.
    """

    name: str
    payload: Optional[str] = None

    def __str__(self) -> str:  # pragma: no cover - debug aid
        return f"Key({self.name!r}" + (f", {self.payload!r})" if self.payload is not None else ")")


def _match_csi(body: str) -> Optional[str]:
    """Match *body* (the text after ``ESC[``) against known keys."""
    for suffix, name in _CSI_KEYS:
        if body == suffix or body.startswith(suffix + ";"):
            return name
    return None


class KeyReader:
    """Incremental parser from raw terminal input to :class:`Key`."""

    def __init__(self) -> None:
        self._buf: str = ""
        self._pasting = False
        self._paste_text: str = ""

    @property
    def in_paste(self) -> bool:
        return self._pasting

    def feed(self, data: str) -> List[Key]:
        """Consume one chunk of raw input; return completed keys."""
        self._buf += data
        keys: List[Key] = []
        while True:
            key, consumed = self._take_one()
            if key is None:
                break
            self._buf = self._buf[consumed:]
            keys.append(key)
        return keys

    # -- internals ------------------------------------------------------
    def _take_one(self):
        """Return ``(key_or_None, bytes_consumed)``.

        ``bytes_consumed`` is 0 when the buffer starts a sequence that
        is not yet complete (nothing may be consumed).
        """
        b = self._buf
        if not b:
            return None, 0

        # Bracketed paste text mode: consume until the closing marker.
        if self._pasting:
            idx = b.find(_PASTE_END)
            if idx < 0:
                # Incomplete: keep all but the partial marker tail.
                return None, 0
            text = b[:idx]
            self._pasting = False
            self._paste_text = ""
            return Key("paste", text), idx + len(_PASTE_END)

        if b.startswith("\x1b["):
            # CSI: find the terminator (bytes 0x40-0x7E).
            end = -1
            for i in range(1, len(b)):
                if 0x40 <= ord(b[i]) <= 0x7E:
                    end = i
                    break
            if end < 0:
                # Incomplete CSI (marker possibly split across chunks).
                return None, 0
            body = b[2:end]
            if body == "200~":
                self._pasting = True
                self._paste_text = ""
                return Key("paste-start", ""), end + 1
            name = _match_csi(body)
            if name is None:
                # Unknown CSI (mouse, etc.) — swallow it whole.
                return Key("unknown", b[: end + 1]), end + 1
            return Key(name), end + 1

        if b.startswith("\x1b"):
            # Standalone ESC or a two-key legacy sequence (ESC O x).
            if len(b) >= 2 and b[1] == "O":
                if len(b) >= 3:
                    legacy = {"A": "up", "B": "down", "C": "right",
                              "D": "left"}.get(b[2])
                    if legacy:
                        return Key(legacy), 3
                    return Key("unknown", b[:3]), 3
                return None, 0  # wait for the third byte
            # Bare ESC.
            return Key("escape"), 1

        ch = b[0]
        if ch in ("\r", "\n"):
            return Key("enter"), 1
        if ch in ("\x7f", "\x08"):
            return Key("backspace"), 1
        if ch == "\t":
            return Key("tab"), 1
        if 0x01 <= ord(ch) <= 0x1a:
            return Key("ctrl+" + chr(ord(ch) + 0x60)), 1
        if ch == " ":
            return Key("space"), 1
        if ord(ch) < 0x20:
            return Key("unknown", ch), 1
        return Key(ch), 1

    def flush(self) -> List[Key]:
        """Discard any pending partial sequence (e.g. on exit)."""
        self._buf = ""
        self._pasting = False
        self._paste_text = ""
        return []
