"""Client-side designs for the C=8 saturation tests.

Two designs drive the SAME calibrated ``EngineSim``:

* ``legacy`` -- today's nbchat mapping: one worker per agent session,
  the worker pinned to its session.  While a turn is in flight the
  worker's lane is busy; afterwards the worker's behaviour depends on
  ``held_gap``:

  - ``held_gap=False`` (stream closed after the turn): the lane is
    released during the tool gap -- the engine can backfill it.
  - ``held_gap=True`` (the SSE stream stays open across the tool gap,
    as nbchat does while a tool runs): the lane stays RESERVED and no
    backfill is possible -- this is the wedge under test.

  TUI filler turns may grab any free lane (``reserve`` lanes of
  headroom is the mitigation knob).

* ``dispatcher`` -- the proposed design: one shared ready list plus one
  due-time heap; a single dispatcher submits whatever is due whenever a
  lane is free.  Tool I/O never holds a lane.  Filler is admitted only
  when no agent turn is due within a short grace window, so the agent
  queue never starves.

Sessions are nbchat-shaped: LLM turns separated by tool I/O.  The
visible prompt grows by (output + tool noise) each turn and is
truncated at the ceiling (nbchat windowing); turn 1 prefills the full
visible prompt, later turns prefill only the appended delta (prefix
reuse).
"""

from __future__ import annotations

import heapq
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .sim import EngineSim, Request
from .workload import WorkItem

_EPS = 1e-9
_FILLER_GRACE_S = 0.0   # dispatcher defers filler if an agent is due this soon


@dataclass
class AgentSession:
    """One long-horizon agent session: N LLM turns separated by tool I/O."""
    sid: int
    total_turns: int
    prompt_tokens: int          # base prompt (system + first user)
    turn_tokens: int            # expected output per turn
    tool_gap: float = 0.0       # tool I/O between turns (s), no lane held
    ceiling: int = 16_384       # context ceiling per request
    held_gap: bool = False      # legacy: SSE stream (and lane) held across gap
    tool_noise_tokens: int = 300

    # --- runtime state (mutated by the client) ---
    turn: int = 0
    due: float = 0.0
    done: bool = False
    done_time: Optional[float] = None
    _visible_prompt: int = 0    # prompt tokens currently in the window

    def prefill_tokens(self, turn: int) -> int:
        if turn == 0:
            return self.prompt_tokens
        return self.turn_tokens + self.tool_noise_tokens   # delta only

    def advance(self) -> None:
        """Fold this turn's output + tool noise into the window,
        truncating at the ceiling (room left for the next output)."""
        room = self.ceiling - self.turn_tokens
        self._visible_prompt = min(
            self._visible_prompt + self.turn_tokens + self.tool_noise_tokens,
            room)


@dataclass
class _Item:
    due: float
    seq: int
    kind: str                                # "agent" | "filler"
    prompt_tokens: int
    target_tokens: int
    ceiling: int
    prefill_tokens: int = 0
    session: Optional[AgentSession] = None
    worker: int = -1
    submit_time: Optional[float] = None

    def __lt__(self, other: "_Item") -> bool:
        return self.seq < other.seq


class MultiTurnClient:
    def __init__(self, sim: EngineSim, sessions: List[AgentSession],
                 mode: str, fillers: Optional[List[WorkItem]] = None,
                 reserve: int = 0, max_fillers: int = 2,
                 workers: Optional[int] = None,
                 filler_gap_s: float = 2.5, filler_first_s: float = 5.0) -> None:
        if mode not in ("legacy", "dispatcher"):
            raise ValueError(mode)
        if mode == "legacy" and workers is not None \
                and workers != len(sessions):
            raise ValueError("legacy mode is 1:1 (workers == sessions)")
        self.sim = sim
        self.sessions = sessions
        self.mode = mode
        self._n_workers = len(sessions)          # 1:1 pinned workers
        self.reserve = reserve
        self.max_fillers = max_fillers
        self._inflight: Dict[int, _Item] = {}
        self._seq = 0
        self.turns_done = 0
        self.filler_done = 0
        self.filler_latencies: List[float] = []
        self.first_done_time: Optional[float] = None
        self.agent_done_time: Optional[float] = None
        self._agent_left = sum(s.total_turns for s in sessions)
        self._util: List[Tuple[float, int]] = []
        self._last_util_t = 0.0
        self._filler_gap = filler_gap_s
        self._filler_due_next = filler_first_s

        # --- legacy state per (pinned) worker == per session ---
        self._queues: List[List[_Item]] = [[] for _ in sessions]
        self._w_busy: List[bool] = [False] * self._n_workers
        self._w_holding: List[bool] = [False] * self._n_workers
        for i, s in enumerate(sessions):
            self._queues[i].append(self._agent_item(s, 0.0, i))

        # --- dispatcher: due-heap + shared ready list ---
        self._due: List[Tuple[float, int, AgentSession]] = []
        self._ready: List[_Item] = []
        for s in sessions:
            heapq.heappush(self._due, (0.0, self._next_seq(), s))

        # filler feed
        self._fillers = list(fillers or [])
        self._filler_i = 0
        self._filler_active = 0
        self._filler_rejected = 0

        sim.on_complete = self._on_complete

    # ---------------------------------------------------------------- helpers
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _agent_item(self, s: AgentSession, due: float, worker: int = -1) -> _Item:
        turn = s.turn
        return _Item(due=due, seq=self._next_seq(), kind="agent",
                     prompt_tokens=s._visible_prompt or s.prompt_tokens,
                     target_tokens=s.turn_tokens, ceiling=s.ceiling,
                     prefill_tokens=s.prefill_tokens(turn),
                     session=s, worker=worker)

    def _filler_item(self, due: float) -> _Item:
        f = self._fillers[self._filler_i]
        return _Item(due=due, seq=self._next_seq(), kind="filler",
                     prompt_tokens=f.prompt_tokens,
                     target_tokens=f.target_tokens,
                     ceiling=f.context_ceiling)

    # ---------------------------------------------------------------- submit
    def _submit(self, item: _Item) -> bool:
        req = self.sim.submit(item.prompt_tokens, item.target_tokens,
                              item.ceiling, kind=item.kind,
                              prefill_tokens=item.prefill_tokens)
        if not req.ok:
            if item.kind == "agent" and item.session is not None:
                item.session.due = self.sim.t + 1.0
            return False
        item.submit_time = self.sim.t
        self._inflight[req.rid] = item
        if item.kind == "filler":
            self._filler_active += 1
        return True

    def _try_filler(self, t: float) -> bool:
        if self._filler_i >= len(self._fillers) or t < self._filler_due_next:
            return False
        if self._filler_active >= self.max_fillers:
            return False
        free = self.sim.cfg.C - self.sim.lanes_used
        if free < 1:
            return False
        if self.mode == "legacy":
            # wedge: filler grabs any free lane unless `reserve` lanes
            # of headroom are demanded
            if free <= self.reserve:
                return False
        else:
            # dispatcher: filler only on truly idle lanes -- never while
            # an agent turn is ready, or due (but not yet submitted)
            # within the grace window.  In-flight sessions are exempt:
            # their next due time is in the future, not pending.
            if self._ready:
                return False
            inflight_sids = {it.session.sid for it in self._inflight.values()
                             if it.session is not None}
            if any(not s.done and s.sid not in inflight_sids
                   and s.due <= t + _FILLER_GRACE_S + _EPS
                   for s in self.sessions):
                return False
        if self._submit(self._filler_item(t)):
            self._filler_i += 1
            self._filler_due_next = t + self._filler_gap
            return True
        self._filler_rejected += 1
        return False

    # ---------------------------------------------------------------- events
    def _on_complete(self, req: Request) -> None:
        item = self._inflight.pop(req.rid, None)
        if item is None or not req.ok:
            return
        if item.kind == "filler":
            self.filler_done += 1
            self._filler_active = max(0, self._filler_active - 1)
            if item.submit_time is not None:
                self.filler_latencies.append(req.finish_time - item.submit_time)
            return
        s = item.session
        assert s is not None
        self.turns_done += 1
        self._agent_left -= 1
        s.turn += 1
        s.advance()
        if self.first_done_time is None:
            self.first_done_time = self.sim.t
        next_due = self.sim.t + s.tool_gap
        s.due = next_due
        if s.turn >= s.total_turns:
            s.done = True
            s.done_time = self.sim.t
            self.agent_done_time = self.sim.t
        else:
            self._queues[s.sid].append(self._agent_item(s, next_due, s.sid))
            # dispatcher: arm the due-heap for the next turn
            heapq.heappush(self._due, (next_due, self._next_seq(), s))

        if self.mode == "legacy":
            w = item.worker
            self._w_busy[w] = False
            # held stream: the lane stays reserved until the next turn
            # (submitted by the tick when it comes due); closed stream:
            # the lane is released now and may be backfilled
            self._w_holding[w] = bool(s.held_gap) and not s.done
            sim_reserved(self)

    # ------------------------------------------------------------------ tick
    def _tick(self) -> None:
        t = self.sim.t
        self._try_filler(t)
        if self.mode == "legacy":
            for w in range(self._n_workers):
                q = self._queues[w]
                if not q or self._w_busy[w]:
                    continue
                item = q[0]
                if item.due > t + _EPS:
                    continue
                if self._submit(item):
                    q.pop(0)
                    self._w_busy[w] = True
                    self._w_holding[w] = False
            sim_reserved(self)
        else:
            while self._due and self._due[0][0] + _EPS <= t:
                due, _seq, s = heapq.heappop(self._due)
                if not s.done:
                    self._ready.append(self._agent_item(s, due))
            while self._ready:
                if self.sim.lanes_used >= self.sim.cfg.C:
                    break
                item = self._ready.pop(0)
                if self._submit(item):
                    continue
                heapq.heappush(self._due, (self.sim.t, item.seq,
                                           item.session))
                break

    # ------------------------------------------------------------------ drive
    def run(self) -> float:
        """Drive until every session is done; return the last agent
        completion time (the client-side makespan for the agents)."""
        started = time.monotonic()
        while not all(s.done for s in self.sessions) or self._inflight:
            self._tick()
            self._sample_util()
            if all(s.done for s in self.sessions) and not self._inflight:
                break
            if time.monotonic() - started > 15.0:
                raise RuntimeError(
                    f"client run did not converge at sim t={self.sim.t:.1f} "
                    f"({self._agent_left} agent turns undone)")
            cands = [s.due for s in self.sessions
                     if not s.done and s.due > self.sim.t + _EPS]
            if self.mode == "legacy":
                cands += [it.due for q in self._queues for it in q
                          if it.due > self.sim.t + _EPS]
            if self._filler_i < len(self._fillers):
                cands.append(self._filler_due_next)
            tgt = min(cands) if cands else self.sim.t + 20.0
            if tgt <= self.sim.t + _EPS:
                tgt = self.sim.t + 20.0
            self.sim.advance_to(tgt + 0.001)
            if self.sim.quiescent and not self._inflight and cands:
                # engine idle while work is due: only legitimate during
                # tool gaps (nothing runnable); nudge to the next due time
                self.sim.advance_to(min(cands) + 0.001)
        if self.agent_done_time is None:
            self.agent_done_time = max(
                (s.done_time for s in self.sessions if s.done_time),
                default=self.sim.t)
        self._sample_util()
        return self.agent_done_time

    def _sample_util(self) -> None:
        if self.sim.t - self._last_util_t >= 0.5:
            self._util.append((self.sim.t, self.sim.lanes_used))
            self._last_util_t = self.sim.t


def sim_reserved(client: MultiTurnClient) -> None:
    """Legacy only: lanes reserved by held (open-but-idle) streams."""
    if client.mode == "legacy":
        client.sim.reserved_lanes = sum(client._w_holding)
