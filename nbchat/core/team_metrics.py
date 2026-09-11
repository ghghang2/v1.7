"""Automatic measurement recording for /team runs.

Implements the Step-2 measurement requirement from
``docs/c8-saturation-assessment.md``: every ``/team`` run writes a small,
append-only JSONL capture so that worker-count experiments (C=4 vs C=8)
can be evaluated against the c8lab simulation *without* manual timing.

Recorded per run (one file ``logs/team_metrics/<run_id>.jsonl``):

* ``task``  -- per-task row: status, claimed/finished timestamps and the
  wall-clock duration each task spent claimed (the "per-task times" the
  assessment asks for).
* ``sample``-- a periodic (~1 Hz) queue-state row: ``inflight`` (workers
  currently executing), ``pending``, ``claimed``.  The time series over
  ``inflight`` *is* the lane-utilisation trace; ``pending`` *is* the
  queue-depth trace (the server has no live queue-depth endpoint, so this
  is captured client-side, which is exactly what /team controls).
* ``gpu``   -- ``nvidia-smi`` GPU-memory sample alongside each ``sample``
  row.  With a fixed server KV pool, the high-water of
  ``memory_used_mib`` across the run is the KV-pool high-water proxy the
  assessment calls for.
* ``summary``-- one final row: wall-clock makespan, configured worker
  count, per-task durations, peak/mean inflight, peak pending and peak GPU
  memory.

The recorder is deliberately defensive: any failure (no ``nvidia-smi``,
read-only fs, ...) degrades to a note on the summary row and never
propagates out of the measurement path or disturbs the run itself.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path


# How often (seconds) the sampler thread polls queue depth + GPU memory.
# ~1 Hz is enough resolution for a makespan of minutes and keeps the
# capture small (a 900 s run yields <= ~900 rows).
_SAMPLE_INTERVAL = 1.0

# How long to block waiting for ``nvidia-smi`` (it is a short-lived call;
# a hard bound keeps a wedged driver from stalling the sampler thread).
_GPU_TIMEOUT = 2.0

_METRICS_DIRNAME = Path("logs") / "team_metrics"


class _TokenMeter:
    """Thread-safe cumulative token/call counter for one /team run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.total_tokens = 0
        self.calls = 0

    def add(self, n) -> None:
        if not n:
            return
        with self._lock:
            self.total_tokens += int(n)
            self.calls += 1


# Process-global slot for the currently-recording /team run's token meter.
# The main terminal agent is idle while a /team run executes (the TUI is
# blocked on run()), so at most one meter is active at a time; the client
# reports each completion's token count into it via :func:`record_tokens`.
_ACTIVE_METER: _TokenMeter | None = None

# Process-global meter for the MAIN agent (the current TUI instance), separate
# from the /team run's meter.  Every LLM completion reports its token count
# here (as well as the active team meter, when a /team run is recording), so
# a per-session ``/budget`` view (tui3 Phase 3) can read the ACTUAL token
# usage instead of an estimate.  It is process-global (one per TUI instance),
# which is a close proxy for the current session.
_MAIN_METER: _TokenMeter = _TokenMeter()


def _set_active_meter(meter: _TokenMeter | None) -> None:
    global _ACTIVE_METER
    _ACTIVE_METER = meter


def record_tokens(total_tokens) -> None:
    """Add *total_tokens* to the active run's meter and the main-agent meter.

    The main-agent meter always accumulates (it backs the tui3 ``/budget``
    view); the team run's meter only accumulates when a /team run is
    recording.
    """
    _MAIN_METER.add(total_tokens)
    meter = _ACTIVE_METER
    if meter is not None:
        meter.add(total_tokens)


def main_token_stats() -> dict:
    """Return the main-agent token usage for this process.

    A small dict ``{"total_tokens": int, "calls": int}`` (the LLM completions
    that reported a token count).  Used by the tui3 ``/budget`` command.
    """
    with _MAIN_METER._lock:
        return {"total_tokens": _MAIN_METER.total_tokens,
                "calls": _MAIN_METER.calls}


def reset_main_tokens() -> None:
    """Reset the main-agent token meter (used by ``/budget reset``)."""
    with _MAIN_METER._lock:
        _MAIN_METER.total_tokens = 0
        _MAIN_METER.calls = 0


def _gpu_used_mib() -> float | None:
    """Return GPU memory used (MiB) from ``nvidia-smi``, or ``None``.

    Reads ``memory.used`` for the first device.  ``None`` when the tool
    is absent or fails -- the caller records a sample without a GPU field
    rather than dropping the whole row.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.used",
             "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=_GPU_TIMEOUT)
        if out.returncode != 0:
            return None
        line = out.stdout.decode("utf-8", "replace").strip().splitlines()
        if not line:
            return None
        return float(line[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


class TeamRunMetrics:
    """Record the measurements a saturation assessment needs for one run.

    Intended use::

        rec = TeamRunMetrics(run_id, max_workers)
        queue = TaskQueue(tasks)
        rec.attach(queue)          # per-task start/finish capture
        rec.start()                # begin queue-depth / GPU sampling
        ... pool.run() ...
        rec.stop(makespan)         # stop sampling, write summary + close

    ``attach`` rebinds ``queue.claim`` / ``queue.wait_claim`` with
    duration-stamping wrappers; it is idempotent per queue.
    """

    def __init__(self, run_id: str, max_workers: int) -> None:
        self.run_id = run_id
        self.max_workers = int(max_workers)
        self._dir = _METRICS_DIRNAME
        self._path: Path | None = None
        self._fh = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._queue = None
        self._queue_lock = None
        # task_id -> {"started": ts, "claimed": ts}
        self._task_start: dict = {}
        self._task_rows: dict = {}
        self._peak_inflight = 0
        self._peak_pending = 0
        self._inflight_samples = 0
        self._inflight_sum = 0
        self._peak_gpu = 0.0
        self._gpu_seen = False
        self._gpu_error = False
        self._sample_count = 0
        self._start_mono = None
        self._stop_mono = None
        self._summary = None
        self._tokens = _TokenMeter()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._start_mono = time.monotonic()
        _set_active_meter(self._tokens)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path = self._dir / f"{self.run_id}.jsonl"
            self._fh = open(self._path, "a", encoding="utf-8")
            self._write({
                "type": "run_start",
                "run_id": self.run_id,
                "max_workers": self.max_workers,
                "ts": time.time(),
            })
        except OSError as exc:
            # Non-fatal: measurement is best-effort.  Remember why the
            # capture is absent so the summary row is honest.
            self._path = None
            self._fh = None
            self._gpu_error = True
            self._gpu_err_msg = f"metrics file unavailable: {exc}"
            # Still run the sampler so in-memory stats (used in the
            # summary row even when unflushed) are populated.
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._sample_loop, daemon=True,
            name=f"nbchat-team-metrics-{self.run_id}")
        self._thread.start()

    def stop(self, makespan: float) -> Path | None:
        """Stop sampling, flush the summary row and close the file.

        Returns the capture path (or ``None`` if it could not be opened).
        Safe to call more than once.
        """
        if self._stop.is_set() and self._thread is None:
            return self._path
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=_SAMPLE_INTERVAL + _GPU_TIMEOUT)
            self._thread = None
        self._stop_mono = time.monotonic()
        duration = (self._stop_mono - self._start_mono) \
            if self._start_mono is not None else makespan
        total_tokens = self._tokens.total_tokens
        _set_active_meter(None)
        summary = {
            "type": "summary",
            "run_id": self.run_id,
            "makespan_s": round(makespan, 2),
            "wall_duration_s": round(duration, 2),
            "max_workers": self.max_workers,
            "total_tokens": total_tokens,
            "token_calls": self._tokens.calls,
            "throughput_tok_s": (
                round(total_tokens / makespan, 1) if makespan > 0 else 0.0),
            # Decode-only rate: completion (output) tokens / makespan.  This
            # is the figure that actually scales with concurrency --
            # total_tokens is dominated by re-sent prompt tokens that grow with
            # task count rather than with concurrency, so it understates the
            # effect of pushing workers from 4 to 8 (see
            # docs/multi-agent-framework-for-c8-saturation.md, sec 8).
            "completion_tokens": self._tokens.completion_tokens,
            "decode_tok_s": (
                round(self._tokens.completion_tokens / makespan, 1)
                if makespan > 0 else 0.0),
            "peak_inflight": self._peak_inflight,
            "mean_inflight": (
                round(self._inflight_sum / self._inflight_samples, 2)
                if self._inflight_samples else 0.0),
            "utilization": (
                round(self._inflight_sum / (self._inflight_samples
                                            * self.max_workers), 3)
                if self._inflight_samples and self.max_workers else 0.0),
            "peak_pending": self._peak_pending,
            "peak_gpu_mem_mib": (
                round(self._peak_gpu, 1) if self._gpu_seen else None),
            "samples": self._sample_count,
            "task_rows": self._task_rows,
            "ts": time.time(),
        }
        if self._gpu_seen is False:
            summary["gpu_note"] = (self._gpu_err_msg
                                   if self._gpu_error
                                   else "nvidia-smi unavailable "
                                        "(GPU/KV high-water not captured)")
        self._summary = summary
        self._write(summary)
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except OSError:
                pass
            try:
                self._fh.close()
            finally:
                self._fh = None
        return self._path

    # -- queue wiring -----------------------------------------------------

    @property
    def summary_line(self) -> str:
        """One-line throughput summary for the coordinator report.

        Returns an empty string until :meth:`stop` has written the summary
        row.  This is the single most useful number for the C=8 objective:
        how many decode tokens per second the fixed-KV-pool server produced
        over the whole run.
        """
        m = self._summary
        if not m:
            return ""
        parts = [
            f"{m['total_tokens']} tokens / {m['makespan_s']}s",
            f"= {m['throughput_tok_s']} tok/s",
            f"decode {m.get('decode_tok_s', 0.0)} tok/s",
            f"peak in-flight {m['peak_inflight']}/{m['max_workers']}",
        ]
        gpu = m.get("peak_gpu_mem_mib")
        if gpu is not None:
            parts.append(f"peak GPU {round(gpu/1024, 1)} GiB")
        return "throughput: " + ", ".join(parts)


    def attach(self, queue) -> None:
        """Bind per-task start capture to a :class:`TaskQueue`."""
        self._queue = queue
        self._queue_lock = queue._lock
        claim = queue.claim
        wait_claim = queue.wait_claim

        def _stamp(tid):
            if tid is None:
                return tid
            with self._lock:
                self._task_start.setdefault(tid, {
                    "claimed": time.monotonic(),
                    "ts": time.time()})
            return tid

        def claim_w():
            return _stamp(claim())

        def wait_claim_w(timeout=None):
            return _stamp(wait_claim(timeout))

        queue.claim = claim_w
        queue.wait_claim = wait_claim_w

    def task_finished(self, task) -> None:
        """Record a task's duration once it reaches a terminal state."""
        with self._lock:
            info = self._task_start.get(task.task_id)
            if info is None or task.task_id in self._task_rows:
                return
            end = time.monotonic()
            self._task_rows[task.task_id] = {
                "status": task.status,
                "duration_s": round(end - info["claimed"], 2),
            }
        self._write({
            "type": "task",
            "run_id": self.run_id,
            "task_id": task.task_id,
            "status": task.status,
            "duration_s": self._task_rows[task.task_id]["duration_s"],
            "ts": time.time(),
        })

    # -- sampler ----------------------------------------------------------

    def _inflight_pending(self):
        """Read (inflight, pending) without deadlocking the queue.

        ``inflight`` (workers currently executing) is the pool's
        ``_inflight`` counter when a pool is attached, else the count of
        claimed tasks.  ``pending`` is the count of pending tasks.  Both
        are read under a single lock so a sample is a consistent
        snapshot.  Counts are computed inline (not via ``pending_count``)
        because that helper takes the queue lock itself.
        """
        pool_cv = getattr(self, "_pool_cv", None)
        pool_inflight = getattr(self, "_pool_inflight", None)
        if pool_cv is not None and self._queue is not None:
            with pool_cv:
                inflight = pool_inflight()
                pending = sum(1 for t in self._queue._tasks.values()
                              if t.status == "pending")
            return inflight, pending
        if self._queue is not None and self._queue_lock is not None:
            with self._queue_lock:
                inflight = sum(1 for t in self._queue._tasks.values()
                               if t.status == "claimed")
                pending = sum(1 for t in self._queue._tasks.values()
                              if t.status == "pending")
            return inflight, pending
        return 0, 0

    def attach_pool(self, pool) -> None:
        """Prefer the pool's exact ``_inflight`` for the sample rows."""
        self._pool_cv = pool._cv
        self._pool_inflight = (lambda: pool._inflight)

    def _sample_loop(self) -> None:
        while not self._stop.wait(_SAMPLE_INTERVAL):
            try:
                inflight, pending = self._inflight_pending()
                gpu = _gpu_used_mib()
                with self._lock:
                    self._sample_count += 1
                    self._peak_inflight = max(self._peak_inflight, inflight)
                    self._peak_pending = max(self._peak_pending, pending)
                    self._inflight_sum += inflight
                    self._inflight_samples += 1
                    if gpu is None:
                        self._gpu_error = True
                    else:
                        self._gpu_seen = True
                        self._peak_gpu = max(self._peak_gpu, gpu)
                row = {
                    "type": "sample",
                    "t": round(time.monotonic() - self._start_mono, 2),
                    "inflight": inflight,
                    "pending": pending,
                    "claimed": inflight,
                    "gpu_mem_mib": (round(gpu, 1) if gpu is not None
                                    else None),
                    "ts": time.time(),
                }
                self._write(row)
            except Exception:
                # The sampler must never take the run down with it.
                continue

    # -- io ---------------------------------------------------------------

    def _write(self, row: dict) -> None:
        with self._lock:
            fh = self._fh
        if fh is None:
            return
        try:
            fh.write(json.dumps(row, default=str) + "\n")
            fh.flush()
        except (OSError, TypeError, ValueError):
            pass


