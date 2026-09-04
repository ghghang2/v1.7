"""nbchat.tools.delegate_task
============================

Delegation tool for team (``/team``) worker agents.

A team worker is a single claimer thread on a shared task queue.  When a
worker finds its task is too coarse for one agent, it calls
``delegate_task`` to push independent subtasks onto the same queue; the
team worker pool spawns additional claimer threads for them (up to
``team_max_workers`` total) so genuinely independent work runs in
parallel instead of serially on a single worker.

This module is **stateless** like the other tools: it resolves the
current team delegation context from a module-level slot in
:mod:`nbchat.core.team` (:data:`nbchat.core.team._current_delegation`)
that the coordinator sets on the executing agent for the duration of
each task.  Called outside a team run (e.g. from the main terminal
agent) it reports that delegation is unavailable rather than failing.

Bounded by design (see ``docs/multi_agent.md``):

* ``team_max_workers``  - total in-flight claimer threads (coordinator
  workers + delegated workers);
* ``team_max_delegation_depth`` - a worker at depth ``d`` may only
  delegate while ``d < limit`` (deeper subtasks run inline);
* ``team_max_subtasks`` - hard cap on worker-delegated subtasks per run.
"""
from __future__ import annotations

import json

name = "delegate_task"
description = (
    "Delegate an independent subtask to another team worker so it runs in "
    "parallel on an idle worker slot. Only available to /team worker "
    "agents. The subtask must be fully self-contained (the sub-worker sees "
    "ONLY its objective) and must not depend on the outcome of your own "
    "task or of any other delegated subtask. Use it only when your task "
    "genuinely splits into independent pieces; the parent task is not "
    "marked done until every delegated subtask is finished. Returns JSON "
    "with the subtask id and whether a parallel worker was spawned, or an "
    "error key when delegation is unavailable or the delegation limits "
    "(depth / subtask cap) are reached."
)


def func(objective: str, title: str = "") -> str:
    """Delegate *objective* as a new subtask of the current team task."""
    from nbchat.core.team import (
        _current_delegation,
        _delegate_subtask,
    )
    ctx = _current_delegation.get()
    if ctx is None:
        return json.dumps({
            "error": (
                "delegation is only available to /team worker agents "
                "during an active team run"
            ),
        })
    return _delegate_subtask(ctx, objective, title)


__all__ = ["name", "description", "func"]
