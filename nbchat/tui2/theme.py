"""Themes for the TUI v2 (ported conceptually from prime-agent's
``theme/*.json`` files — dark, light, prime).

Each theme is a small immutable mapping of named colours to
:class:`~nbchat.tui2.frame.Style` values.  Only a compact SGR subset is
used (30-37 / 90-97 foregrounds plus bold and dim), so it works on every
terminal and stays cheap to diff.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Tuple

from .frame import Style

# SGR codes for the 16 ANSI colours: 40-47 normal, 48;5+x for bright.
_FG = ("30", "31", "32", "33", "34", "35", "36", "37")
_BRIGHT_FG = ("90", "91", "92", "93", "94", "95", "96", "97")
_BOLD = "1"
_DIM = "2"


def _code(fg: str, bold: bool = False, dim: bool = False) -> str:
    parts = []
    if bold:
        parts.append(_BOLD)
    if dim:
        parts.append(_DIM)
    parts.append(fg)
    return ";".join(parts)


def style(fg_index: int, bold: bool = False, dim: bool = False) -> Style:
    """Build a :class:`Style` from an 0-7 colour index."""
    return Style(_code(_FG[fg_index], bold, dim))


BRIGHT: Mapping[str, str] = dict(zip(
    ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"),
    _BRIGHT_FG,
))


@dataclass(frozen=True)
class Theme:
    """Named colour set used by the TUI components."""

    name: str
    primary: Style
    accent: Style
    muted: Style
    user: Style
    assistant: Style
    tool: Style
    ok: Style
    warn: Style
    error: Style
    border: Style
    status: Style

    def as_dict(self) -> Dict[str, str]:
        """Colour name -> SGR code (for inspection/tests)."""
        return {
            field: s.as_code() for field, s in (
                ("primary", self.primary),
                ("accent", self.accent),
                ("muted", self.muted),
                ("user", self.user),
                ("assistant", self.assistant),
                ("tool", self.tool),
                ("ok", self.ok),
                ("warn", self.warn),
                ("error", self.error),
                ("border", self.border),
                ("status", self.status),
            )
        }


def _dark() -> Theme:
    return Theme(
        name="dark",
        primary=style(7),
        accent=style(5, bold=True),
        muted=style(7, dim=True),
        user=style(3, bold=True),
        assistant=style(7),
        tool=style(6),
        ok=style(2),
        warn=style(3),
        error=style(1, bold=True),
        border=style(7, dim=True),
        status=style(6, dim=True),
    )


def _light() -> Theme:
    return Theme(
        name="light",
        primary=style(0),
        accent=style(4, bold=True),
        muted=style(0, dim=True),
        user=style(2, bold=True),
        assistant=style(0),
        tool=style(5),
        ok=style(2),
        warn=style(3),
        error=style(1, bold=True),
        border=style(0, dim=True),
        status=style(4, dim=True),
    )


def _prime() -> Theme:
    # The "prime" identity: magenta accent over a neutral base.
    return Theme(
        name="prime",
        primary=style(7),
        accent=style(5, bold=True),
        muted=style(7, dim=True),
        user=style(4, bold=True),
        assistant=style(7),
        tool=style(6),
        ok=style(2),
        warn=style(3),
        error=style(1, bold=True),
        border=style(5, dim=True),
        status=style(6, dim=True),
    )


DARK: Theme = _dark()
LIGHT: Theme = _light()
PRIME: Theme = _prime()

_BY_NAME: Dict[str, Theme] = {
    DARK.name: DARK,
    LIGHT.name: LIGHT,
    PRIME.name: PRIME,
}


def get_theme(name: str) -> Theme:
    """Look up a theme by name (case-insensitive); defaults to dark."""
    return _BY_NAME.get(name.strip().lower(), DARK)


def all_themes() -> Tuple[Theme, ...]:
    return (DARK, LIGHT, PRIME)
