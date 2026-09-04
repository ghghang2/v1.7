# Multi-agent coordinated task execution — design

Status: Phases 1–3 implemented; Phase 4 (live-server e2e) not yet implemented.
This document is the authoritative design reference for `nbchat/core/team.py`.

## Phases

| Phase | Scope | Status |
|-------|-------|--------|
| 1 — Core infrastructure | `TaskQueue` (FIFO claim, atomicity), plan/JSON parsing, `TeamAgent` output hooks (per-message `[wn]` prefix), `run_plan` parallel dispatch with per-run deadline and post-deadline settle. No LLM required. | **Done** (20 tests) |
| 2 — Coordinator cycle | `TeamCoordinator.run()`: plan → dispatch → collect → verify (run_tests) → integrate (commit+push) → synthesize (final LLM report). Mocks `_coordinator_call` so tests run without a live server. | **Done** (5 tests, mock client) |
| 3 — TUI + config wiring | `/team <goal>`, `/team` (status), `/team stop` in the TUI REPL loop; `TEAM_*` knobs in `repo_config.yaml` + `nbchat/core/config.py`; `db.get_history()` for transcript retrieval. | **Done** |
| 3.5 — ToolArbiter | `ToolArbiter` wraps `nbchat.ui.tool_executor.run_tool` at module level to serialize repo-mutating tools (`run_command`, `make_change_to_file`, `create_file`, `push_to_github`) and `run_tests` with per-resource re-entrant locks. `install()`/`remove()` idempotent. | **Done** (4 tests) |
| 4 — Live-server e2e | Two real `TerminalAgent` workers on disjoint file-creation tasks, hitting a live inference server, skipped automatically when the server is down. | **Not yet implemented** (the shipped e2e tests use a mock client; a live-server variant is future work) |

Phases 1–3 are fully implemented and tested, including the `ToolArbiter`
safety net and the `/team` TUI command. Phase 4 (live-server e2e) is not
yet implemented.

## Goal

Maximize task-completion throughput by letting a team of 4+ parallel agents
tackle one goal in a coordinated fashion, while preserving the invariants the
single-agent product already holds:

* every file mutation is safe (no lost / interleaved writes),
* git state is never corrupted by concurrent operations,
* a failed sub-task degrades the run, never the repo,
* the user always gets an honest final report.

## Why this shape

The existing stack is already 90% multi-agent ready:

| Layer | Multi-agent safe? | Evidence |
|---|---|---|
| `TerminalAgent` state | yes | every agent owns `session_id`, `history`, `task_log`, `_stop_event`, `_send_lock`, `ImportanceTracker` |
| SQLite (`nbchat/core/db.py`) | yes | WAL journal, per-operation connections, `busy_timeout=2000`; all tables keyed by `session_id` |
| Monitoring | yes | `SessionMonitor` per session, guarded by `_monitors_lock` |
| LLM server (llama.cpp) | yes (queued) | `n_parallel: 2`; requests beyond the slots queue inside the server — throughput is slot-bound, correctness is not |
| Tools: `run_command`, `make_change_to_file`, `create_file`, `push_to_github`, `run_tests` | **no** | shared working tree, shared `.pytest_cache`/`__pycache__`; tool bodies run on a *shared* 4-thread executor |

So the only new machinery needed is (a) a shared work queue with claim
semantics, (b) arbitration of repo-mutating tools, and (c) a coordinator that
plans / verifies / integrates / synthesizes.

## Roles

```
user  ──/team <goal>──▶  TeamCoordinator
                              │  1. plan (LLM, non-streaming, supervisor slot)
                              │  2. dispatch: N worker threads pull from TeamQueue
                              │  3. verify: run_tests (once, after all tasks)
                              │  4. integrate: single commit + push (only if green)
                              │  5. synthesize: final user-facing report (LLM)
                              ▼
        TeamAgent #1..#N  (fresh TerminalAgent per worker, session team:<run>:w<i>)
        each worker: claim → run agentic turn on the task → report
```

* **Coordinator** — one always-on LLM client for planning and synthesis
  (non-streaming calls on the model, like the supervisor). Owns the queue,
  the arbiter lifecycle, verification and integration.
* **Workers** — `TeamAgent(TerminalAgent)` instances, each with its own
  session (`team:<runid>:w<i>`), its own history, and output hooks that
  prefix every line with a coloured `[W<i>]` label so interleaved streams stay
  readable.
* **ToolArbiter** — safety net: maps repo-mutating tools to named resources
  and serializes them with per-resource locks (re-entrant per OS thread).

## Worker contract (the prompt is the contract, the arbiter is the net)

Workers are told:

1. Work only on your assigned task; other agents are running concurrently on
   other parts of the repo.
2. Reads are always fine. Writes must stay inside your task's scope.
3. **Do not run the full test suite** (`run_tests`) — the coordinator
   verifies once at the end. Read-only checks (`grep`, `py_compile`) are fine.
4. **Do not commit or push** (`push_to_github`, git commit) — the coordinator
   integrates once after verification, so commits never interleave with
   other workers' in-flight edits.
5. End the final message with exactly one machine-readable line:

   ```
   TASK RESULT: <DONE|FAILED|BLOCKED>: <one-line evidence>
   FILES: <comma-separated paths created or modified, or "none">
   ```

Even if a worker ignores 3–4, the arbiter still serializes `run_tests` and
the git tools, so the worst case is slowdown, not corruption.

## Concurrency model

* **TeamQueue** — single `threading.Lock`; tasks move
  `queued → running → done|failed`. `claim(worker)` is atomic; a task is
  claimed exactly once (guaranteed by the lock; stress-tested).
* **ToolArbiter** — resources:
  * `repo` ← `run_command`, `make_change_to_file`, `create_file`,
    `push_to_github` (working tree + git index/refs),
  * `tests` ← `run_tests` (pytest caches).
  Unmanaged: `browser`, `get_weather`, `repo_overview`, `send_email`
  (side effects are per-call, no shared mutable state).
  Installation wraps `nbchat.ui.tool_executor.run_tool` at module level
  (the single choke point through which every agent executes tools);
  `remove()` restores the original. Idempotent. Re-entrant per thread
  (a tool body executing on a pool thread that already holds the resource
  must not deadlock).
* **LLM slots** — with `n_parallel: 2`, up to 2 of the N workers actually
  generate at once; the rest queue inside llama.cpp. This is intentional
  backpressure, not a bug. Raising `n_parallel` (and memory) linearly
  increases effective throughput.
* **Per-agent turn serialization** — each worker's turns are already
  serialized by its own `_send_lock`; workers never share a history, so no
  cross-worker history corruption is possible.
* **DB** — per-session rows; WAL readers never block writers.

## Failure modes and mitigations

| Failure | Mitigation |
|---|---|
| Planner returns bad JSON | fall back to a single-task plan (the goal itself) — the run degrades to single-agent, never dies |
| Worker turn crashes / dies | `TeamAgent` turn wrapper catches; task marked `failed`, retry once with the error attached; then continue |
| Worker ignores `TASK RESULT` line | parsed report defaults to `FAILED: no result line`; coordinator treats as failed |
| Task hangs (tool timeout already caps single tools, but a whole turn can drift) | coordinator watchdog: `team_task_timeout` → `worker.interrupt()`; unfinished task marked failed |
| Tests red after all tasks | one coordinator repair turn (real agentic turn on the coordinator's own quiet agent); re-verify; if still red: **no push**, honest report |
| Push refused / git fails | reported in the synthesis; file changes remain on disk |
| User interrupts mid-run | `/team stop` (or Ctrl+C at REPL exit) sets the stop event: in-flight turns stop at the next safe point, claimed-but-unreported tasks are marked failed, arbiter is always uninstalled (finally block) |
| Two workers touch the same file | arbiter serializes writes; last writer wins per file — acceptable for phase 1 (documented); planner is instructed to split by disjoint file sets |
| Crash mid-run | work on disk is uncommitted (durability trade-off, documented); committed state is untouched, repo is never left mid-git-operation because git ops are coordinator-only and lock-serialized |

## Configuration (`repo_config.yaml`)

All optional at load time (`.get()` with defaults), so older configs work:

```yaml
team_enabled: true
team_max_workers: 4        # parallel workers per team run
team_task_timeout: 900     # seconds; coordinator interrupts a drifting task
team_plan_max_tokens: 2048
team_synthesis_max_tokens: 1536
team_llm_timeout: 120      # per planner/synthesizer LLM call
```

## User interface (TUI)

```
/team <goal>       start a coordinated run (background; live [Wn] output)
/team              status of the current/last run (queue snapshot)
/team stop         stop the current run
```

## Test plan (tests/test_team.py)

Phase 1 (no LLM):
1. Queue: FIFO claim, atomicity under 8-thread contention (each task claimed
   exactly once), lifecycle transitions, `all_done`.
2. Arbiter: serialization (max concurrency 1 under 2 threads), per-thread
   re-entrancy, install/remove round-trip + idempotence, unmanaged tools
   pass through.
3. Report/plan parsing: fenced JSON, malformed JSON fallback, missing
   `TASK RESULT` line default.
4. `TeamAgent` output prefixing (capsys).
Phase 2 (mock LLM, injected `plan_fn` / `turn_fn`):
5. Full coordinator cycle: plan → dispatch → collect → verify → integrate →
   synthesize; failures retry once; verify-red blocks the push;
   `stop()` terminates promptly; arbiter always uninstalled.
Phase 4 (live server, skipped when the inference server is down):
6. Two real workers, two disjoint file-creation tasks, end-to-end.
   *Not yet exercised* — the shipped e2e tests (item 5) use a
   mock client; a live-server variant is future work.

## Non-goals (phase 1)

* Dependency chains between tasks (tasks are independent by design —
  throughput comes from parallelism, not pipelining).
* Per-worker git worktrees (single shared tree + coordinator-only git is
  simpler and sufficient for the current task mix).
* Voice announcements for team events (hook exists via `on_event`; wiring
  Alfred is future work).

## Status (2026-09-04)

**Implementation complete and shipped** (commits `3a62012` WIP + `2925925`
fixes, on `main`):

* `nbchat/core/team.py` — plan parsing, `TaskQueue` claim semantics,
  parallel `run_plan` dispatch with a per-run deadline and a post-deadline
  settle (claimed tasks are swept to `failed`), `TeamAgent` output hooks
  (one `[wn]` marker per message, not per token), `TeamCoordinator.run()`
  end-to-end (plan → dispatch → synthesize → persist).
* `tests/test_team.py` — 25/25 passing in ~3.3 s with no live server
  (the e2e tests inject a mock client; `nbchat/ui/conversation.py` now
  resolves `get_client` at call time so the mock binding reaches workers).
* Full repo suite: 260 passed, 0 failed.

Defects found during bring-up (T1–T6, all resolved; tracked in
`issues.md`, section "Multi-agent team") and their fixes:

| # | Defect | Fix |
|---|--------|-----|
| T1 | `run_plan` named `_run_plan`; `tasks=None` early-bail | public rename; queue-supplied runs |
| T2 | Prefix hook stamped `[wn]` per stream token | once-per-message open/close |
| T3 | e2e tests hung (workers hit the live server via an import-time `get_client` binding) | call-time resolution in `conversation.py` |
| T4 | Timeout sweep raced the in-flight worker handler (`claimed` stuck) | post-deadline sweep + 0.25 s settle |
| T5 | Same root cause as T4 (pending-marking test) | same fix |
| T6 | Hooks test asserted a stale literal session id | restored literal id |
