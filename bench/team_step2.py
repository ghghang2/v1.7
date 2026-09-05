#!/usr/bin/env python3
"""Step-2 measurement run driver (docs/c8-saturation-assessment.md).

Runs a real parallelizable goal through the /team coordinator at the
requested worker width and relies on the built-in TeamRunMetrics recorder
(nbchat/core/team_metrics.py) to write logs/team_metrics/<run_id>.jsonl.

Usage:  python bench/team_step2.py [workers]
Prints the run status and the recorded summary line at the end.
"""
from __future__ import annotations

import json
import sys
import threading
import time

from nbchat.core import config
from nbchat.core.team import TeamAgent, TeamCoordinator, ToolArbiter

GOAL = (
    "Write one short paragraph (2-3 sentences) about each of these "
    "topics, then finish.  Topics: (1) the 1912 sinking of the Titanic; "
    "(2) the 1969 Apollo 11 landing; (3) the invention of the printing "
    "press; (4) the 1859 Carrington solar storm; (5) the first "
    "transatlantic telegraph cable; (6) the 1903 Wright brothers flight; "
    "(7) the discovery of penicillin; (8) the 1936 Berlin Olympics.  "
    "Each topic is an independent task.  Do not use any tools; just "
    "write the paragraph and stop."
)


def main() -> int:
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else config.TEAM_MAX_WORKERS
    config.TEAM_MAX_WORKERS = workers  # drive the width per invocation

    agent = TeamAgent(color=False)
    coordinator = TeamCoordinator(agent)
    with ToolArbiter():
        t0 = time.monotonic()
        result = coordinator.run(GOAL)
        wall = time.monotonic() - t0

    print(f"== team run finished: status={result.get('status')} "
          f"wall={wall:.1f}s workers={workers}")
    for t in result.get("tasks", []):
        print(f"  [{t.get('status', '?')}] {t.get('task_id', '?')}: "
              f"{(t.get('title') or '')[:60]}")

    # Locate the JSONL this run wrote (latest file mentioning our run id).
    from nbchat.core.team_metrics import _METRICS_DIRNAME as metrics_dir
    candidates = sorted(metrics_dir.glob("*.jsonl"),
                        key=lambda p: p.stat().st_mtime)
    for path in reversed(candidates):
        summary = None
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") == "summary":
                summary = row
        if summary is not None and summary.get("makespan_s", 0) > 1:
            print(f"== metrics: {path.name}")
            print(json.dumps(summary, indent=2))
            break
    else:
        print("== no metrics summary found in " + str(metrics_dir))
        return 2
    return 0 if result.get("status") == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
