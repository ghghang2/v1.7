# Tracker: Fix /team stuck run

## Goal
`/team` (nbchat core team feature) hangs: worker threads claim tasks, leave them
`claimed`, and the coordinator never settles the run (only the deadline sweep
saves it). Fix the root cause; verify with a real run; keep metrics recording
(the earlier task) intact.

## Constraints
- Do NOT repeat prior work (check git log + this tracker before acting).
- Keep logs/team_metrics/*.jsonl recording working.
- tests/test_team.py: user said to ignore the 2 pre-existing failures (do NOT
  re-investigate them unless a fix regresses them).

## Known facts (from prior turns — do not re-derive)
- Metrics hook (team_metrics.py) committed at 872b5c7 and working.
- Bench `bench/team_step2.py 8` → status=failed, wall=13.6s, all 8 tasks
  `claimed`, no worker output; metrics show peak_inflight=0, makespan=0.01,
  samples=0.
- Live `/team` (server :8080) → user reports it is STUCK (hangs).
- Config: task_timeout=900, llm_timeout=120.0, max_workers=8, enabled=True.
- Server :8080 is up (/health ok).

## Hypothesis (leading)
A worker claimer thread raises an UNHANDLED exception inside
`_WorkerPool._execute_one` (before/around `TeamCoordinator._execute_task`), so
the thread dies. Because the task was already set `claimed` by `queue.claim()`
and `_execute_task`'s `except Exception` never runs, the task stays `claimed`
forever, `_inflight` returns to 0, reaper won't respawn (pending_count==0),
and `run()` waits until the deadline → in live /team with no/long deadline it
hangs. Debug monkeypatch on `_execute_task`/`_worker_run` never fired, which
points to a raise BEFORE `_execute_task` (i.e. in `_make_delegation`/
`_make_worker` or token setup).

## Investigation steps
- [x] Read TaskQueue claim/wait_claim/notify/pending_count (lines 479-620).
- [x] Read _WorkerPool._spawn_claimer/_claim_loop/_execute_one/_reaper/run
      (lines 1140-1300).
- [x] Read _worker_run, _clip_summary (1060-1140).
- [x] Read _make_delegation + _execute_task + run_plan (1442-1560).
- [ ] Read _make_worker (line 1423) — does it RETURN False on failure or
      raise? If it raises, that's the unhandled path.
- [ ] Read _FutureStub (result()) — does it swallow/propagate?
- [ ] Inspect debug script /tmp/dbg_team.py output (logs/dbg_team.log) for the
      real traceback. Empty so far (buffering or not-yet-done).
- [ ] Check git log for prior /team worker fixes (avoid repeating).

## Fix plan (pending confirmation of root cause)
- [ ] Make _claim_loop/_execute_one catch exceptions per task and mark the
      task failed instead of dying silently (defense: run always settles).
- [ ] Fix the actual raising call (likely _make_worker / delegation / worker
      agent construction).
- [ ] Verify with a single-task /team then the 8-task bench; confirm metrics
      show inflight>0 and tasks reach terminal states.

## Log
- (entries appended below as work proceeds)
