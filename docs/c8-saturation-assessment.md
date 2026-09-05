# C=8 Saturation — Findings and Plan

Status: review complete; no code changes proposed beyond one config change and
one measurement run.  This document corrects and supersedes the earlier
in-conversation recommendations, which were written before a full review of
`nbchat/core/team.py`.

## 1. What was reviewed

- `nbchat/core/team.py` (planner, `TaskQueue`, `_WorkerPool`, `ToolArbiter`,
  delegation), `nbchat/core/client.py` (streaming client),
  `nbchat/core/config.py` (team_* knobs), `nbchat/tui/agent.py` (turn loop).
- `ninfer/docs/performance.md` — measured Qwen3.8-27B NVFP4 MTP3 corpus
  makespans at C = 1, 2, 4, 8 on RTX 5090.
- `docs/multi-agent-framework-for-c8-saturation.md` — the proposed rewrite.
- `c8lab/` — the validated simulation built in the previous phase.

## 2. Findings — nbchat side

### 2.1 `/team` already implements the decomposition

The feature delivers: LLM planning into up to `team_max_tasks` (8) tasks with
forward-only `depends_on` edges (cycles stripped at parse), claim-based
`TaskQueue` with dependency-ready admission, a `_WorkerPool` with claimer
threads plus a reaper that spawns additional claimers for delegated
subtasks, runtime worker-to-worker delegation with depth/subtask/total caps,
per-run LLM read timeouts (`team_llm_timeout`), deadline-bounded repo lock
acquisition, and per-worker prefixed terminal output.  Failure modes
(planner down, truncated plan, worker crash, timeout, Ctrl+C) degrade to
reported statuses rather than exceptions.

### 2.2 Stream hygiene is already correct (scenario A)

Each turn is one OpenAI streaming request that the client fully drains and
closes (`nbchat/core/client.py`); tool calls run in-process in the worker
thread between requests.  The engine therefore releases the lane as soon as
a turn's tokens finish — the held-stream wedge that motivated the dispatcher
rewrite **does not occur in today's `/team`**.  Scenario B of the c8lab
simulation does not describe current behaviour; it describes a hypothetical
stream-holding client.

### 2.3 The real bottleneck: `team_max_workers` = 4 vs C = 8 lanes

With the default config, at most 4 worker threads run concurrently, so a
`/team` run occupies **at most 4 of the 8 server lanes**.  Half the engine
idles by default on a 4-worker team even when the plan has 8 independent
tasks.  `team_max_tasks` = 8 already admits plans wider than the pool.

## 3. Findings — ninfer side

Measured Qwen3.8-27B NVFP4 MTP3, 75-request fixed corpus (RTX 5090):

| C | Decode tok/s (aggregate) | Avg batch | Makespan speedup vs C=1 |
|---:|---:|---:|---:|
| 1 | 161.7 | 1.00 | 1.00x |
| 2 | 214.0 | 1.91 | 1.29x |
| 4 | 258.2 | 3.67 | 1.61x |
| 8 | 315.3 | 4.76 | 2.09x |

Refreshed-profile run peaks at C=4: **432.9 tok/s** aggregate (avg batch
3.29); C=8 lands at 334.2 (avg batch 2.36) — the C=8 point in that table
saturation-degrades because the corpus's long contexts crowd the shared KV
pool.  KV capacity at C=8 resolves to ~314K tokens.  Two consequences:

- The "1,000 tok/s" figure sometimes quoted for C=8 is **not** the measured
  result for this model; the measured ceiling is ~300-430 tok/s aggregate
  depending on context lengths in flight.
- KV pressure is workload-shaped: 8 concurrent agent sessions of ~40K
  context each sit right at the ~314K pool edge.  Fewer, shorter-context
  workers leave headroom.

## 4. Reconciliation with the c8lab simulation

The simulation remains valid for what it models (client-mapping strategy and
stream lifetime at C=8) but two inputs need reweighting:

- Scenario B (held streams) is a hypothetical, not current behaviour.
- The realistic operating point for `/team` today is **C=4**, so the
  relevant engine figures are the C=4 rows (258-433 tok/s aggregate), and
  the sim's per-turn decode rates at batch<=4 match those.

## 5. Plan

### Step 1 — Config change (minutes, no code)
Raise `team_max_workers` from 4 to 8 (in `repo_config.yaml` or the env).
This is the single highest-impact change: a well-decomposed 6-8 task run
can now fill all 8 lanes.  Keep `team_max_tasks` at 8.  The pool's reaper
and accounting already support arbitrary widths up to the cap.

### Step 2 — Measurement run (one hour)
Run a real parallelizable task (e.g. earnings-research decomposition) via
`/team` with 4 and 8 workers, and record: wall-clock makespan, per-task
times, server-side batch size / lane utilisation / queue depth, and KV pool
high-water.  Compare against the c8lab simulation's scenario-A predictions.
Acceptance: 8-worker makespan within ~20% of the sim prediction; no KV
spill or owner degradation on the server log.

### Step 3 — KV guard (small code, if Step 2 shows pressure)
If 8 wide-context workers crowd the pool, add a soft cap on total in-flight
context (sum of worker context lengths) rather than on worker count — the
pool already exposes `_inflight` and the config already carries context
limits, so this is a bounded change.  Do this only if Step 2's evidence
demands it.

### Step 4 — Dispatcher port (defer; insurance only)
The shared-queue/dispatcher rewrite from the planning doc is **not required
for the measured gain**: today's client already releases lanes between
turns.  Defer it unless a future nbchat change holds streams across tool I/O
or TUI-vs-agent lane contention becomes measurable.  The reference
implementation in `c8lab/clients.py` remains the starting point if needed.

### Explicitly out of scope
- No ninfer-serve changes: the server's measured behaviour at C=4/C=8 is
  healthy (no spill/eviction events in the corpus runs).
- No planner redesign: plan parsing, DAG semantics and caps are already
  tested and correct.

## 6. Risks and open questions

- **KV headroom at 8 workers** is the main unknown; Step 2 measures it.
  Fallback: keep `team_max_workers` at 6.
- The C=8 corpus point degrading below C=4 (334 vs 433 tok/s) means
  "more workers" is not monotonic when contexts are long; the config should
  be tuned per workload shape, and Step 2's data decides the default.
- The sim's filler-priority finding (agents starve TUI prompts) is
  unaffected by the worker-count change and remains a known behavioural
  trade; it only matters when the TUI and a full team run contend.

## 7. Corrections to earlier conversation claims

1. `/team` decomposition was presented as missing work — it exists and is
   the most complete part of the system.
2. The held-stream wedge was presented as an active problem — it is a
   hypothetical for a stream-holding client, not current behaviour.
3. The "1,000 tok/s at C=8" figure was from memory and is not supported by
   the measured doc; the real ceiling is ~300-430 tok/s aggregate.
4. The 200s -> 60-90s projection for a research task still holds in shape
   (parallel substreams remove the serial chain), but the binding constraint
   is `team_max_workers` and KV headroom, not the client mapping.
