"""Lightweight ANSI colour helper for the terminal UI.

Uses raw escape sequences so it works in a basic terminal with no extra
dependencies (no ``rich`` / ``colorama``).  Colours are auto-disabled when
stdout is not a TTY or when ``NO_COLOR`` is set, so output piped to a file or
another program stays clean.
"""
from __future__ import annotations

import os
import sys

_RESET = "\033[0m"


class Palette:
    """Small wrapper that colours text unless disabled."""

    def __init__(self, color: bool = True) -> None:
        # Respect an explicit opt-out plus a live-terminal check.
        self.color = (
            color
            and bool(sys.stdout.isatty())
            and not os.environ.get("NO_COLOR")
        )

    def _wrap(self, code: str, text: str) -> str:
        if not self.color or not text:
            return text
        return f"\033[{code}m{text}{_RESET}"

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def blue(self, text: str) -> str:
        return self._wrap("34", text)

    def magenta(self, text: str) -> str:
        return self._wrap("35", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def gray(self, text: str) -> str:
        return self._wrap("90", text)
