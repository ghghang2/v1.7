"""Multi-agent team execution — coordinated parallel task completion.

Architecture
============
``/team <goal>`` hands a goal to a **coordinator** (one plain LLM instance,
no tools, no conversation state) that decomposes the goal into 2-8
**independent** tasks.  Each task is then executed by its own **worker**
agent — a fresh :class:`nbchat.tui.agent.TerminalAgent` with the full tool
set — running on its own daemon thread.  Workers are mutually
independent (the planner is prompted to make them so); the only
coordination points are:

* the shared ``n_parallel`` LLM slots, which naturally throttle
  concurrency to what llama-server can actually serve;
* one shared SQLite database (WAL mode, per-call connections) for
  persistence;
* per-agent locks/locks-free state — each worker owns its own
  ``TerminalAgent``, so there is no shared mutable conversation state.

After all tasks finish (or the per-run deadline expires), the coordinator
makes one final non-streaming LLM call that synthesizes the per-task
results into a single user-facing report, which is persisted to the shared
database under the team session id (``team:<run_id>``) alongside the
per-task rows.

Design invariants
-----------------
* The coordinator NEVER calls tools and NEVER writes into a worker's
  message history.  It only reads the final task reports.
* A worker NEVER sees another worker's output and MUST NOT push to git or
  run the full test suite (those are coordinator/follow-up duties) — the
  worker prompt states this explicitly.
* Every failure mode (planner LLM down, unparseable plan, worker crash,
  per-task timeout, Ctrl+C mid-run) degrades to a reported status
  (``done`` / ``failed`` / ``interrupted`` / ``skipped``) instead of an
  exception escaping to the terminal.
* ``run_plan`` is a pure staticmethod over ``(tasks, worker)`` so the
  dispatch mechanics are unit-testable without any LLM or agent objects.

See ``docs/multi_agent.md`` for the design document and
``tests/test_team.py`` for the behavioral contract.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from dataclasses import dataclass

from nbchat.core import config
from nbchat.core import db
from nbchat.tui.agent import TerminalAgent
from nbchat.tui.colors import Palette

# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------

PLAN_PARSE_FAILED = "plan_parse_failed"


class PlanParseError(Exception):
    """The planner's output could not be turned into a usable task list.

    ``reason`` is a stable machine-readable marker (``PLAN_PARSE_FAILED``)
    so callers can distinguish "model produced garbage" from a transport
    error without string-matching.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass
class Task:
    """One unit of parallel work."""
    task_id: str
    objective: str
    title: str = ""
    # pending -> claimed -> done | failed | interrupted
    status: str = "pending"
    summary: str = ""


def _extract_json(text: str) -> str:
    """Return the outermost JSON array in *text*, fenced or not.

    Scans for the first ``[`` and matches brackets with string awareness
    (brackets inside string literals do not count), so plans wrapped in
    prose or code fences work, and string literals containing nested
    arrays (``"note: [[x]]"``) cannot break the extraction.  Raises
    ``ValueError`` when no complete top-level array is present (e.g. the
    output was truncated mid-array by ``max_tokens``).
    """
    start = text.find("[")
    if start < 0:
        raise ValueError("no JSON array found in planner output")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("JSON array is incomplete (truncated plan output?)")


def _sanitize_plan(raw: list, max_tasks: int = 8) -> list:
    """Normalize raw planner entries into usable :class:`Task` objects.

    Drops entries that are not objects or that carry no usable objective
    (a title-only entry falls back to using the title as the objective),
    truncates to *max_tasks*, and assigns unique slug task ids.
    """
    out: list = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        objective = str(entry.get("objective") or "").strip()
        if not objective:
            # A title-only entry is still usable: the worker gets the title
            # as its full instruction.
            objective = str(entry.get("title") or "").strip()
        if not objective:
            continue
        title = str(entry.get("title") or "").strip() or objective[:60]
        # Positional id (T1, T2, ...) — stable, unique, terminal-friendly.
        out.append(Task(f"T{len(out) + 1}", objective, title=title))
    return out[:max_tasks]


def _parse_tasks(plan_text: str, max_tasks: int = 8) -> list:
    """Parse the planner's raw output into a (non-empty) list of tasks.

    Raises :class:`PlanParseError` on empty output, missing/invalid/truncated
    JSON, or a plan that yields no usable tasks — the caller decides how to
    degrade (the coordinator falls back to a single full-goal task).
    """
    text = (plan_text or "").strip()
    if not text:
        raise PlanParseError(PLAN_PARSE_FAILED, "empty plan output")
    try:
        raw = json.loads(_extract_json(text))
    except Exception as exc:
        raise PlanParseError(PLAN_PARSE_FAILED, f"invalid JSON: {exc}") from exc
    if isinstance(raw, dict):  # tolerate {"tasks": [...]}
        raw = raw.get("tasks")
    if not isinstance(raw, list):
        raise PlanParseError(PLAN_PARSE_FAILED, "plan is not a JSON array")
    tasks = _sanitize_plan(raw, max_tasks=max_tasks)
    if not tasks:
        raise PlanParseError(PLAN_PARSE_FAILED, "plan contains no usable tasks")
    return tasks


# ---------------------------------------------------------------------------
# ToolArbiter
# ---------------------------------------------------------------------------

class ToolArbiter:
    """Serialises repo-mutating tool calls with per-resource locks.

    The LLM tool loop runs on a shared 4-thread executor.  Two workers
    calling ``make_change_to_file`` simultaneously could interleave writes
    to the same file and corrupt it.  ``ToolArbiter`` wraps
    ``nbchat.ui.tool_executor.run_tool`` at the module level so every
    tool invocation passes through a per-resource ``threading.RLock``
    (re-entrant per OS thread — a worker that calls ``run_command`` inside
    ``run_command`` does not self-deadlock).

    Resources (from the design doc):

    * ``repo``  ← ``run_command``, ``make_change_to_file``,
      ``create_file``, ``push_to_github``
    * ``tests`` ← ``run_tests``

    All other tools (``browser``, ``get_weather``, ``repo_overview``,
    ``send_email``, …) are unmanaged and pass through without a lock.

    Usage::

        arbiter = ToolArbiter()
        arbiter.install()     # wraps run_tool globally (idempotent)
        ...
        arbiter.remove()      # restores the original run_tool

    ``install()`` is idempotent: calling it twice does not double-wrap.
    ``remove()`` is safe to call when the arbiter is not installed (no-op).
    """

    _MANAGED: dict[str, list[str]] = {
        "repo": ["run_command", "make_change_to_file",
                 "create_file", "push_to_github"],
        "tests": ["run_tests"],
    }

    def __init__(self) -> None:
        self._locks: dict[str, threading.RLock] = {
            res: threading.RLock() for res in self._MANAGED
        }
        self._original = None
        self._installed = False

    # -- public API ------------------------------------------------------

    def resource_for(self, tool_name: str) -> str | None:
        """Return the resource name for *tool_name*, or ``None``."""
        for res, tools in self._MANAGED.items():
            if tool_name in tools:
                return res
        return None

    # Backwards-compatible / test-facing alias.
    _resource_for = resource_for

    def install(self) -> None:
        """Wrap ``nbchat.ui.tool_executor.run_tool`` with arbiter logic.

        Idempotent — calling ``install()`` twice has no additional effect.
        """
        if self._installed:
            return
        import nbchat.ui.tool_executor as te
        self._original = te.run_tool
        orig = self._original
        arbiter = self

        def _arbitrated(tool_name: str, args_json: str,
                        timeout: int | None = None) -> str:
            res = arbiter.resource_for(tool_name)
            # Resolve dynamically: tests swap ``arbiter._original`` for a
            # stub after install(); reading it here keeps the swap live.
            _fn = arbiter._original
            if res is None:
                return _fn(tool_name, args_json, timeout=timeout)
            with arbiter._locks[res]:
                return _fn(tool_name, args_json, timeout=timeout)

        _arbitrated.__name__ = "run_tool"
        _arbitrated.__qualname__ = "ToolArbiter._arbitrated"
        te.run_tool = _arbitrated
        self._installed = True

    def remove(self) -> None:
        """Restore the original ``run_tool``.  No-op if not installed."""
        if not self._installed:
            return
        import nbchat.ui.tool_executor as te
        te.run_tool = self._original
        self._original = None
        self._installed = False

    def is_installed(self) -> bool:
        return self._installed



# ---------------------------------------------------------------------------
# Task queue (claim semantics)
# ---------------------------------------------------------------------------

class TaskQueue:
    """Thread-safe task pool with single-ownership claim semantics.

    ``claim()`` hands out one pending task at a time (insertion order); a
    claimed task is invisible to other claimants until it transitions to a
    terminal status or is explicitly ``release``d back.  ``stop()`` makes
    further claims return ``None`` (used by the coordinator on timeout /
    interrupt) and ``join()`` is the non-blocking predicate worker threads
    use to know the run is over.
    """

    def __init__(self, tasks: list) -> None:
        self._tasks = {t.task_id: t for t in tasks}
        self._lock = threading.Lock()
        self._stopped = False

    def claim(self):
        """Claim and return the next pending task id, or ``None``.

        Returns ``None`` when nothing is pending or the queue has been
        stopped — worker threads treat both as "exit".
        """
        with self._lock:
            if self._stopped:
                return None
            for t in self._tasks.values():
                if t.status == "pending":
                    t.status = "claimed"
                    return t.task_id
            return None

    def release(self, task_id: str) -> None:
        """Return a claimed task to the pool (rarely needed)."""
        with self._lock:
            t = self._tasks.get(task_id)
            if t is not None and t.status == "claimed":
                t.status = "pending"

    def stop(self) -> None:
        """Halts further claiming (coordinator timeout / interrupt)."""
        with self._lock:
            self._stopped = True

    def join(self) -> bool:
        """Non-blocking: ``True`` once the run must end (stop() called)."""
        with self._lock:
            return self._stopped

    def is_done(self) -> bool:
        """``True`` when no tasks remain pending (all claimed/resolved)."""
        with self._lock:
            return not any(t.status == "pending" for t in self._tasks.values())

    def __len__(self) -> int:
        return len(self._tasks)


# ---------------------------------------------------------------------------
# Worker output hooks
# ---------------------------------------------------------------------------

_PREFIX_HOOKS = (
    "_on_stream_reasoning", "_on_stream_token", "_on_tool_display",
    "_on_agent_message", "_on_stream_complete",
)


def _install_prefix_hooks(agent, tag: str) -> None:
    """Wrap an agent's output hooks so every line it prints is prefixed
    with ``[<tag>]`` — the terminal interleave of 4 workers stays readable
    and attributable.  Works for both :class:`TeamAgent` and real
    :class:`TerminalAgent` workers (same hook surface).
    """
    p = agent.palette
    prefix = p.dim(f"[{tag}] ")
    streamers = {"_on_stream_token", "_on_stream_reasoning"}
    for name in _PREFIX_HOOKS:
        orig = getattr(agent, name)
        if orig is None:
            continue
        state = {"active": False}

        def _inner(*args, _orig=orig, _name=name, **kwargs):
            # Streaming hooks fire per token: open the stream with the
            # tag once and close it on the matching stream-complete, so
            # the tag never fragments mid-sentence.  Discrete hooks
            # (tool display, agent messages) carry their own marker.
            if _name in streamers:
                if not state["active"] and args and args[0]:
                    sys.stdout.write(prefix)
                    sys.stdout.flush()
                    state["active"] = True
            elif args:
                sys.stdout.write(prefix)
                sys.stdout.flush()
            if _name == "_on_stream_complete":
                if state["active"] and not (args and args[0]):
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                state["active"] = False
            return _orig(*args, **kwargs)

        _inner.__name__ = name
        setattr(agent, name, _inner)


class TeamAgent:
    """Lightweight stand-in agent for the team run.

    Owns the team session id (``team:<run_id>``) and the worker registry
    (``sessions[task_id] -> worker agent``).  It implements the same
    output-hook surface as the terminal agent so it can be exercised on
    its own (tests) and its hooks can be prefix-wrapped exactly like a
    worker's.  The real per-task workers are full ``TerminalAgent``
    instances; this object is the coordinator-side container, not an LLM
    session of its own.
    """

    def __init__(self, *, color: bool = True) -> None:
        db.init_db()
        self.palette = Palette(color)
        self.model_name = config.MODEL_NAME
        self.session_id = f"team:{uuid.uuid4().hex[:8]}"
        self.sessions: dict = {}
        self._content_printed = ""

    # -- Output hooks (ConversationMixin-compatible surface) ------------

    def _on_stream_reasoning(self, reasoning: str) -> None:
        pass

    def _on_stream_token(self, content: str) -> None:
        delta = content[len(self._content_printed):]
        if delta:
            sys.stdout.write(delta)
            sys.stdout.flush()
        self._content_printed = content
        if content:
            db.log_message(self.session_id, "user", content)

    def _on_tool_display(self, raw_result: str, tool_name: str,
                         tool_args: str) -> None:
        p = self.palette
        sys.stdout.write(p.blue(f"  [tool] {tool_name}\n"))
        sys.stdout.flush()
        if raw_result:
            db.log_message(self.session_id, "tool", raw_result)

    def _on_agent_message(self, text: str) -> None:
        sys.stdout.write(self.palette.red(f"  ! {text}\n"))
        sys.stdout.flush()

    def _on_stream_complete(self, content: str, tool_calls) -> None:
        if content:
            sys.stdout.write("\n")
            sys.stdout.flush()
            db.log_message(self.session_id, "assistant", content)
        self._content_printed = ""

    def _wrap_worker_hooks(self, tag: str) -> None:
        """Prefix-wrap this agent's hooks (see :func:`_install_prefix_hooks`)."""
        _install_prefix_hooks(self, tag)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_PLANNER_SYSTEM = """\
You are the coordinator for a team of parallel software-engineering agents.
Your job is PLANNING ONLY — you MUST NOT attempt to execute any part of the
goal yourself.

- Analyze the user's goal and decompose it into 2-8 concrete, independent
  tasks that can be executed in parallel by separate workers.
- Tasks MUST be independent: each one must be completable without the
  output of any other task.  Never create sequencing tasks ("then fix the
  tests", "after that summarize") — workers run concurrently and cannot
  depend on each other.
- Each task must be fully self-contained: a worker with no other context
  must be able to complete it from its objective alone.  State the exact
  files, commands, or questions involved.
- A worker MUST NOT be asked to run the full test suite or to push to git;
  those are handled by the coordinator after the team run.
- If the goal is too small to split, still emit 2 tasks (e.g. "investigate
  X" + "verify/investigate Y").

Respond with ONLY a JSON array (no prose, no markdown fences) of objects:
[{"title": "<short name>", "objective": "<complete self-contained instruction for one worker>"}, ...]
"""

_SYNTHESIS_SYSTEM = """\
You are the coordinator of a team of parallel software-engineering agents.
Below is the final status report of the team's tasks.  Write the
user-facing final report in plain text: what was accomplished, key
findings, which tasks failed or were interrupted and why, and recommended
next steps.  Be concise.  Do NOT invent results that are not present in
the report.
"""


def _coordinator_system_prompt() -> str:
    """System prompt for the planner LLM call (planning only)."""
    return _PLANNER_SYSTEM


def _default_worker_prompt() -> str:
    """System prompt for each parallel worker agent."""
    return (
        "You are a worker agent in a team of parallel co-workers. "
        "Complete ONLY the task given in the user message, using your "
        "tools.  Other workers are executing different tasks in parallel "
        "right now: never assume changes made by other tasks, and keep all "
        "of your changes strictly within your own task — your work must be "
        "self-contained and independent of the other tasks.  When finished, "
        "reply with a concise summary of exactly what you did and the key "
        "results.  Do NOT run the full test suite and do NOT push to git; "
        "the coordinator handles verification and publishing after the "
        "team run."
    )


# ---------------------------------------------------------------------------
# Timeout helper
# ---------------------------------------------------------------------------

class _FutureStub:
    """Run ``fn(objective)`` on a private daemon thread and wait for it
    with a timeout — the worker thread must not block on the task's full
    duration, or the coordinator's deadline could never fire while the
    worker is still mid-task.  Re-raises the task's exception (including
    ``KeyboardInterrupt``) on the waiting thread once the task finishes."""

    def __init__(self, fn, objective) -> None:
        self._box = {"result": None, "exc": None}
        self._done = threading.Event()

        def _go() -> None:
            try:
                self._box["result"] = fn(objective)
            except BaseException as exc:  # noqa: BLE001 — re-raised below
                self._box["exc"] = exc
            finally:
                self._done.set()

        self._thread = threading.Thread(target=_go, daemon=True,
                                        name="nbchat-team-task")
        self._thread.start()

    def result(self, timeout: float | None = None):
        if not self._done.wait(timeout):
            raise TimeoutError(
                f"task timed out after {int(timeout or 0)}s")
        if self._box["exc"] is not None:
            raise self._box["exc"]
        return self._box["result"]


def _try_interrupt(worker) -> None:
    """Best-effort interrupt of a real worker agent (no-op for stubs)."""
    fn = getattr(worker, "interrupt", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class _WorkerProxy:
    """Adapter presenting one ``run(objective)`` call to ``run_plan``
    while mapping each objective to its own per-task worker agent."""

    def __init__(self, coordinator: "TeamCoordinator") -> None:
        self._c = coordinator

    def run(self, objective: str) -> str:
        worker = self._c._workers_by_objective.get(objective)
        if worker is None:
            raise RuntimeError(
                f"team worker not registered for: {objective[:80]}")
        return worker.send(objective)


class TeamCoordinator:
    """Plans a goal and executes the resulting tasks in parallel.

    ``run(goal)`` returns a dict ``{"status", "summary", "tasks"}`` where
    status is one of ``done`` / ``failed`` / ``interrupted`` / ``skipped``
    and tasks is a list of ``{task_id, title, objective, status, summary}``
    dicts.  It never raises for expected failure modes.
    """

    def __init__(self, agent: TeamAgent, worker_factory=None) -> None:
        self.agent = agent
        self._workers_by_objective: dict = {}
        self._run_id = ""
        # Injectable worker factory (tests pass stubs; default builds
        # full TerminalAgents with the real tool set).
        self._worker_factory = worker_factory or TerminalAgent

    # -- LLM calls (non-streaming) --------------------------------------

    def _coordinator_call(self, system: str, user: str, *,
                          max_tokens: int) -> str:
        """One non-streaming LLM call for the coordinator.

        Uses ``get_client()`` resolved at call time (so tests can
        monkeypatch ``nbchat.core.client.get_client``).  Prefers a flat
        ``chat_completions_create`` attribute (the shape used by test
        doubles) and falls back to the real client's
        ``chat.completions.create`` path.
        """
        from nbchat.core.client import get_client
        client = get_client()
        try:
            create = client.chat_completions_create
        except AttributeError:
            create = client.chat.completions.create
        resp = create(
            model=self.agent.model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        return (resp.choices[0].message.content or "").strip()

    # -- Plan dispatch ----------------------------------------------------

    @staticmethod
    def _execute_task(worker, task: Task, deadline: float | None) -> None:
        """Run one task through *worker* with the run deadline applied."""
        t0 = time.monotonic()
        remaining = (deadline - t0) if deadline is not None else None
        if remaining is not None and remaining <= 0:
            task.status = "failed"
            task.summary = "timed out (started after the run deadline)"
            return
        try:
            summary = _FutureStub(worker.run, task.objective).result(remaining)
        except KeyboardInterrupt:
            # Propagate: the coordinator marks the run interrupted and lets
            # the terminal handle the rest.
            raise
        except TimeoutError as exc:
            task.status = "failed"
            task.summary = f"{exc} (worker interrupted)"
            _try_interrupt(worker)
            return
        except Exception as exc:
            task.status = "failed"
            task.summary = str(exc)
            return
        task.status = "done"
        task.summary = str(summary) if summary else "(worker returned no summary)"

    @staticmethod
    def run_plan(tasks: list | None, worker, *, queue: TaskQueue | None = None,
                 max_workers: int = 4,
                 timeout: float | None = None) -> str:
        """Execute *tasks* on *worker* with up to *max_workers* parallel
        claimer threads.

        *worker* is any object with ``run(objective) -> str``.  ``timeout``
        is the coordinator's wall-clock budget for the whole plan: when it
        expires, unclaimed tasks are marked failed and claimed tasks are
        left to wind down (their workers are interrupted).  Returns
        ``"done"`` when at least one task succeeded, ``"failed"`` when none
        did, and ``"interrupted"`` on ``KeyboardInterrupt``.  Never raises
        for expected failure modes.
        """
        q = queue if queue is not None else TaskQueue(tasks)
        if not q._tasks:
            return "failed"
        n_workers = max(1, min(int(max_workers), len(q._tasks)))
        start = time.monotonic()
        deadline = start + timeout if timeout is not None else None
        state = [{"exc": None} for _ in range(n_workers)]

        def _worker_main(idx: int) -> None:
            while True:
                tid = q.claim()
                if tid is None:
                    break
                try:
                    TeamCoordinator._execute_task(worker, q._tasks[tid],
                                                  deadline)
                except KeyboardInterrupt:
                    state[idx]["exc"] = KeyboardInterrupt()
                    q.stop()
                    break
                except Exception as exc:  # defensive: harness must not die
                    q._tasks[tid].status = "failed"
                    q._tasks[tid].summary = f"worker harness error: {exc}"

        threads = [
            threading.Thread(target=_worker_main, args=(i,), daemon=True,
                             name=f"nbchat-team-worker-{i + 1}")
            for i in range(n_workers)
        ]
        for t in threads:
            t.start()

        interrupted = False
        try:
            for t in threads:
                if deadline is None:
                    t.join()
                else:
                    t.join(max(0.0, deadline - time.monotonic()))
        except KeyboardInterrupt:
            interrupted = True
        for s in state:
            if s["exc"] is not None:
                interrupted = True
        # Grace period (timeout case only): the main thread's join deadline
        # and the workers' internal per-task deadline race by milliseconds.
        # Without a small settle after the deadline has fired, the sweep
        # below can observe a task still "claimed" just before its in-flight
        # handler marks it failed/interrupted.  Never applied when the run
        # finished before the deadline (keeps the concurrency path tight).
        if deadline is not None and time.monotonic() >= deadline:
            time.sleep(0.25)

        q.stop()
        if interrupted:
            for t in q._tasks.values():
                if t.status in ("pending", "claimed"):
                    t.status = "interrupted"
                    t.summary = "interrupted"
            return "interrupted"

        timed_out = deadline is not None and time.monotonic() > deadline + 0.05
        if timed_out:
            for t in q._tasks.values():
                if t.status == "claimed":
                    t.status = "failed"
                    t.summary = "timed out (coordinator run deadline)"
                elif t.status == "pending":
                    t.status = "failed"
                    t.summary = "not started (team run timed out)"
            # In-flight ("claimed") tasks were left mid-run by the deadline
            # (their worker threads are still winding down): mark them failed
            # too, so no task is left claimed forever.
            for t in q._tasks.values():
                if t.status == "claimed":
                    t.status = "failed"
                    t.summary = "timed out (coordinator run deadline)"
            _try_interrupt(worker)
        return "done" if any(t.status == "done"
                             for t in q._tasks.values()) else "failed"

    # -- Run ---------------------------------------------------------------

    def run(self, goal: str) -> dict:
        """Plan *goal*, execute the plan in parallel, synthesize a report.

        Always returns; see the class docstring for the result shape.
        """
        if not config.TEAM_ENABLED:
            return {
                "status": "skipped",
                "summary": ("Team execution is disabled "
                            "(team_enabled: false in repo_config.yaml)."),
                "tasks": [],
            }
        self._run_id = uuid.uuid4().hex[:8]
        self.agent.session_id = f"team:{self._run_id}"
        self.agent.sessions = {}
        self._workers_by_objective = {}
        p = self.agent.palette

        print(p.magenta(f"\n  [team] planning run {self._run_id} ...\n"))
        try:
            plan_text = self._coordinator_call(
                _coordinator_system_prompt(), goal,
                max_tokens=config.TEAM_PLAN_MAX_TOKENS)
        except KeyboardInterrupt:
            return self._finish("interrupted",
                                "Team run interrupted during planning.", [])
        except Exception as exc:
            return self._finish("failed",
                                f"Planner LLM call failed: {exc}", [])

        try:
            tasks = _parse_tasks(plan_text, max_tasks=config.TEAM_MAX_TASKS)
        except PlanParseError as exc:
            print(p.yellow(
                f"  [team] plan unparseable ({exc.reason}); "
                f"running the goal as a single task."))
            tasks = [Task("task", goal, title="full goal")]

        for i, t in enumerate(tasks, 1):
            print(p.cyan(
                f"  [team] task {i} ({t.task_id}): {t.objective[:120]}"))

        # One worker agent per task, registered on the team agent.
        for i, t in enumerate(tasks, 1):
            tag = f"w{i}"
            if not self._make_worker(t, tag):
                continue

        proxy = _WorkerProxy(self)
        try:
            result = self.run_plan(
                tasks, proxy,
                max_workers=config.TEAM_MAX_WORKERS,
                timeout=config.TEAM_TASK_TIMEOUT)
        except KeyboardInterrupt:
            result = "interrupted"
            for t in tasks:
                if t.status in ("pending", "claimed"):
                    t.status = "interrupted"
                    t.summary = "interrupted"

        report = "Team task report:\n" + "\n".join(
            f"- [{t.status}] {t.title or t.task_id}: {t.summary[:500]}"
            for t in tasks)
        if result == "interrupted":
            summary = f"Team run interrupted. {report}"
        else:
            try:
                summary = self._coordinator_call(
                    _SYNTHESIS_SYSTEM, report,
                    max_tokens=config.TEAM_SYNTHESIS_MAX_TOKENS)
            except KeyboardInterrupt:
                result = "interrupted"
                summary = f"Team run interrupted during synthesis. {report}"
            except Exception as exc:
                summary = f"Synthesis LLM call failed ({exc}).\n{report}"

        print(p.magenta(f"\n  [team] report ({result}):\n"))
        for line in summary.splitlines() or [""]:
            print("  " + line)
        print()
        return self._finish(result, summary, tasks)

    def _make_worker(self, task: Task, tag: str) -> bool:
        """Build + register one worker for *task*; False on failure."""
        try:
            try:
                worker = self._worker_factory(color=False)
            except TypeError:
                worker = self._worker_factory()
        except Exception as exc:  # shouldn't happen; degrade per task
            task.status = "failed"
            task.summary = f"worker init failed: {exc}"
            return False
        worker.session_id = f"team:{self._run_id}-{tag}"
        worker.system_prompt = _default_worker_prompt()
        _install_prefix_hooks(worker, tag)
        self.agent.sessions[task.task_id] = worker
        self._workers_by_objective[task.objective] = worker
        return True

    def _finish(self, status: str, summary: str, tasks: list) -> dict:
        """Persist the final report row and shape the result dict."""
        try:
            db.log_message(self.agent.session_id, "assistant", summary)
        except Exception:
            pass  # persistence is best-effort; the report is already printed
        return {
            "status": status,
            "summary": summary,
            "tasks": [
                {"task_id": t.task_id, "title": t.title,
                 "objective": t.objective, "status": t.status,
                 "summary": t.summary}
                for t in tasks
            ],
        }


__all__ = [
    "PLAN_PARSE_FAILED", "PlanParseError", "Task", "TaskQueue",
    "TeamAgent", "TeamCoordinator", "ToolArbiter",
    "_coordinator_system_prompt", "_default_worker_prompt",
    "_extract_json", "_parse_tasks", "_sanitize_plan",
]
