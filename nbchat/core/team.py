"""Multi-agent team execution — coordinated parallel task completion.

Architecture
============
``/team <goal>`` hands a goal to a **coordinator** (one plain LLM instance,
no tools, no conversation state) that decomposes the goal into 2-8
**independent** tasks.  Each task is then executed by its own **worker**
agent — a fresh :class:`nbchat.tui.agent.TerminalAgent` with the full tool
set — running on its own claimer thread.  Workers are mutually
independent (the planner is prompted to make them so); the coordination
points are:

* the shared LLM slots of the inference server, which naturally throttle
  concurrency to what it can actually serve;
* one shared SQLite database (WAL mode, per-call connections) for
  persistence;
* per-agent locks and state — each worker owns its own ``TerminalAgent``,
  so there is no shared mutable conversation state;
* a shared work queue (:class:`TaskQueue`) behind a bounded pool of
  claimer threads (:class:`_WorkerPool`).

**Worker delegation.**  A worker may split its own task further: the
``delegate_task`` tool (``nbchat/tools/delegate_task.py``) pushes
independent subtasks onto the same queue, and the pool spawns additional
claimer threads for them while worker slots are free — so with
``team_max_workers: 4`` a single coarse top-level task can still fill all
three remaining slots with sub-workers.  Delegation is bounded by
``team_max_delegation_depth`` (a worker at depth ``d`` may only delegate
while ``d < limit``; deeper "subtasks" are answered with an instruction to
run inline), ``team_max_subtasks`` (per-run cap on delegated subtasks) and
``team_max_total_tasks`` (queue capacity).  A delegating parent stays
``claimed`` until its subtasks all resolve, so it occupies its slot while
its children run and its final summary reflects them.

The per-task context a worker sees while executing is a
:class:`DelegationContext` bound via a ``contextvars.ContextVar`` that is
copied into the worker's execution thread (``TerminalAgent.send`` is
thread-safe and runs the agentic loop in-process), so nested delegation
propagates correctly and the main terminal agent never sees a live
context.

After all tasks finish (or the per-run deadline expires), the coordinator
makes one final non-streaming LLM call that synthesizes the per-task
results (top-level tasks *and* their subtasks) into a single user-facing
report, which is persisted to the shared database under the team session
id (``team:<run_id>``) alongside the per-task rows.

Design invariants
-----------------
* The coordinator NEVER calls tools and NEVER writes into a worker's
  message history.  It only reads the final task reports.
* A worker NEVER sees another worker's output, MUST NOT push to git or
  run the full test suite (those are coordinator/follow-up duties), and
  may only delegate subtasks that are fully independent of its own task
  (the worker prompt states this explicitly).
* Every failure mode (planner LLM down, unparseable plan — retried up to
  ``team_plan_attempts`` times, worker crash, per-task timeout, Ctrl+C
  mid-run) degrades to a reported status (``done`` / ``failed`` /
  ``interrupted`` / ``skipped``) instead of an exception escaping to the
  terminal.
* ``run_plan`` is a pure staticmethod over ``(tasks, worker)`` so the
  dispatch mechanics are unit-testable without any LLM or agent objects.

See ``docs/multi_agent.md`` for the design document and
``tests/test_team.py`` for the behavioral contract.
"""
from __future__ import annotations

import contextvars
import json
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

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
    """One unit of parallel work.

    ``parent_id`` links a delegated subtask to the task that delegated it
    (``None`` for top-level planner tasks).
    """
    task_id: str
    objective: str
    title: str = ""
    # pending -> claimed -> done | failed | interrupted
    # (a task that delegated subtasks stays 'claimed' until they all
    #  resolve; the pool's settle step turns it into done/failed)
    status: str = "pending"
    summary: str = ""
    parent_id: Optional[str] = None


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
    degrade (the coordinator retries the planner, then falls back to a
    single full-goal task).
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
    ``send_email``, ...) are unmanaged and pass through without a lock.

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
            lock = arbiter._locks[res]
            # Bounded acquisition: a worker inside a team run may block on
            # the resource lock at most until the run deadline (minus a
            # safety margin).  A tool that wedges its holder can no longer
            # convoy every other worker past the deadline — the caller
            # gets an actionable error result instead of parking forever.
            wait = None
            deadline = _team_deadline.get()
            if deadline is not None:
                wait = max(5.0, deadline - time.monotonic() - 10.0)
            acquired = (
                lock.acquire() if wait is None
                else lock.acquire(timeout=wait))
            if not acquired:
                return (f"Tool '{tool_name}' could not acquire the team "
                        f"'{res}' lock before the run deadline; the "
                        "resource is probably held by a hung tool call. "
                        "Do not retry it — finish your task without "
                        "mutating the repository and report the conflict.")
            try:
                return _fn(tool_name, args_json, timeout=timeout)
            finally:
                lock.release()

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
    terminal status or is explicitly ``release``d back.  ``add()`` admits
    tasks delegated at run time (by worker agents via the
    ``delegate_task`` tool), and ``notify()`` wakes the pool's reaper so
    the new work is picked up without a poll.  ``stop()`` makes further
    claims return ``None`` (used by the coordinator on timeout /
    interrupt) and ``join()`` is the predicate worker threads use to know
    the run is over.
    """

    def __init__(self, tasks: list) -> None:
        self._tasks = {t.task_id: t for t in tasks}
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
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

    def add(self, task: Task) -> None:
        """Register a newly delegated task and wake the pool."""
        with self._cv:
            self._tasks[task.task_id] = task
            self._cv.notify_all()

    def notify(self) -> None:
        """Wake waiters (delegation bookkeeping hook)."""
        with self._cv:
            self._cv.notify_all()

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks.values()
                       if t.status == "pending")

    def children(self, parent_id: str) -> list:
        """The subtasks *parent_id* delegated (in claim order)."""
        with self._lock:
            return [t for t in self._tasks.values()
                    if t.parent_id == parent_id]

    def children_pending(self, parent_id: str) -> int:
        """Count of a parent's subtasks that are not yet resolved."""
        with self._lock:
            return sum(
                1 for t in self._tasks.values()
                if t.parent_id == parent_id and t.status in ("pending", "claimed")
            )

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
# Delegation context (worker -> worker handoff)
# ---------------------------------------------------------------------------

#: ContextVar carrying the :class:`DelegationContext` of the team task
#: currently executing on this thread.  Set per task by the worker pool
#: (copied into the worker's execution thread), read by the
#: ``delegate_task`` tool.  Empty outside a team run, so the main
#: terminal agent (and any non-team context) sees delegation as
#: unavailable instead of hitting the team queue.
_current_delegation: contextvars.ContextVar = contextvars.ContextVar(
    "nbchat_team_delegation", default=None)

#: ContextVar carrying the monotonic deadline of the team run executing
#: on this thread (or None outside a team run).  ``ToolArbiter`` reads it
#: to cap how long a worker may block acquiring a per-resource lock: with
#: an unbounded ``with lock:`` a single hung tool call could hold the
#: ``repo`` lock forever and wedge every other worker's next
#: repo-mutating call — the dead-forever path behind the hung /team
#: runs (workers parked on the lock's futex, no I/O, no output).
_team_deadline: contextvars.ContextVar = contextvars.ContextVar(
    "nbchat_team_deadline", default=None)


@dataclass
class DelegationContext:
    """Everything a worker needs to hand a subtask to a peer.

    Created by :meth:`TeamCoordinator._make_delegation` per task, bound
    to the executing thread via :data:`_current_delegation`, and consumed
    by :func:`_delegate_subtask` (called from the ``delegate_task`` tool).
    """
    queue: TaskQueue
    deadline: Optional[float]
    worker_factory: Callable
    max_workers: int
    max_subtasks: int
    max_total: int
    max_depth: int
    run_id: str
    agent: Any
    task_id: str
    parent_objective: str
    depth: int
    tag: str
    session_id: str
    # Delegation bookkeeping shared by every context in one run (guarded
    # by ``lock`` so two workers delegating at the same instant cannot
    # both pass the caps).
    subtask_count: int
    subtask_seq: int
    lock: threading.Lock


def _delegate_subtask(ctx: DelegationContext, objective: str,
                      title: str = "") -> str:
    """Push one independent subtask onto the team queue.

    Called by the ``delegate_task`` tool on the delegating worker's
    thread.  Enforces the delegation limits; returns a JSON string the
    worker can read.
    """
    objective = (objective or "").strip()
    if not objective:
        return json.dumps({"error": "objective must not be empty"})

    with ctx.lock:
        if ctx.depth >= ctx.max_depth:
            return json.dumps({
                "inline": True,
                "reason": (
                    f"delegation depth limit reached "
                    f"(depth {ctx.depth} >= {ctx.max_depth}); complete "
                    "this subtask inline instead of delegating"
                ),
            })
        if ctx.subtask_count >= ctx.max_subtasks:
            return json.dumps({
                "inline": True,
                "reason": (
                    f"subtask cap reached ({ctx.max_subtasks}); complete "
                    "the remaining work inline"
                ),
            })
        if len(ctx.queue) >= ctx.max_total:
            return json.dumps({
                "inline": True,
                "reason": (
                    f"total task cap reached ({ctx.max_total}); complete "
                    "the remaining work inline"
                ),
            })
        ctx.subtask_count += 1
        ctx.subtask_seq += 1
        sub_id = f"{ctx.task_id}.s{ctx.subtask_seq}"

    task = Task(sub_id, objective,
                title=(title or "").strip() or objective[:60],
                parent_id=ctx.task_id)
    ctx.queue.add(task)
    sys.stdout.write(
        f"  [team] {ctx.task_id} delegated {sub_id} to the pool "
        f"(depth {ctx.depth + 1}): {objective[:100]}\n")
    sys.stdout.flush()
    return json.dumps({
        "subtask_id": sub_id,
        "queued": True,
        "note": (
            "the subtask runs on an idle worker slot; the parent task is "
            "not marked done until it resolves"
        ),
    })


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

    Stub workers (unit tests) have no TUI surface: skip silently when
    neither a palette nor any hook is present rather than crashing the
    worker thread.
    """
    palette = getattr(agent, "palette", None)
    if palette is None and not any(
            getattr(agent, name, None) for name in _PREFIX_HOOKS):
        return
    prefix = (palette.dim(f"[{tag}] ") if palette is not None
              else f"[{tag}] ")
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

- Analyze the user's goal and decompose it into 2-4 concrete, independent
  tasks (up to 8 only for a very large goal) that can be executed in
  parallel by separate workers.  Do not emit more tasks than the work
  justifies: each task runs on its own worker agent.
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
- The JSON array MUST be complete and well-formed — a truncated plan
  degrades the whole run to a single worker.

Respond with ONLY a JSON array (no prose, no markdown fences) of objects:
[{"title": "<short name>", "objective": "<complete self-contained instruction for one worker>"}, ...]
"""

_RETRY_PLANNER_USER = (
    "Your previous plan could not be parsed as a complete JSON array of "
    "{{title, objective}} objects.  Reply again with ONLY the complete JSON "
    "array (2-4 tasks, no prose, no fences, nothing after the closing "
    "bracket).  The plan was unparseable: {detail}\n"
    "Original goal:\n{goal}"
)

_SYNTHESIS_SYSTEM = """\
You are the coordinator of a team of parallel software-engineering agents.
Below is the final status report of the team's tasks (including any
subtasks that workers delegated to their peers).  Write the
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
        "You are a worker agent in a team of parallel co-workers.  "
        "Complete ONLY the task given in the user message, using your "
        "tools.  Other workers may be executing different tasks in "
        "parallel right now: never assume changes made by other tasks, "
        "and keep all of your changes strictly within your own task — "
        "your work must be self-contained and independent of the other "
        "tasks.  You can see how many worker slots are busy from the "
        "delegate_task tool's response context; if your task genuinely "
        "splits into 2 or more independent sub-pieces, delegate them "
        "with the delegate_task tool so idle worker slots pick them up in "
        "parallel — NEVER report that you are the only worker available "
        "when slots are free.  You MUST NOT delegate work that depends "
        "on your own task's outcome, and delegated subtasks must be "
        "fully self-contained.  When finished, reply with a concise "
        "summary of exactly what you did and the key results.  Do NOT "
        "run the full test suite and do NOT push to git; the coordinator "
        "handles verification and publishing after the team run."
    )


# ---------------------------------------------------------------------------
# Timeout helper
# ---------------------------------------------------------------------------

class _FutureStub:
    """Run ``fn(objective)`` on a private daemon thread and wait for it
    with a timeout — the claimer thread must not block on the task's full
    duration, or the coordinator's deadline could never fire while the
    worker is still mid-task.  The calling thread's ``contextvars``
    context (notably the current :class:`DelegationContext`) is copied
    into the worker thread, so a delegated worker can itself delegate.
    Re-raises the task's exception (including ``KeyboardInterrupt``) on
    the waiting thread once the task finishes."""

    def __init__(self, fn, objective) -> None:
        self._box = {"result": None, "exc": None}
        self._done = threading.Event()
        self._ctx = contextvars.copy_context()

        def _go() -> None:
            try:
                self._box["result"] = self._ctx.run(fn, objective)
            except BaseException as exc:  # noqa: BLE001 — re-raised below
                self._box["exc"] = exc
            finally:
                self._done.set()
                _untrack_task_thread(self._thread)

        self._thread = threading.Thread(target=_go, daemon=True,
                                        name="nbchat-team-task")
        _track_task_thread(self._thread)
        self._thread.start()


    def result(self, timeout: float | None = None):
        if not self._done.wait(timeout):
            raise TimeoutError(
                f"task timed out after {int(timeout or 0)}s")
        if self._box["exc"] is not None:
            raise self._box["exc"]
        return self._box["result"]

#: Every live worker-task thread of the current run, so the pool's deadline
#: cleanup can join them and *report* the ones that cannot be woken
#: (a parked thread stays alive past the run — silent before, visible now).
_task_threads: set = set()
_task_threads_lock = threading.Lock()


def _track_task_thread(thread) -> None:
    with _task_threads_lock:
        _task_threads.add(thread)


def _untrack_task_thread(thread) -> None:
    with _task_threads_lock:
        _task_threads.discard(thread)


def _collect_abandoned_threads(join_timeout: float = 5.0) -> list:
    """Join outstanding task threads briefly; return the ones still alive."""
    with _task_threads_lock:
        threads = [t for t in _task_threads if t is not threading.current_thread()]
    for t in threads:
        t.join(join_timeout)
    with _task_threads_lock:
        return [t for t in threads if t.is_alive()]


def _try_interrupt(worker) -> None:
    """Best-effort interrupt of a real worker agent (no-op for stubs)."""
    fn = getattr(worker, "interrupt", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            pass


def _worker_run(worker, objective: str) -> str:
    """Invoke a worker's turn.

    Real workers are ``TerminalAgent`` instances (``send``); the stub
    workers used by ``run_plan`` tests expose ``run`` directly.
    """
    fn = getattr(worker, "run", None)
    if not callable(fn):
        fn = worker.send
    return fn(objective)


# ---------------------------------------------------------------------------
# Worker pool (grow-as-needed claimer threads + delegation dispatch)
# ---------------------------------------------------------------------------

class _WorkerPool:
    """Live dispatch of claimer threads over a shared :class:`TaskQueue`.

    The pool starts one claimer per initially pending task (capped at
    ``max_workers``) plus a **reaper** thread that spawns *additional*
    claimer threads whenever delegated subtasks appear and claimer slots
    are free (in-flight work = claimed tasks + pending subtasks, which is
    always <= the number of live claimer threads, so the accounting is
    exact — a run of N tasks starts min(N, max) claimers and a single
    delegating worker can still grow the pool up to the cap).  Each
    claimer executes its claimed task through a fresh worker agent bound
    to a :class:`DelegationContext` (see :meth:`_execute_one`); a task
    whose worker delegated subtasks stays ``claimed`` until the pool's
    settle step turns it into done/failed.
    """

    def __init__(self, queue: TaskQueue, worker_factory,
                 max_workers: int, deadline: float | None,
                 coordinator: "TeamCoordinator",
                 max_subtasks: int, max_total: int, max_depth: int) -> None:
        self.queue = queue
        self.factory = worker_factory
        self.max_workers = max(1, int(max_workers))
        self.deadline = deadline
        self.c = coordinator
        self.max_subtasks = max_subtasks
        self.max_total = max_total
        self.max_depth = max_depth
        self._threads: set[threading.Thread] = set()
        self._inflight = 0
        self._worker_seq = 0
        # RLock: run() and _reaper() call _spawn_claimer() while they
        # already hold the pool lock, and _spawn_claimer re-acquires it.
        # A plain Lock self-deadlocked there on every real /team run.
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._stopped = False

    # -- internals -------------------------------------------------------

    def _spawn_claimer(self) -> None:
        with self._lock:
            self._worker_seq += 1
            seq = self._worker_seq
        t = threading.Thread(
            target=self._claim_loop, daemon=True,
            name=f"nbchat-team-worker-{seq}")
        with self._lock:
            self._threads.add(t)
        t.start()

    def _claim_loop(self) -> None:
        try:
            while True:
                if self.queue.join():
                    break
                tid = self.queue.claim()
                if tid is None:
                    break
                with self._cv:
                    self._inflight += 1
                    self._cv.notify_all()
                try:
                    self._execute_one(tid)
                finally:
                    with self._cv:
                        self._inflight -= 1
                        self._cv.notify_all()
        finally:
            with self._lock:
                self._threads.discard(threading.current_thread())

    def _execute_one(self, tid: str) -> None:
        """Build a per-task worker, bind its delegation context and run."""
        c = self.c
        queue = self.queue
        with queue._lock:
            task = queue._tasks.get(tid)
        if task is None:
            return
        if self.deadline is not None and time.monotonic() >= self.deadline:
            task.status = "failed"
            task.summary = "not started (started after the run deadline)"
            return
        ctx = c._make_delegation(
            task, self.factory, self.max_workers, self.deadline,
            self.max_subtasks, self.max_total, self.max_depth)
        if ctx.agent is None:
            # Worker init failed: _make_delegation already marked the
            # task failed; nothing to delegate.
            return
        from nbchat.core import client as _client_mod
        # Bind the run deadline + LLM budget onto this execution thread so
        # (a) ToolArbiter bounds lock waits and (b) the worker's LLM
        # requests carry a hard read timeout (a worker parked inside an
        # unbounded SDK call was the other root cause of hung runs).
        deadline_token = _team_deadline.set(self.deadline)
        llm_token = _client_mod.team_llm_timeout.set(
            float(config.TEAM_LLM_TIMEOUT))
        token = _current_delegation.set(ctx)
        try:
            TeamCoordinator._execute_task(ctx.agent, task, self.deadline)
        finally:
            _client_mod.team_llm_timeout.reset(llm_token)
            _team_deadline.reset(deadline_token)
            _current_delegation.reset(token)
        # A delegating parent stays 'claimed' while its subtasks run;
        # annotate its summary so the report is honest either way.
        kids = queue.children(task.task_id)
        if kids:
            done = sum(1 for k in kids if k.status == "done")
            task.summary = (
                f"{task.summary} "
                f"(delegated {len(kids)} subtask(s), {done} done)")

    def _reaper(self) -> None:
        """Spawn claimer threads while slots are free and work exists."""
        while True:
            with self._cv:
                if self._stopped:
                    return
                if (self.deadline is not None
                        and time.monotonic() >= self.deadline):
                    return
                if (self.queue.pending_count() > 0
                        and self._inflight < self.max_workers):
                    self._spawn_claimer()
                try:
                    self._cv.wait(timeout=0.5)
                except Exception:
                    return

    # -- public API ------------------------------------------------------

    def run(self) -> str:
        """Dispatch the whole run; returns done / failed / interrupted."""
        queue = self.queue
        if not len(queue):
            return "failed"
        with self._lock:
            n_initial = min(self.max_workers,
                            max(1, queue.pending_count()))
            for _ in range(n_initial):
                self._spawn_claimer()
        reaper = threading.Thread(target=self._reaper, daemon=True,
                                  name="nbchat-team-pool-reaper")
        reaper.start()
        try:
            while True:
                with self._cv:
                    if self._stopped:
                        break
                    if (self.deadline is not None
                            and time.monotonic() >= self.deadline):
                        break
                    if queue.is_done() and self._inflight == 0:
                        break
                    self._cv.wait(timeout=0.1)
        except KeyboardInterrupt:
            with self._cv:
                self._stopped = True
                self._cv.notify_all()
        finally:
            with self._cv:
                self._stopped = True
                self._cv.notify_all()
        for t in list(self._threads):
            if t.ident == threading.get_ident():
                continue
            t.join(max(0.0, self.deadline - time.monotonic())
                   if self.deadline is not None else None)
        reaper.join(None)

        interrupted = any(
            t.status == "interrupted" for t in queue._tasks.values())
        queue.stop()
        if interrupted:
            for t in queue._tasks.values():
                if t.status in ("pending", "claimed"):
                    t.status = "interrupted"
                    t.summary = "interrupted"
            return "interrupted"

        # Grace period (timeout case only): let in-flight handlers mark
        # their own tasks failed before the sweep below runs.
        timed_out = (self.deadline is not None
                     and time.monotonic() >= self.deadline)
        if timed_out:
            time.sleep(0.25)

        if timed_out:
            for t in queue._tasks.values():
                if t.status == "pending":
                    t.status = "failed"
                    t.summary = "not started (team run timed out)"
                elif t.status == "claimed":
                    t.status = "failed"
                    t.summary = "timed out (coordinator run deadline)"
            self.c._interrupt_active_workers()
            # Interrupt is cooperative; threads parked inside an LLM read
            # or on a lock will not wake.  Surface them instead of leaving
            # the run to appear hung (this is the diagnostic that was
            # missing from the two stuck sessions).
            abandoned = _collect_abandoned_threads(join_timeout=2.0)
            if abandoned:
                print(
                    f"  [team] warning: {len(abandoned)} worker thread(s) "
                    "could not be interrupted and are being abandoned; "
                    "they will exit when their blocking call returns. "
                    "Run status is unaffected.",
                    file=sys.stderr)
            return "failed"

        # Settle: a claimed task that delegated subtasks is resolved
        # against its children (all done -> done, any failure -> failed).
        for t in queue._tasks.values():
            if t.status != "claimed":
                continue
            kids = queue.children(t.task_id)
            if not kids:
                continue
            if all(k.status == "done" for k in kids):
                t.status = "done"
            else:
                t.status = "failed"
                t.summary = (
                    f"{t.summary} (delegated subtasks incomplete: "
                    f"{sum(1 for k in kids if k.status != 'done')} "
                    f"of {len(kids)} not done)")
        return "done" if any(t.status == "done"
                             for t in queue._tasks.values()) \
            else "failed"


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class TeamCoordinator:
    """Plans a goal and executes the resulting tasks in parallel.

    ``run(goal)`` returns a dict ``{"status", "summary", "tasks"}`` where
    status is one of ``done`` / ``failed`` / ``interrupted`` / ``skipped``
    and tasks is a list of ``{task_id, title, objective, status, summary,
    parent_id}`` dicts (top-level tasks first, then any subtasks workers
    delegated).  It never raises for expected failure modes.
    """

    def __init__(self, agent: TeamAgent, worker_factory=None) -> None:
        self.agent = agent
        # Injectable worker factory (tests pass stubs; default builds
        # full TerminalAgents with the real tool set).
        self._worker_factory = worker_factory or TerminalAgent
        self._run_id = ""
        self._active_workers: list = []
        self._active_workers_lock = threading.Lock()

    # -- LLM calls (non-streaming) --------------------------------------

    def _coordinator_call(self, system: str, user: str, *,
                          max_tokens: int,
                          timeout: float | None = None) -> str:
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
        kwargs = dict(
            model=self.agent.model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = create(**kwargs)
        return (resp.choices[0].message.content or "").strip()

    # -- Worker construction ---------------------------------------------

    def _make_worker(self, task: Task, tag: str, factory) -> bool:
        """Build + register one worker for *task*; False on failure."""
        try:
            try:
                worker = factory(color=False)
            except TypeError:
                worker = factory()
        except Exception as exc:  # shouldn't happen; degrade per task
            task.status = "failed"
            task.summary = f"worker init failed: {exc}"
            return False
        worker.session_id = f"team:{self._run_id}-{tag}"
        worker.system_prompt = _default_worker_prompt()
        _install_prefix_hooks(worker, tag)
        self.agent.sessions[task.task_id] = worker
        with self._active_workers_lock:
            self._active_workers.append(worker)
        return True

    def _make_delegation(self, task: Task, factory, max_workers: int,
                         deadline: float | None, max_subtasks: int,
                         max_total: int, max_depth: int) -> DelegationContext:
        """Build the per-task worker + its :class:`DelegationContext`."""
        tag = f"w{self._next_worker_tag()}"
        agent = None
        if self._make_worker(task, tag, factory):
            agent = self.agent.sessions[task.task_id]
        queue = self._pool_queue
        return DelegationContext(
            queue=queue if queue is not None else TaskQueue([]),
            deadline=deadline,
            worker_factory=factory, max_workers=max_workers,
            max_subtasks=max_subtasks, max_total=max_total,
            max_depth=max_depth, run_id=self._run_id, agent=agent,
            task_id=task.task_id, parent_objective=task.objective,
            depth=self._task_depth(task), tag=tag,
            session_id=agent.session_id if agent is not None else "",
            subtask_count=self._pool_subtask_count,
            subtask_seq=self._pool_subtask_seq,
            lock=self._pool_lock,
        )

    _worker_tag_seq = 0
    _pool_queue: Optional[TaskQueue] = None
    _pool_subtask_count = 0
    _pool_subtask_seq = 0
    _pool_lock = threading.Lock()

    def _next_worker_tag(self) -> int:
        with self._active_workers_lock:
            TeamCoordinator._worker_tag_seq += 1
            return TeamCoordinator._worker_tag_seq

    def _task_depth(self, task: Task) -> int:
        """Delegation depth of *task* (top-level = 0)."""
        depth = 0
        seen = set()
        queue = self._pool_queue
        while task.parent_id and task.parent_id not in seen:
            seen.add(task.parent_id)
            parent = (queue._tasks.get(task.parent_id)
                      if queue is not None else None)
            if parent is None:
                break
            depth += 1
            task = parent
        return depth

    def _interrupt_active_workers(self) -> None:
        with self._active_workers_lock:
            workers = list(self._active_workers)
        for w in workers:
            _try_interrupt(w)

    # -- Task execution ----------------------------------------------------

    @staticmethod
    def _execute_task(worker, task: Task, deadline: float | None) -> None:
        """Run one task through *worker* with the run deadline applied.

        When the task's worker delegated subtasks, the task stays
        ``claimed`` (the pool's settle step resolves it against the
        children) instead of being marked done immediately.
        """
        t0 = time.monotonic()
        remaining = (deadline - t0) if deadline is not None else None
        if remaining is not None and remaining <= 0:
            task.status = "failed"
            task.summary = "timed out (started after the run deadline)"
            return
        try:
            summary = _FutureStub(
                lambda obj: _worker_run(worker, obj), task.objective
            ).result(remaining)
        except KeyboardInterrupt:
            # Propagate: the claim loop marks the run interrupted and lets
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
        if not isinstance(summary, str):
            summary = str(summary) if summary else ""
        # A parent with pending children stays claimed (the pool settles
        # it); a leaf task is done.
        ctx = _current_delegation.get()
        pending_children = (
            ctx.queue.children_pending(task.task_id)
            if ctx is not None else 0)
        if pending_children:
            task.status = "claimed"
            task.summary = (
                f"{summary or '(worker returned no summary)'} "
                f"({pending_children} delegated subtask(s) still pending)")
        else:
            task.status = "done"
            task.summary = summary or "(worker returned no summary)"

    # -- Plan dispatch ----------------------------------------------------

    @staticmethod
    def run_plan(tasks: list | None, worker, *, queue: TaskQueue | None = None,
                 max_workers: int = 4,
                 timeout: float | None = None) -> str:
        """Execute *tasks* on *worker* with up to *max_workers* parallel
        claimer threads.

        *worker* is any object with ``agent`` and
        ``agent.run(objective) -> str`` (tests pass a stub object whose
        ``agent`` is itself).  ``timeout`` is the coordinator's wall-clock
        budget for the whole plan: when it expires, unclaimed tasks are
        marked failed and claimed tasks are left to wind down (their
        workers are interrupted).  Returns ``"done"`` when at least one
        task succeeded, ``"failed"`` when none did, and
        ``"interrupted"`` on ``KeyboardInterrupt``.  Never raises for
        expected failure modes.
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
                    TeamCoordinator._execute_task(worker.agent, q._tasks[tid],
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
            _try_interrupt(worker.agent)
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
        with self._active_workers_lock:
            self._active_workers = []
        self._pool_queue = None
        self._pool_subtask_count = 0
        self._pool_subtask_seq = 0
        p = self.agent.palette

        print(p.magenta(f"\n  [team] planning run {self._run_id} ...\n"))
        try:
            tasks = self._plan_tasks(goal)
        except PlanParseError as exc:
            print(p.yellow(
                f"  [team] plan unparseable ({exc.reason}); "
                f"running the goal as a single task."))
            tasks = [Task("task", goal, title="full goal")]
        except KeyboardInterrupt:
            return self._finish("interrupted",
                                "Team run interrupted during planning.", [])
        except Exception as exc:
            return self._finish("failed",
                                f"Planner LLM call failed: {exc}", [])

        for i, t in enumerate(tasks, 1):
            print(p.cyan(
                f"  [team] task {i} ({t.task_id}): {t.objective[:120]}"))

        queue = TaskQueue(tasks)
        self._pool_queue = queue
        timeout = config.TEAM_TASK_TIMEOUT
        deadline = (time.monotonic() + timeout) if timeout else None
        pool = _WorkerPool(
            queue, self._worker_factory, config.TEAM_MAX_WORKERS, deadline,
            self, config.TEAM_MAX_SUBTASKS, config.TEAM_MAX_TOTAL_TASKS,
            config.TEAM_MAX_DELEGATION_DEPTH)

        try:
            result = pool.run()
        except KeyboardInterrupt:
            result = "interrupted"
            for t in queue._tasks.values():
                if t.status in ("pending", "claimed"):
                    t.status = "interrupted"
                    t.summary = "interrupted"

        # Report includes subtasks (delegated children last).
        top = [t for t in queue._tasks.values() if t.parent_id is None]
        subs = [t for t in queue._tasks.values() if t.parent_id is not None]
        all_tasks = top + subs
        report = "Team task report:\n" + "\n".join(
            f"- [{t.status}] {t.title or t.task_id}"
            + (f" (subtask of {t.parent_id})" if t.parent_id else "")
            + f": {t.summary[:500]}"
            for t in all_tasks)
        if result == "interrupted":
            summary = f"Team run interrupted. {report}"
        else:
            try:
                summary = self._coordinator_call(
                    _SYNTHESIS_SYSTEM, report,
                    max_tokens=config.TEAM_SYNTHESIS_MAX_TOKENS,
                    timeout=config.TEAM_LLM_TIMEOUT)
            except KeyboardInterrupt:
                result = "interrupted"
                summary = f"Team run interrupted during synthesis. {report}"
            except Exception as exc:
                summary = f"Synthesis LLM call failed ({exc}).\n{report}"

        print(p.magenta(f"\n  [team] report ({result}):\n"))
        for line in summary.splitlines() or [""]:
            print("  " + line)
        print()
        return self._finish(result, summary, all_tasks)

    def _plan_tasks(self, goal: str) -> list:
        """Call the planner LLM, retrying up to TEAM_PLAN_ATTEMPTS times."""
        attempts = max(1, config.TEAM_PLAN_ATTEMPTS)
        last_exc: Optional[PlanParseError] = None
        for attempt in range(1, attempts + 1):
            try:
                if attempt == 1:
                    user = goal
                else:
                    user = _RETRY_PLANNER_USER.format(
                        detail=getattr(last_exc, "detail", "")
                        or (str(last_exc) if last_exc else ""),
                        goal=goal)
                plan_text = self._coordinator_call(
                    _coordinator_system_prompt(), user,
                    max_tokens=config.TEAM_PLAN_MAX_TOKENS,
                    timeout=config.TEAM_LLM_TIMEOUT)
                return _parse_tasks(plan_text,
                                    max_tasks=config.TEAM_MAX_TASKS)
            except KeyboardInterrupt:
                raise
            except PlanParseError as exc:
                last_exc = exc
                if attempt < attempts:
                    print(self.agent.palette.yellow(
                        f"  [team] planner output unparseable "
                        f"(attempt {attempt}/{attempts}); retrying..."))
                    continue
                raise
        raise last_exc or PlanParseError(PLAN_PARSE_FAILED, "no plan")

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
                 "summary": t.summary, "parent_id": t.parent_id}
                for t in tasks
            ],
        }


__all__ = [
    "PLAN_PARSE_FAILED", "PlanParseError", "Task", "TaskQueue",
    "TeamAgent", "TeamCoordinator", "ToolArbiter",
    "DelegationContext", "_current_delegation", "_delegate_subtask",
    "_coordinator_system_prompt", "_default_worker_prompt",
    "_extract_json", "_parse_tasks", "_sanitize_plan",
]
