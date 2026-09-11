"""Themes for the TUI v2 (ported conceptually from prime-agent's
``theme/*.json`` files — dark, light, prime).

Each theme is a small immutable mapping of named colours to
:class:`~nbchat.tui2.frame.Style` values.  Colours are expressed as
0-15 palette indices (8-15 the bright variants) plus bold/dim flags; the
engine's :class:`Style` renders these through the structured SGR codes
(``38;5;N`` / attribute flags), which is exactly what the differential
renderer expects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from .frame import Style


def style(fg_index: int, bold: bool = False, dim: bool = False) -> Style:
    """Build a structured :class:`Style` from a 0-15 colour index.

    ``fg_index`` is the palette position (0-7 normal, 8-15 bright).  The
    ``bold``/``dim`` flags map straight onto the :class:`Style` dataclass
    so the renderer emits a well-formed SGR sequence — a raw SGR *string*
    must never be stored in the ``fg`` field, which the renderer assumes
    is an integer.
    """
    if not (0 <= fg_index <= 15):
        raise ValueError("fg_index must be 0-15")
    return Style(fg=fg_index, bold=bold, dim=dim)


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


# Concrete theme objects (the actual colour sets).
_DARK_T: Theme = _dark()
_LIGHT_T: Theme = _light()
_PRIME_T: Theme = _prime()

_BY_NAME: Dict[str, Theme] = {
    _DARK_T.name: _DARK_T,
    _LIGHT_T.name: _LIGHT_T,
    _PRIME_T.name: _PRIME_T,
}


class _ThemeRef:
    """A stable module-level name that proxies attribute access to the
    currently-active theme.

    Components import ``DARK`` and keep writing ``DARK.accent`` etc.; the
    proxy forwards every attribute to the active theme, so ``set_active``
    (``/theme``) switches the colours everywhere without touching the
    call sites.  ``DARK`` is created once at import time and its target is
    mutated in place, so every ``from .theme import DARK`` binding shares
    the same proxy.
    """

    def __init__(self, target: Theme):
        self._target = target

    def __getattr__(self, name: str):
        # Only reached for names not found on the instance itself
        # (i.e. not ``_target``), forwarding to the active theme.
        return getattr(self._target, name)

    def set_target(self, target: Theme) -> None:
        self._target = target

    @property
    def active(self) -> Theme:
        return self._target


# The public ``DARK`` is the live proxy (defaulting to the dark theme); the
# concrete LIGHT / PRIME remain available as real Theme objects for lookup.
DARK = _ThemeRef(_DARK_T)
LIGHT: Theme = _LIGHT_T
PRIME: Theme = _PRIME_T


def current() -> Theme:
    """The currently-active theme."""
    return DARK._target


def set_active(name: str) -> Theme:
    """Switch the active theme (case-insensitive); unknown -> dark.

    Returns the theme that is now active.
    """
    theme = _BY_NAME.get(name.strip().lower(), _DARK_T)
    DARK.set_target(theme)
    return theme


def get_theme(name: str) -> Theme:
    """Look up a theme by name (case-insensitive); defaults to dark."""
    return _BY_NAME.get(name.strip().lower(), _DARK_T)


def all_themes() -> Tuple[Theme, ...]:
    return (_DARK_T, _LIGHT_T, _PRIME_T)
