"""Live status line + one-line session recap for the terminal REPL.

The status line is the single highest-value "channel" win (Claude-Code feature
review, Tier 1 #1).  It is rendered as a *self-overwriting line* so it never
interferes with the streamed assistant reply that is printed above it:

    model · mode · ctx ▓▓▓▓▓░░░ 42% · 12.3k/32k tok · cache 87% · 14.2 tok/s · tool: pytest 2.1s

Redraw strategy (deliberately the most performant for a line-based UI):
  * No full-screen / mouse / cursor-jumping escape sequences.
  * Every update overwrites the previous line with
    ``\\r\\033[K<text>\\033[K`` — clear-to-end-of-line on both sides so a shorter
    update cannot leave a tail.  ``\\r`` (no newline) means the next print()
    lands cleanly below it.
  * Updated *per event* (turn start, tool start/finish, retry, compact, turn
    end), not per streamed token, so the hot path never pays a status cost.
    Tool elapsed time is animated by a single 1 Hz background thread that is
    started only while a tool is running.

The status line is intentionally a *view* over state the agent already keeps:
context window + budget (``ContextMixin._window``), cache similarity
(``nbchat.core.monitoring``), and recent throughput
(``app.last_turn_stats``).  It never computes anything new.
"""
from __future__ import annotations

import json
import time
import threading
from typing import Dict, List, Optional, Tuple

import nbchat.core.config as config

from .colors import Palette

# ── Context-window capacity thresholds ────────────────────────────────────
# Ratio of the context budget in use.  ``WARN`` drives the /context warning +
# auto-compact suggestion; ``COMPACT`` triggers auto-compact.  Both are
# conservative relative to the headroom already applied in _window().
CTX_WARN_FRACTION = 0.80
CTX_COMPACT_FRACTION = 0.90

# ── Recap thresholds ───────────────────────────────────────────────────────
RECAP_IDLE_SECONDS = 180      # ≥3 min since the last assistant turn
RECAP_MIN_TURNS = 3           # and ≥3 user turns since the last recap
RECAP_MAX_CHARS = 200

# ANSI: carriage-return, clear-to-EOL, then text, then clear-to-EOL again so a
# shorter replacement cannot leave a stale tail.
_CLEAR = "\033[K"


def _fmt_k(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def bar(fraction: float, width: int = 8) -> str:
    """Return a ▓░ bar for ``fraction`` in [0, 1]."""
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return "▓" * filled + "░" * (width - filled)


def _truncate(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


class StatusLine:
    """A self-overwriting status line, owned by the TUI agent."""

    def __init__(self, palette: Palette):
        self.palette = palette
        # Current phase: idle | thinking | tool | waiting | compacting | error
        self._phase = "idle"
        self._phase_detail = ""          # e.g. tool name, retry label, error text
        self._phase_start = 0.0         # monotonic when current phase began
        self._tool_running = False
        # Latest context metrics, refreshed by the agent after each window
        self._ctx_used = 0              # estimated tokens in current window
        self._ctx_budget = 0            # effective token budget
        # Latest cache similarity for the most recent turn (0.0..1.0)
        self._cache_sim = 0.0
        # Recent throughput (tok/s) — refreshed cheaply by the agent
        self._tps: Optional[float] = None
        # The previous line's rendered width, so we only repaint on change.
        self._last_text = ""
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    # ── State setters (called by agent / conversation hooks) ──────────────
    def set_context(self, used_tokens: int, budget_tokens: int) -> None:
        self._ctx_used = int(max(0, used_tokens))
        self._ctx_budget = int(max(0, budget_tokens))

    def set_cache_sim(self, sim: float) -> None:
        self._cache_sim = max(0.0, min(1.0, float(sim)))

    def set_tps(self, tps: Optional[float]) -> None:
        self._tps = tps

    def idle(self) -> None:
        self._set_phase("idle", "")

    def thinking(self) -> None:
        self._set_phase("thinking", "")

    def tool_start(self, name: str) -> None:
        self._tool_running = True
        self._set_phase("tool", name)

    def tool_done(self, name: str, elapsed: float) -> None:
        self._tool_running = False
        # Brief confirmation with the measured duration, then idle.
        self._set_phase("idle", f"✓ {name} {elapsed:.1f}s")

    def waiting(self, detail: str) -> None:
        self._set_phase("waiting", detail)

    def compacting(self) -> None:
        self._set_phase("compacting", "")

    def error(self, detail: str) -> None:
        self._set_phase("error", detail)

    # ── Rendering ──────────────────────────────────────────────────────────
    def _set_phase(self, phase: str, detail: str) -> None:
        self._phase = phase
        self._phase_detail = detail
        self._phase_start = time.monotonic()
        self.render()

    def ctx_fraction(self) -> float:
        if self._ctx_budget <= 0:
            return 0.0
        return self._ctx_used / self._ctx_budget

    def render(self) -> None:
        """Rebuild + paint the status line (idempotent; no-op if unchanged)."""
        text = self._build_text()
        if text == self._last_text:
            return
        self._last_text = text
        line = f"\r{_CLEAR}{self.palette.dim(text)}{_CLEAR}"
        try:
            import sys
            sys.stdout.write(line)
            sys.stdout.flush()
        except Exception:
            pass

    def _build_text(self) -> str:
        # Phase token — the leftmost, most human-glanceable field.
        elapsed = time.monotonic() - self._phase_start if self._phase_start else 0.0
        if self._phase == "tool":
            phase = f"⚙ {self._phase_detail} {elapsed:.0f}s"
        elif self._phase == "thinking":
            phase = f"◌ thinking {elapsed:.0f}s"
        elif self._phase == "waiting":
            phase = f"… {self._phase_detail or 'waiting'}"
        elif self._phase == "compacting":
            phase = "▒ compacting…"
        elif self._phase == "error":
            phase = f"✗ {_truncate(self._phase_detail, 32)}"
        else:
            if self._phase_detail:      # brief confirmation after a tool
                phase = f"idle · {self._phase_detail}"
            else:
                phase = "idle"

        # Context meter.
        if self._ctx_budget > 0:
            frac = self.ctx_fraction()
            ctx = (f"{bar(frac)} {int(frac * 100):3d}% "
                   f"{_fmt_k(self._ctx_used)}/{_fmt_k(self._ctx_budget)} tok")
        else:
            ctx = "ctx --/---"

        # Cache hit for the most recent turn.
        cache = f"cache {int(self._cache_sim * 100)}%" if self._cache_sim > 0 else "cache --"

        # Recent throughput.
        tps = f"{self._tps:.1f} tok/s" if (self._tps is not None and self._tps > 0) else "-- tok/s"

        return f"{phase} · {ctx} · {cache} · {tps}"

    # ── Light 1 Hz animation for the running tool (elapsed clock) ─────────
    def _start_timer(self) -> None:
        with self._lock:
            if self._timer is not None:
                return
            self._timer = threading.Timer(1.0, self._tick)
            self._timer.daemon = True
            self._timer.start()

    def _stop_timer(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _tick(self) -> None:
        with self._lock:
            if self._tool_running and self._phase == "tool":
                self.render()
                self._timer = threading.Timer(1.0, self._tick)
                self._timer.daemon = True
                self._timer.start()
            else:
                self._timer = None

    # Called by the agent around the tool executor.
    def on_tool_running(self, running: bool) -> None:
        self._tool_running = running
        if running:
            self._start_timer()
        else:
            self._stop_timer()


# ── Session recap (Tier 1 #5) ──────────────────────────────────────────────
# Generated *asynchronously* so it never blocks the user's next input.  We
# reuse the primary model (per product decision) with a short, bounded prompt.

_RECAP_PROMPT = (
    "Summarise the following conversation in EXACTLY ONE line of at most "
    "120 characters. State what was accomplished and the current state "
    "(e.g. open questions or next step). Be concrete and factual. "
    "No preamble, no quotes, no labels.\n\n"
)


def build_recap_context(history: List[Tuple], max_turns: int = 8) -> str:
    """Build a compact, prompt-sized transcript of the recent *user* turns.

    ``history`` rows are ``(role, content, tool_id, tool_name, tool_args,
    error_flag)``.  We keep the last few user→assistant exchanges, truncating
    each so the recap prompt stays tiny and cheap on a local model.
    """
    # Walk back, collecting the last ``max_turns`` user turns with the
    # assistant reply that immediately follows (if any).
    pairs: List[Tuple[str, str]] = []
    n_user = 0
    for i in range(len(history) - 1, -1, -1):
        role, content = history[i][0], history[i][1] or ""
        if role == "user":
            n_user += 1
            if n_user > max_turns:
                break
            # Find the next assistant row after this one.
            reply = ""
            for j in range(i + 1, len(history)):
                if history[j][0] in ("assistant", "assistant_full"):
                    reply = history[j][1] or ""
                    break
            pairs.append((content[:400], reply[:400]))
    pairs.reverse()
    lines = []
    for k, (u, a) in enumerate(pairs, 1):
        u = _truncate(u, 240)
        a = _truncate(a, 240)
        lines.append(f"{k}. User: {u}")
        if a:
            lines.append(f"   Agent: {a}")
    return "\n".join(lines)


def generate_recap(history: List[Tuple], model_name: str) -> Optional[str]:
    """Generate a one-line recap.  Returns None on any failure (never raises).

    Kept synchronous + bounded so it can be fired on a background thread by
    the agent when the idle/recap threshold is met.
    """
    try:
        transcript = build_recap_context(history)
        if not transcript.strip():
            return None
        from nbchat.core.client import get_client
        resp = get_client().chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": _RECAP_PROMPT},
                {"role": "user", "content": transcript},
            ],
            max_tokens=80,
            temperature=0.3,
        )
        text = (resp.choices[0].message.content or "").strip()
        text = " ".join(text.split())
        return _truncate(text, RECAP_MAX_CHARS) or None
    except Exception:
        return None


def recap_eligible(last_activity_monotonic: float,
                   user_turns_since_last_recap: int,
                   last_recap_monotonic: float) -> bool:
    """True when a recap may fire.

    Gated so it (a) only fires after ≥3 min idle, (b) requires ≥3 user turns
    since the last recap, and (c) never fires twice without new turns.
    """
    now = time.monotonic()
    if now - last_activity_monotonic < RECAP_IDLE_SECONDS:
        return False
    if user_turns_since_last_recap < RECAP_MIN_TURNS:
        return False
    # Never re-fire for the *same* idle window (same activity timestamp).
    if last_recap_monotonic and last_recap_monotonic >= last_activity_monotonic:
        return False
    return True
