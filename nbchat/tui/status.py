"""TUI status line & agent-activity model (see docs/tui_statusline_plan.md).

Framework-free and unit-testable:

* :func:`render_line` is a *pure* function -- given a snapshot dict and a
  terminal width it returns a single-line string (no I/O, no globals).
* :class:`AgentStatus` is one record per actor (assistant, supervisor,
  team workers, ...).
* :class:`StatusBar` is a thread-safe registry + state setter that the
  hook sites call; the ticker thread (Phase 2) reads a ``snapshot()``.

States: ``idle | thinking | tool | waiting | compacting | error | done |
stalled``.
"""

from __future__ import annotations

import sys
import threading
import time

STATES = ("idle", "thinking", "tool", "waiting", "compacting",
          "error", "done", "stalled")

# Fixed left-to-right priority.  Truncation drops from the right first, but
# the context bar (item 3) is demoted earlier than model name -- see
# ``render_line`` which builds segments in priority order and stops when the
# terminal runs out of room.
_BAR_CELLS = 10


class AgentStatus:
    """One actor's live state, owned by the :class:`StatusBar` lock."""

    __slots__ = ("id", "label", "state", "detail", "since",
                 "tokens_seen", "last_tool", "owner_alive")

    def __init__(self, agent_id: str, label: str | None = None) -> None:
        self.id = agent_id
        self.label = label or agent_id
        self.state = "idle"
        self.detail = ""
        self.since = time.monotonic()
        self.tokens_seen = 0
        self.last_tool = ""
        self.owner_alive = True

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "state": self.state,
            "detail": self.detail,
            "since": self.since,
            "tokens_seen": self.tokens_seen,
            "last_tool": self.last_tool,
            "owner_alive": self.owner_alive,
        }


def _humanise(n: float) -> str:
    """12300 -> '12.3k'; 950 -> '950'; 1200 -> '1.2k'."""
    if n < 1000:
        return str(int(n))
    v = n / 1000.0
    if v >= 100:
        return f"{int(round(v))}k"
    return f"{v:.1f}k"


def _truncate(s: str, width: int) -> str:
    """Hard-truncate to ``width`` cells, marking truncation with a trailing
    ellipsis (which counts as one cell)."""
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    if width == 1:
        return "…"
    return s[: width - 1] + "…"


def _state_label(state: str, detail: str) -> str:
    if state == "tool":
        return f"tool: {detail}" if detail else "tool"
    if state == "error":
        return f"error: {detail}" if detail else "error"
    if state == "waiting" and detail:
        return f"waiting ({detail})"
    if state == "thinking" and detail:
        return f"thinking ({detail})"
    return state


def _ctx_bar(used: float, budget: float) -> tuple[str, str]:
    """Return (bar_string, percent_string) for the context bar."""
    if budget <= 0:
        return "▰" * _BAR_CELLS, "100%"
    frac = max(0.0, min(1.0, used / budget))
    filled = int(round(frac * _BAR_CELLS))
    bar = "▰" * filled + "▱" * (_BAR_CELLS - filled)
    return bar, f"{int(round(frac * 100))}%"


def render_line(snap: dict, term_width: int = 80) -> str:
    """Render the one-row status line from a :meth:`StatusBar.snapshot` dict.

    Pure & side-effect free.  ``snap`` keys:
      model, context_used, context_budget, tok_per_s, turn, agents[]
    Each entry of ``agents`` is an :meth:`AgentStatus.snapshot` dict.
    """
    model = snap.get("model") or "?"
    model = str(model).rsplit("/", 1)[-1]

    agents = snap.get("agents") or []
    # Assistant is the primary actor (first registered / id "assistant").
    primary = None
    for a in agents:
        if a["id"] == "assistant":
            primary = a
            break
    if primary is None and agents:
        primary = agents[0]

    mode = _state_label(primary["state"], primary["detail"]) if primary else "idle"

    bar, pct = _ctx_bar(snap.get("context_used", 0),
                        snap.get("context_budget", 0))
    used_h = _humanise(snap.get("context_used", 0))
    budget_h = _humanise(snap.get("context_budget", 0))
    tokseg = f"{snap.get('tok_per_s', 0.0):.1f} tok/s"

    turn = snap.get("turn")
    turnseg = f"turn {turn}"
    if primary is not None and primary.get("last_tool"):
        turnseg += f" (tool: {primary['last_tool']})"

    busy = [a for a in agents if a["state"] not in ("idle", "done")]
    total = len(agents)
    agentsseg = f"agents {len(busy)}/{total}" if total else "agents 0/0"

    # Per-agent chips for non-primary busy agents.
    chips = []
    if len(agents) > 1:
        now = time.monotonic()
        for a in agents:
            if a is primary or a["state"] in ("idle", "done"):
                continue
            label = a["label"]
            elapsed = max(0.0, now - a["since"])
            if a["state"] == "tool":
                chips.append(f"{label} tool:{a['detail']} {elapsed:.1f}s")
            elif a["state"] == "thinking":
                chips.append(f"{label} thinking {a['tokens_seen']} tok")
            else:
                chips.append(f"{label} {a['state']}")
    chipseg = (" · ".join(chips)) if chips else ""

    # Assemble priority-ordered segments.  The base line always includes
    # model, mode, context bar+% and tokens; tok/s, turn, agents and chips are
    # dropped from the right when the width is exceeded.
    core = [
        model,
        mode,
        f"context {bar} {pct}",
        f"{used_h}/{budget_h} tok",
    ]
    rest = [seg for seg in [tokseg, turnseg, agentsseg, chipseg] if seg]

    def _join(segs):
        return " · ".join(segs)

    # Greedily keep right-hand segments while the line fits.
    kept = []
    for seg in reversed(rest):
        candidate = core + list(reversed(kept + [seg]))
        if len(_join(candidate)) <= term_width:
            kept = [seg] + kept
        else:
            # Stop once the first (rightmost) segment doesn't fit; the
            # remaining (more-priority) segments are also dropped because we
            # walk from least-priority to most.
            break
    line = _join(core + kept)
    return _truncate(line, term_width)


class StatusBar:
    """Thread-safe registry of :class:`AgentStatus` records."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agents: dict[str, AgentStatus] = {}
        # Context / throughput fields updated by hook sites.
        self._context_used = 0.0
        self._context_budget = 0.0
        self._tok_per_s = 0.0
        self._turn = 0
        self._model = ""
        # Live token accounting (rolling 1s window) -- updated by
        # ``bump_tokens`` from the stream-token hook.
        self._token_events: list[float] = []

    # -- registration ------------------------------------------------------
    def register(self, agent_id: str, label: str | None = None) -> None:
        with self._lock:
            if agent_id not in self._agents:
                self._agents[agent_id] = AgentStatus(agent_id, label)

    def unregister(self, agent_id: str) -> None:
        with self._lock:
            self._agents.pop(agent_id, None)

    def agent_ids(self) -> list[str]:
        with self._lock:
            return list(self._agents)

    # -- state transitions (call from hook sites) --------------------------
    def set_state(self, agent_id: str, state: str,
                  detail: str = "") -> None:
        if state not in STATES:
            state = "idle"
        with self._lock:
            a = self._agents.get(agent_id)
            if a is None:
                a = AgentStatus(agent_id, agent_id)
                self._agents[agent_id] = a
            if a.state != state:
                a.since = time.monotonic()
            a.state = state
            a.detail = detail
            if state == "tool" and detail:
                a.last_tool = detail

    def bump_tokens(self, agent_id: str = "assistant",
                    n: int = 1) -> None:
        """Record ``n`` streamed tokens (for live tok/s + per-agent count)."""
        if n <= 0:
            return
        now = time.monotonic()
        with self._lock:
            a = self._agents.get(agent_id)
            if a is not None:
                a.tokens_seen += n
            self._token_events.append(now)
            # Prune to a rolling 1s window.
            cutoff = now - 1.0
            while self._token_events and self._token_events[0] < cutoff:
                self._token_events.pop(0)

    def set_turn(self, turn: int) -> None:
        with self._lock:
            self._turn = turn

    def set_context(self, used: float, budget: float) -> None:
        with self._lock:
            self._context_used = used
            self._context_budget = budget

    def set_model(self, model: str) -> None:
        with self._lock:
            self._model = model

    # -- reads (ticker / /status) ------------------------------------------
    def _tok_rate_locked(self) -> float:
        if not self._token_events:
            return 0.0
        now = time.monotonic()
        cutoff = now - 1.0
        recent = [t for t in self._token_events if t >= cutoff]
        if not recent:
            return 0.0
        return float(len(recent))  # one event per token

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "model": self._model,
                "context_used": self._context_used,
                "context_budget": self._context_budget,
                "tok_per_s": self._tok_rate_locked(),
                "turn": self._turn,
                "agents": [a.snapshot() for a in self._agents.values()],
            }

    # -- convenience -------------------------------------------------------
    @property
    def busy_count(self) -> int:
        with self._lock:
            return sum(1 for a in self._agents.values()
                       if a.state not in ("idle", "done"))

    @property
    def total_count(self) -> int:
        with self._lock:
            return len(self._agents)


def _enabled() -> bool:
    """The bar/ticker is only active on a real TTY with color output."""
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


# Module-level singleton.  Hook sites (agent, team, ...) grab this and set
# state on it; the ticker (Phase 2) and the /status command read snapshots.
bar = StatusBar()
