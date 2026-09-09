"""Client-side decode-lane gate (prime-agent "max concurrent agents" port).

The whole reason nbchat exists is to keep the 5090's fixed KV pool busy:
decode throughput scales with the number of *concurrent* in-flight
generations up to a ceiling (measured ~8 on this box, see
``bench/THROUGHPUT_FINDINGS.md`` and ``docs/multi-agent-framework-for-
c8-saturation.md``). Without a gate, a burst of LLM calls (workers +
planner + subtasks + synthesis) can momentarily overshoot the ceiling,
waste prefill on context the KV pool cannot hold, and thrash.

The LaneGate caps the number of *concurrent in-flight generations*
originating from this process to ``LANE_GATE_LIMIT`` (default:
``TEAM_MAX_WORKERS``). Non-team flows issue at most one concurrent
request, so they are never throttled; team runs are held to the ceiling.

Two correctness details matter:

* **Streaming holds the permit for the whole decode.** The OpenAI SDK's
  ``create(stream=True)`` returns an iterator almost instantly; the tokens
  arrive *lazily* as the caller iterates. Releasing the permit right after
  ``create()`` would cap concurrent *connections*, not concurrent
  *decodes*. So for streams the permit is transferred to
  :class:`~nbchat.core.client._InstrumentedStream` and released when the
  stream is exhausted, closed, or errors.
* **Idempotent release + timeout safety valve.** A permit is released at
  most once (a stream may both exhaust *and* be explicitly closed). If a
  permit cannot be acquired within ``LANE_GATE_ACQUIRE_TIMEOUT`` the caller
  proceeds ungated with a logged warning: a brief over-saturation is far
  better than deadlocking the whole team.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# Lazily-initialised process-wide bounded semaphore.  Lazy because the limit
# comes from config (which must not be imported at module-load time in every
# context).
_lock = threading.Lock()
_sem: threading.BoundedSemaphore | None = None
_limit: int = 0


def _semaphore() -> tuple[threading.BoundedSemaphore, int]:
    global _sem, _limit
    with _lock:
        if _sem is None:
            from .config import LANE_GATE_LIMIT, LANE_GATE_ACQUIRE_TIMEOUT
            _limit = max(1, int(LANE_GATE_LIMIT))
            _sem = threading.BoundedSemaphore(_limit)
        assert _sem is not None
        return _sem, _limit


class GateHandle:
    """One acquired permit with idempotent release.

    ``None`` is returned by :func:`acquire` when the gate is disabled or
    could not be acquired in time (in which case no release is owed).
    """

    __slots__ = ("_sem", "_released")

    def __init__(self, sem: threading.BoundedSemaphore) -> None:
        self._sem = sem
        self._released = False

    def release(self) -> None:
        """Release the permit at most once (safe to call from any path)."""
        if not self._released:
            self._released = True
            self._sem.release()


def acquire() -> GateHandle | None:
    """Acquire a decode-lane permit (or ``None`` if ungated).

    Returns ``None`` (and imposes no limit) when the gate is disabled. On
    the (rare) path where the permit is not available within the timeout,
    returns ``None`` and logs a warning rather than blocking indefinitely.
    """
    try:
        from .config import LANE_GATE_ENABLED, LANE_GATE_ACQUIRE_TIMEOUT
    except Exception:
        return None
    if not LANE_GATE_ENABLED:
        return None
    sem, _lim = _semaphore()
    timeout = max(0.0, float(LANE_GATE_ACQUIRE_TIMEOUT))
    if timeout > 0 and not sem.acquire(blocking=True, timeout=timeout):
        logger.warning(
            "lane_gate: no permit within %.0fs; proceeding ungated (brief "
            "over-saturation possible)", timeout)
        return None
    return GateHandle(sem)


def limit() -> int:
    """The configured permit count (for logging / metrics)."""
    try:
        from .config import LANE_GATE_LIMIT
        return max(1, int(LANE_GATE_LIMIT))
    except Exception:
        return 0
