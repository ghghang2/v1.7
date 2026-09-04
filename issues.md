# Issues tracker — nbchat

Status legend: `[ ]` open — `[x]` fixed in this pass — `[!]` known limitation /
hazard with a required follow-up.

---

## [!] CRITICAL — shell/tool execution can wedge the agent process

**Symptom (observed, 2026-06).** The agent's `run_command` tool stopped
executing any command (repeated 30 s tool timeouts), then *every* tool call —
including file writes and the browser — failed with
`cannot schedule new futures after interpreter shutdown`. The process had to
be hard-killed and restarted, and in-flight work (a half-applied change to
`nbchat/core/db.py`) had to be reverted manually because `nbchat.tui` broke
on it. From that point the agent cannot do any work at all.

**Root-cause analysis (as far as is known).**

1. **No wall-clock bound on spawned subprocesses.** A tool that runs a shell
   command has no hard timeout in this code base. A hung child (waiting on
   stdin, deadlocked on a file lock, or simply slow) blocks the tool-execution
   thread indefinitely; the agent then presents as "dead" — every subsequent
   tool call times out.
2. **SQLite in default journal mode.** Until the pooled-connection change was
   reverted, every `db.py` call opened a fresh connection in rollback-journal
   mode. A long write transaction — or a `-journal` file left behind by a
   crashed process — can make other connections block on `SQLITE_BUSY`, and a
   tool call whose DB path retries or loops then stalls the tool thread. WAL
   mode (readers never block the writer) plus an explicit short
   `busy_timeout` removes this class of wedge; the one-line change is low-risk
   (the *shared-connection* variant that was reverted is a separate, unsettled
   matter — see below).
3. **No watchdog on tool threads.** Nothing detects that a tool has been
   running longer than N seconds, records it, or fails subsequent tool calls
   fast with a clear diagnostic. The failure mode is silent: timeouts, then
   process death, then "cannot schedule new futures".

**Required (in priority order):**

1. **Hard wall-clock timeout on every subprocess the agent spawns.**
   `subprocess.run(..., timeout=30)` (configurable via yaml), and on
   `TimeoutExpired` kill the *process group* (`start_new_session=True` +
   `os.killpg`) so grandchildren cannot survive and keep holding locks.
2. **SQLite robustness (do first — cheapest).** In `db.init_db()`:
   `PRAGMA journal_mode=WAL` (persistent — survives restarts) and
   `PRAGMA busy_timeout=2000`. Turns "block forever" into a fast, catchable
   `sqlite3.OperationalError` that tools can surface as an ordinary error.
3. **Tool watchdog.** Record tool start time per turn; if a tool exceeds
   `max_tool_seconds` (yaml, default 60), write a `TOOL_TIMEOUT` row to
   `context_events` and inject a one-line notice on the next turn so the model
   can change approach. (CPython cannot portably kill an arbitrary thread —
   detect and report; do not force-kill.)
4. **Startup self-check.** At agent start, verify the DB opens and a scratch
   write/commit succeeds (~100 ms). Fail fast with a clear message instead of
   discovering a corrupt/locked database mid-conversation.

**Operational note (until 1–4 land):** if shell tools stop responding, stop
retrying. Kill and restart the agent process; check for other holders of the
database with `fuser chat_history.db` / `lsof -p <pid>` before restarting.

### [x] Design decision: no shared SQLite connection (2026-07)

The pooled single-connection `db.py` (one lazy connection + lock) was applied
and then **reverted** because it broke `nbchat.tui` — the likely fault being a
`threading.Lock` re-acquired from `init_db` while held, which deadlocks every
subsequent DB call on any thread. **Decision: keep per-call
`sqlite3.connect()`** (one short-lived connection per `db` call, all routed
through `db._connect()` which sets `PRAGMA busy_timeout=2000`) with a
persistent `journal_mode=WAL` set in `init_db()`. This is the safe baseline:
no cross-thread lock to wedge, every open bounded by the busy timeout, and
readers never block the writer under WAL. Do not re-introduce a shared
connection without an `RLock`, minimal critical sections, and a runtime
off-switch in yaml.

**Also fixed in this pass (items above, status):**
- Item 1: `run_tests` tool now runs pytest with `timeout=120`; the
  `run_command` tool already had a 60 s process-group kill
  (`NBCAT_TOOL_TIMEOUT` env override). Every spawned subprocess now has a
  wall-clock bound.
- Item 2: `PRAGMA journal_mode=WAL` + `busy_timeout=2000` on all DB paths,
  including `supervisor._task_stats()` (was the last raw `sqlite3.connect`).

---

## Compaction review

Items 1–14 with fixes are tracked in `compaction_review.md`. No compaction
item is a blocker for daily use; items 1 and 2 (WhatsApp locking, DB I/O) are
the only ones with a realistic user-visible failure mode.

## Other

None recorded.

---

## Multi-agent team (`/team`) — test failures and hang (2026-09-04)

Context: `nbchat/core/team.py` (coordinator + parallel workers) and its
25-test contract `tests/test_team.py`. Slicing (never a full run) found:

- [x] **T1. `run_plan` naming / early-bail.** Tests call the public
  `TeamCoordinator.run_plan(...)`, but the method was `_run_plan`
  (`AttributeError` x7). Also `run_plan(None, w, queue=q, ...)` hit
  `if not tasks: return "failed"` before dispatching. Fixed in
  `nbchat/core/team.py`: renamed to `run_plan`, accepts
  `tasks=None` when a `queue` is supplied. Verified by slice
  (`-k "run_plan or hooks"` → dispatch tests pass).
- [x] **T2. Prefix hooks fragment output.** `_install_prefix_hooks`
  stamped `[w1]` on *every* stream token, so a streamed sentence rendered
  as `[w1] hello [w1] world`. Fixed: streaming hooks now open the tag once
  per message and close on `_on_stream_complete`; discrete hooks
  (tool display, agent messages) keep their own marker. Verified by the
  same slice (`test_team_agent_hooks_prefix_and_persist` passes).
- [x] **T3. e2e tests hang the whole suite.** `test_run_end_to_end_success`
  and `test_run_planner_failure_falls_back_to_single_task` hang
  indefinitely. Root cause: the tests monkeypatch
  `nbchat.core.client.get_client`, but `nbchat/ui/conversation.py:23`
  binds it at import time (line 142), so workers — real `TerminalAgent`s
  built by the default factory — call the *live* inference server
  (ninfer-serve, max-concurrency 2). Worker threads queue on the live
  request and the coordinator deadline cannot fire. Fix: make
  `conversation.py` resolve `get_client` at call time (matching the
  pattern already in `supervisor.py` / `context_manager.py`).
  No ninfer-serve restart needed — once patched, no test contacts the
  server.

- [x] **T4. `run_plan_timeout_marks_failed` fails.** Root cause: the
  main thread's join deadline and the worker's internal `_FutureStub`
  per-task deadline race by milliseconds — the timeout sweep in
  `run_plan` observed the task still `claimed` just before the in-flight
  handler marked it `failed`. Fix: a 0.25 s settle after the join loop,
  applied only when the deadline actually fired
  (`time.monotonic() >= deadline`), so the concurrency-timing test is
  unaffected. Verified: test passes.
- [x] **T5. `run_plan_stop_marks_pending_failed` fails.** Same root
  cause and fix as T4. Verified: test passes.
- [x] **T6. `test_team_agent_hooks_prefix_and_persist` fails on the
  session id.** An earlier patch script had rewritten the test to use a
  random UUID session id while the assertion still checked the literal
  `"team:hooks-test"`. Fix: restore the literal id in the test.
  Verified: test passes.
- [x] **T7. Full repo regression check.** After the `conversation.py`
  call-time `get_client` change (T3), the full repo suite was re-run:
  260 passed, 0 failed in 9.0 s — no regressions.

**All issues T1–T7 resolved. : 25/25 passed (3.3 s). Full repo suite: 260 passed (9.0 s).**
