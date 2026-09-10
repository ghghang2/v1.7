"""Phase 1b — adaptive reasoning effort.

The conversation loop runs every LLM turn at a fixed ``reasoning_effort``
(default ``medium``).  This module gives the loop a tiny, pure decision
kernel so the effort can *react to evidence* instead of being static:

* **Escalate** when the agent is failing (consecutive tool errors) or
  stalling (identical tool calls for ``STALL_TURNS`` rows) — more
  reasoning is exactly what those situations need.
* **Reset** after a short run of clean turns — we do not want to pay
  the xhigh token premium forever after one bad patch.

Design constraints (see docs/optimization-roadmap.md, item 1b):

* The ladder is strict and short: ``low < medium < high < xhigh``.
  ``next_effort`` returns at most one rung up, so a single trigger can
  never jump straight to the most expensive tier.
* A **pinned** effort (user set ``/effort`` explicitly) is always
  respected: the loop only calls these helpers for auto-escalation
  when nothing is pinned.  The helpers are still pure and testable.
* No I/O, no state — the loop owns the state; this module owns the
  rules.  That keeps the wiring trivially reversible.
"""
from __future__ import annotations

__all__ = [
    "EFFORT_LADDER",
    "DEFAULT_EFFORT",
    "ESCALATE_AFTER_ERRORS",
    "CLEAN_TURNS_TO_RESET",
    "next_effort",
    "should_reset",
]

# Ordered cheapest -> most expensive.  "none" is not a rung: a session
# with effort "none" simply never escalates (next_effort returns None).
EFFORT_LADDER = ("low", "medium", "high", "xhigh")

# Session default (matches config.DEFAULT_REASONING_EFFORT).
DEFAULT_EFFORT = "medium"

# Escalate on failure once the agent has this many tool errors *in a row*.
# Two in a row means the current approach is broken; one transient error
# should not flip us to a more expensive tier.
ESCALATE_AFTER_ERRORS = 2

# Drop back to the base effort after this many consecutive clean turns
# (no tool errors, no stalls).  Short enough that we stop paying the
# premium quickly; long enough that a genuinely hard task gets a run of
# high-effort turns before we relax.
CLEAN_TURNS_TO_RESET = 3


def next_effort(current: str, reason: str = "") -> str | None:
    """One rung up from *current*, or ``None`` if already at the top
    (or if *current* is not a ladder rung, e.g. ``"none"``/``""``).

    *reason* is only for logging; the decision itself is pure.
    """
    try:
        idx = EFFORT_LADDER.index((current or "").lower())
    except ValueError:
        return None
    if idx + 1 >= len(EFFORT_LADDER):
        return None
    return EFFORT_LADDER[idx + 1]


def should_reset(clean_turns: int, base_effort: str) -> bool:
    """True once *clean_turns* has reached the reset threshold AND the
    base effort is a ladder rung (never reset a "none" session — there
    is nothing to reset)."""
    if (base_effort or "").lower() not in EFFORT_LADDER:
        return False
    return clean_turns >= CLEAN_TURNS_TO_RESET
