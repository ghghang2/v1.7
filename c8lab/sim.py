"""Analytic discrete-event simulator of the ninfer-serve engine.

Phase model: between structural changes (a prefill finishing, a request
completing, a KV demotion, a host-tier resume) the decode cohort is
static, so every active lane emits tokens at one rate
``decode_base * batch^-batch_exp``.  A step advances to the next
structural event in closed form instead of per token.

Calibration anchors (workspace/ninfer/docs/performance.md, Qwen3.8-27B
NVFP4, MTP3, RTX 5090, corpus 2026-08-17):

    C  makespan (s)  aggregate decode tok/s  avg batch
    1  4,670.27      161.1                   1.00
    2  2,510.78      294.7                   1.98
    4  1,647.74      432.9                   3.29
    8  2,164.90      334.2                   2.36   (KV pool 187,712)

Mechanics modeled (verified against src/runtime/engine/):

* C decode lanes; prefill is serialized on one lane (single
  ``prefill_lane_`` in scheduler.h) while the other lanes keep
  decoding.
* KV pool ``kv_tokens``: the sum of live decode contexts must fit.
  Overflow demotes the largest contexts to the 8 GiB host tier
  (``--host-kv-mib 8192``): a demoted request pauses, holds no device
  context (counting toward neither pool nor batch), and resumes after a
  re-fault pause when space and a lane free up.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List, Optional, Tuple

_EPS = 1e-9


@dataclass
class ServerConfig:
    C: int
    kv_tokens: int
    prefill_tps: float = 11_000.0
    decode_base: float = 161.0   # per-lane tok/s at batch 1 (measured C=1)
    batch_exp: float = 0.145     # per-lane rate ~= base * batch^-exp
    demote_pause: float = 0.60   # host re-fault pause before resume (s)
    max_context: int = 131_072
    max_pending: int = 8

    @classmethod
    def nvfp4(cls, C: int) -> "ServerConfig":
        """Measured ``--kv-capacity auto`` resolutions (docs table).

        C=1/2/4 are not published; they are interpolated from the
        C=4 auto value (270,000) using the same bytes/KV-token slope as
        the measured C=4 -> C=8 drop.
        """
        pool = {1: 111_000, 2: 187_000, 4: 270_000, 8: 187_712}[C]
        return cls(C=C, kv_tokens=pool)

    @classmethod
    def groupwise(cls, C: int) -> "ServerConfig":
        pool = {1: 131_072, 2: 262_144, 4: 341_952, 8: 313_984}[C]
        return cls(C=C, kv_tokens=pool)


@dataclass
class Request:
    rid: int
    prompt_tokens: int
    target_tokens: int
    context_ceiling: int
    produced: int = 0
    ok: bool = True
    error: str = ""
    kind: str = ""
    submit_time: float = 0.0
    finish_time: float = 0.0
    prefill_tokens: int = 0          # cost to prefill (prefix reuse trims it)

    def __post_init__(self) -> None:
        if self.prefill_tokens <= 0:
            self.prefill_tokens = self.prompt_tokens

    @property
    def context(self) -> int:
        # The context ceiling caps the TOTAL (prompt + output), so the
        # output budget is the ceiling minus the prompt (engine admission
        # plans prompt + output against the ceiling).
        planned = min(self.target_tokens,
                      max(0, self.context_ceiling - self.prompt_tokens))
        return self.prompt_tokens + min(self.produced, planned)


class EngineSim:
    """One server.  ``submit`` admits a request (prefill queued FIFO);
    the client drives time with ``advance_to`` and receives completions
    via ``on_complete`` (callback may call ``submit`` again)."""

    def __init__(self, cfg: ServerConfig, rng: Optional[random.Random] = None):
        self.cfg = cfg
        self._probe = None
        self._probe_interval = 0.5
        self.rng = rng or random.Random(0)
        self.t = 0.0
        self._rid = 0
        self._pending: List[Request] = []            # admitted, awaiting prefill
        self._pre: Optional[Tuple[Request, float]] = None  # (req, remaining)
        self._active: List[Tuple[Request, float]] = []     # (req, remaining)
        self._demoted: List[Request] = []
        self._resume_at: Optional[float] = None
        self.results: List[Request] = []
        self.submits = 0
        self.lane_active_seconds = 0.0
        self.degradations = 0
        self.max_pending_depth = 0
        self.oom_fails = 0
        self._completes: List[callable] = []
        # Lanes consumed by OPEN but idle streams (nbchat legacy workers
        # hold the SSE connection across tool I/O).  The client sets this;
        # it counts against the C-lane cap exactly like an active lane.
        self.reserved_lanes = 0

    @property
    def on_complete(self) -> Optional[callable]:
        return self._completes[-1] if self._completes else None

    @on_complete.setter
    def on_complete(self, fn: Optional[callable]) -> None:
        if fn is not None:
            self._completes.append(fn)

    # ------------------------------------------------------------------ api
    @property
    def lanes_used(self) -> int:
        return len(self._active) + (1 if self._pre else 0) + self.reserved_lanes

    @property
    def quiescent(self) -> bool:
        return not (self._pending or self._pre or self._active
                    or self._demoted)

    def pool_used(self) -> int:
        return sum(r.context for r, _rem in self._active)

    def submit(self, prompt_tokens: int, target_tokens: int,
               context_ceiling: int, kind: str = "",
               prefill_tokens: int = 0) -> Request:
        req = Request(self._rid, prompt_tokens, target_tokens,
                      context_ceiling)
        req.prefill_tokens = prefill_tokens or prompt_tokens
        req.kind = kind
        self._rid += 1
        req.submit_time = self.t
        self.submits += 1
        if (target_tokens <= 0
                or prompt_tokens > min(context_ceiling, self.cfg.max_context)):
            self._fail(req, "context ceiling")
            return req
        # The real engine queues non-admissible requests in the FIFO
        # pending queue until a round boundary frees capacity (or the
        # deadline expires) -- it does not reject them (engine_core.h
        # try_admit_one: wait, not reject).  We model that: no rejection
        # here, only depth observability.
        self._pending.append(req)
        self.max_pending_depth = max(self.max_pending_depth,
                                     len(self._pending))
        self._try_start_prefill()
        return req

    def _fail(self, req: Request, why: str) -> None:
        req.ok = False
        req.error = why
        req.finish_time = self.t
        self.results.append(req)
        self.oom_fails += 1
        for cb in list(self._completes):
            cb(req)

    def advance_to(self, t_end: float) -> None:
        steps = 0
        next_probe = 0.0
        while self.t < t_end - _EPS:
            steps += 1
            if steps > 2_000_000:
                raise RuntimeError("advance_to did not converge")
            nxt = self._next_event_time()
            t_new = t_end if nxt is None else min(t_end, nxt)
            if t_new <= self.t + _EPS:
                t_new = t_end
            self._step(t_new)
            if self._probe is not None:
                while next_probe <= self.t + _EPS:
                    self._probe(self.t, self.lanes_used, self.pool_used())
                    next_probe += self._probe_interval

    def _rate(self, batch: int) -> float:
        if batch <= 0:
            return 0.0
        return self.cfg.decode_base * (batch ** -self.cfg.batch_exp)

    # ------------------------------------------------------------ event time
    def _next_event_time(self) -> Optional[float]:
        times: List[float] = []
        if self._pre:
            times.append(self._pre[1])
        if self._active:
            r = self._rate(len(self._active))
            if r > 0:
                times.append(self.t + min(rem for _, rem in self._active) / r)
                usage = self.pool_used()
                if usage < self.cfg.kv_tokens:
                    growth = len(self._active) * r  # pool tokens per second
                    times.append(self.t + (self.cfg.kv_tokens - usage) / growth)
        if self._resume_at is not None:
            times.append(self._resume_at)
        return min(times) if times else None

    # ----------------------------------------------------------------- step
    def _step(self, t_new: float) -> None:
        # prefill completion first (frees its lane, adds a decode lane)
        if self._pre is not None and t_new >= self._pre[1] - _EPS:
            req, end = self._pre
            self._pre = None
            self.t = end
            self._enter_decode(req)
        dt = t_new - self.t
        self.t = t_new

        # decode progress, clamped so no lane passes its completion
        if self._active:
            r = self._rate(len(self._active))
            finish_dt = min(rem for _, rem in self._active) / r if r > 0 else float("inf")
            dt_eff = min(dt, finish_dt)
            for i, (req, rem) in enumerate(self._active):
                new_rem = rem - r * dt_eff
                self._active[i] = (req, new_rem)
                req.produced = int(req.target_tokens - new_rem)
            self.lane_active_seconds += len(self._active) * dt_eff
            if self.pool_used() > self.cfg.kv_tokens:
                self._demote_overflow()
            done = [req for req, rem in self._active if rem <= _EPS]
            if done:
                for req in done:
                    self._finish_decode(req)
                    self._active = [(q, rem) for q, rem in self._active
                                    if q is not req]

        self._try_start_prefill()
        self._try_resume()

    # ------------------------------------------------------------- transitions
    def _enter_decode(self, req: Request) -> None:
        self._active.append((req, float(req.target_tokens)))
        if self.pool_used() > self.cfg.kv_tokens:
            self._demote_overflow()

    def _demote_overflow(self) -> None:
        """Demote largest contexts until the pool fits (host tier)."""
        while self.pool_used() > self.cfg.kv_tokens and self._active:
            idx = max(range(len(self._active)),
                      key=lambda i: self._active[i][0].context)
            req, _rem = self._active.pop(idx)
            self._demoted.append(req)
            self.degradations += 1

    def _finish_decode(self, req: Request) -> None:
        req.produced = req.target_tokens
        req.finish_time = self.t
        self.results.append(req)
        for cb in list(self._completes):
            cb(req)

    def _try_start_prefill(self) -> None:
        if self._pre or not self._pending:
            return
        if self.lanes_used >= self.cfg.C:
            return
        req = self._pending[0]
        if (self.pool_used() + req.prompt_tokens > self.cfg.kv_tokens
                and self._active):
            return  # wait for room, as the engine does
        self._pending.pop(0)
        self._pre = (req, self.t + req.prefill_tokens / self.cfg.prefill_tps + 0.004)

    def _try_resume(self) -> None:
        if not self._demoted:
            self._resume_at = None
            return
        if self.lanes_used >= self.cfg.C:
            return
        # largest demoted first (it left the pool earliest)
        self._demoted.sort(key=lambda r: -r.context)
        cand = self._demoted[0]
        if self.pool_used() + cand.context > self.cfg.kv_tokens:
            return
        if self._resume_at is None:
            self._resume_at = self.t + self.cfg.demote_pause
            return
        if self.t >= self._resume_at - _EPS:
            self._resume_at = None
            self._demoted.pop(0)
            self._active.append((cand,
                                 float(cand.target_tokens - cand.produced)))
            if self.pool_used() > self.cfg.kv_tokens:
                self._demote_overflow()
