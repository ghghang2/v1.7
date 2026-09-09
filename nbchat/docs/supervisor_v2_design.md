# Supervisor v2 + Refinement — a first-principles design

> Status: design + phased implementation plan.
> Trigger: the v1 supervisor "never really worked correctly" — the user did not
> trust small fixes, asked for a ground-up redesign, and asked that the
> prime-agent **refinement** feature (continual-harness / `reviewAutoRefine`)
> be incorporated into nbchat.

---

## 1. First principles — what is a supervisor actually for?

Strip everything away and a supervisor over an agent has exactly one job:

> **Detect, before the user does, that the trajectory is wasting effort or
> heading to the wrong destination — and correct it at the cheapest safe
> point.**

Three corollaries follow, and v1 violates all three:

1. **The supervisor must see more of the trajectory than the assistant's own
   prompt contains.** The assistant is myopic (KV-cache pressure, compaction,
   its own momentum). The supervisor's value is the *wide-angle* view.
   *v1 violation:* the review prompt contains only the goal string, the last
   15 action-log lines and 200-char snippets of the last 10 messages. A
   "stuck in a loop" detection on 200-char snippets is a coin flip.

2. **The supervisor must have memory of its own interventions.** Without it,
   it cannot tell "the assistant ignored my last correction" (escalate) from
   "my last correction worked" (back off). Without it, the same useless
   correction can fire again and again and the user experiences a noisy bot
   or a dead one.
   *v1 violation:* `self._interjection_count` is the entire memory.

3. **The supervisor's output must be machine-checkable, not prose.** Free
   text with an "ON_TRACK" prefix is unparsable in the interesting cases —
   "mostly on track but the push keeps failing because…" is neither.
   *v1 violation:* a 128-token prose blob, classified by a string prefix.

A second, deeper principle separates **two jobs v1 mixed into one class**:

| | In-task supervision | Cross-task refinement |
|---|---|---|
| Question | "Is the *current* task going well?" | "What did this trajectory *teach* that future turns/sessions should remember?" |
| Frequency | Every N seconds while a turn is active | Once per meaningful trajectory (task end, error streak, session end) |
| Action channel | Interjection queue → next safe point in the loop | Harness/memory updates (core memory, episodic store, lessons) |
| Cost profile | Must be *cheap and quiet* when all is well | Can be *thorough* because it runs rarely |
| If it errs | User annoyance (noise) or missed rescue | Memory pollution (worse: it poisons every future session) |

prime-agent's `/refine` and `reviewAutoRefine` are purely the right-hand
column. nbchat's supervisor v1 tried to do both with one 60-second timer and
one 128-token output. That is the root cause of "not useful", not the
cooldown numbers.

## 2. Design

### 2.1 Architecture (one daemon, two jobs, one LLM)

```
                    ┌────────────────────────────────────────────┐
                    │  SupervisionDaemon (replaces Supervisor)   │
                    │  one watchdog thread, two cadences         │
                    │                                            │
                    │  fast tick (interval 30s):  REVIEW         │
                    │  slow gate (per task):      REFINE         │
                    └───────┬─────────────────────────────┬──────┘
                            │ verdict {json}              │ proposal {json}
                            ▼                             ▼
                InterjectionQueue               RefinementExecutor
                (existing, unchanged)          (validates + applies)
                            │                             │
                            ▼                             ▼
                 conversation loop                harness memory
                 drains at safe point             ┌──────────────┐
                                                  │ core_memory  │  (goal/constraints/…)
                                                  │ lessons      │  (NEW table, injected at L1)
                                                  │ refinement_  │  (NEW table: audit + dedup)
                                                  │ history      │
                                                  └──────────────┘
```

Single LLM instance (second parallel slot, or gated through `LaneGate` when
`n_parallel == 1`). The daemon is the *only* thing that changed location-wise
compared to v1 — its cognition changed completely.

### 2.2 The REVIEW job (supervision)

**Cadence.** Fire when `_turn_active` AND `now - last_review >= INTERVAL`
(default 60s). Each review is one non-streaming LLM call.

**Input (the wide-angle prompt, ~4k tokens cap):**
- Goal + last user correction (core memory, existing).
- Trajectory digest (NEW, `harness.digest_trajectory(session_id)`):
  - last K=20 tool turns: tool name, key args (truncated), **outcome
    signature** (ok/fail + first 200 chars of error) — not prose.
  - repeated-call detection *computed in code, not by the LLM*:
    identical `(tool_name, args_hash)` ≥ 3×, or same error signature ≥ 2×.
    The supervisor gets a pre-flagged `SUSPECTED_LOOP: yes/no` line.
    (This is the single biggest reliability win: loop detection is a
    counting problem; make it a count, then let the LLM judge *meaning*.)
- Last 6 review verdicts from the refinement_history table
  (its own intervention memory): what it said, what happened after.

**Output (strict JSON, no prose, max 256 tokens):**
```json
{
  "verdict": "on_track | course_correction | stuck | should_stop",
  "confidence": 0.0-1.0,
  "reason": "<= 40 words",
  "interjection": "<= 80 words, only if verdict != on_track>"
}
```

**Actuation rules (deterministic, in code):**
| verdict | action |
|---|---|
| `on_track` | nothing. Record verdict. |
| `course_correction` | push interjection, cooldown 120s (was 300s). |
| `stuck` | push interjection that *names the suspected loop explicitly* + tells the assistant to change tool/approach. Cooldown 60s. |
| `should_stop` | **does not touch the conversation**. Prints a one-line amber notice in the TUI ("supervisor: suggests stopping — <reason>") and sets `stop_requested` so the next `/status` shows it. The *user* decides. |

**Self-silencing (the anti-noise rule):** track `(last_verdict, interjection,
outcome_after)`. If the same (tool, error-signature) pair was already
interjected on and the assistant continued the same behaviour for N=2 more
reviews, the supervisor stops interjecting on that pair and escalates to a
TUI notice instead. It cannot be fixed by shouting.

**Cost guard:** a review call that returns `on_track` 5× in a row
doubles the interval (up to 4×) until something changes — "quiet when
boring", the way a good butler behaves.

### 2.3 The REFINE job (prime-agent port)

**What prime-agent does (verified against `refine/impl.ts` +
`docs/continual-harness.md`):** a controller receives the conversation
trajectory and the *current harness state* (memory entries with ids) and
returns either **local edits** (this session's state: add/update/delete
memory entries) or a **global refine request** (durable cross-session
lessons). Edits are applied through a `refine` tool with explicit
Create/Update/Delete operations against numbered entries, giving the model
surgical edits instead of free-text appending. Auto-trigger runs
`reviewAutoRefine` periodically; it emits a structured
`{shouldRefine, scope, edits...}` decision and a parse-failure is a no-op.

**nbchat port — the harness state is nbchat's memory stack, which already
exists:** core memory (L1), episodic store, task log, and the `messages`
table. What nbchat *lacks* is (a) a durable **lessons** channel that gets
injected into every future prompt, and (b) a **refinement history** that
prevents re-learning the same lesson or oscillating.

**Triggers (not a timer):**
1. `task_log` row transitions to `done`/`failed` (task boundary — the
   natural "trajectory complete" event, matching prime-agent's session
   boundary).
2. A single task accumulates ≥ 3 failed tool calls (mid-task, the
   trajectory is teaching something about *blockers*).
3. Session end (TUI `/quit` or process exit) — only if a refine has not run
   since the last user message.

**Input** (same shape as `reviewAutoRefine`'s): `<trigger>`,
`<current_harness_state>` (core memory + top-10 lessons with ids),
`<refinement_history>` (last 5 refine rounds: what was proposed, what was
applied), `<trajectory>` (last ~12k chars of the task's tool-turn digest).

**Output (strict JSON):**
```json
{
  "should_refine": true,
  "scope": "session | global",
  "edits": [
    {"op": "create|update|delete", "id": "L3",
     "content": "<= 60 words, imperative, actionable",
     "rationale": "<= 20 words"}
  ],
  "goal_delta": "<optional: revised goal/constraints wording>"
}
```

**Executor (deterministic, in code — this is where safety lives):**
- Validate: max 3 edits/round; 60-word cap; `delete` allowed only on
  `session` scope (global memory can only grow or be amended, so a single bad
  round cannot destroy shared knowledge).
- **Dedup:** cosine/substring similarity (threshold 0.85) against existing
  lessons; near-duplicates become `update` (strengthen) instead of `create`.
- **Audit:** every round — proposed, applied, rejected — lands in
  `refinement_history` (also used as the review job's own memory, 2.2).
- **Rollback:** each round is one transaction; `/refine undo` rolls back the
  last round. (prime-agent keeps `rollbackOf`/`rollbackId` in its state for
  the same reason.)
- `scope: global` writes to a session-agnostic lessons table (scope column);
  `scope: session` writes per session. Both are injected at L1 of the
  context manager (new `_get_harness_lessons_block`, capped at top-10 by
  recency×usefulness).

**User control:** `/refine status` (show current harness state + last rounds),
`/refine undo`, `/refine <lesson>` (manual append), `/refine off|on`.

### 2.4 Schemas (additions to `db.init_db`, same style as existing)

```sql
CREATE TABLE IF NOT EXISTS lessons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,          -- '' = global
    scope       TEXT NOT NULL DEFAULT 'session',
    content     TEXT NOT NULL,
    rationale   TEXT DEFAULT '',
    origin      TEXT DEFAULT 'refine',  -- 'refine' | 'user'
    round_id    INTEGER,
    useful      INTEGER DEFAULT 0,      -- bumped when injected & task succeeds
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS refinement_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- 'review' | 'refine'
    round_id    INTEGER,
    payload     TEXT NOT NULL,          -- the full JSON verdict/proposal
    action      TEXT DEFAULT '',        -- 'on_track'|'interjected'|'notice'
                                                      -- |'applied'|'rejected'|'error'
    detail      TEXT DEFAULT '',
    ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lessons ON lessons(session_id, useful DESC);
CREATE INDEX IF NOT EXISTS idx_refh ON refinement_history(session_id, kind, id DESC);
```

### 2.5 Files

| File | Change |
|---|---|
| `nbchat/core/supervisor.py` | `Supervisor` → `SupervisionDaemon`: two cadences, JSON verdicts, actuation table, self-silencing, quiet-mode backoff, `stop_requested`. Keeps `ask()`, `InterjectionQueue`, `/sup`. Fixes `stop()` (join up to `LLM_TIMEOUT+5`, log on abandon). |
| `nbchat/core/refinement.py` (NEW) | `digest_trajectory()`, `HarnessState` view over core memory + lessons, `RefinementExecutor` (validate/dedup/apply/undo), triggers. ~400 lines. |
| `nbchat/core/db.py` | two new tables + `record_refinement()`, `get_lessons(scope)`, `undo_refine_round()`. |
| `nbchat/core/context_manager.py` | new L1 block `_get_harness_lessons_block` appended after core memory. |
| `nbchat/core/config.py` + `repo_config.yaml` | new keys (defaults below); `supervisor_cooldown` 300→120. |
| `nbchat/tui/app.py` | `/refine` subcommands; amber `should_stop`/escalation notices at prompt. |
| `tests/test_supervisor.py` | extend: JSON verdict parsing, actuation rules, self-silencing, stop() join. |
| `tests/test_refinement.py` (NEW) | digest, triggers, executor validation/dedup/undo, scope rules. |

Defaults:
```yaml
supervisor_interval: 60
supervisor_cooldown: 120
supervisor_quiet_backoff: true
refinement_enabled: true
refinement_max_edits: 3
refinement_dedup_threshold: 0.85
refinement_inject_top: 10
```

## 3. Implementation plan (phases, each independently testable)

- **Phase 1 — `core/refinement.py` + db + tests** (no live LLM needed):
  `digest_trajectory`, harness-state view, executor with validation, dedup,
  audit, undo. Unit-testable against a temp DB.
- **Phase 2 — `SupervisionDaemon` rework**: JSON verdict parse (with
  fallback on malformed output → treat as `on_track`, log), actuation rules,
  self-silencing, quiet backoff, `stop()` fix. Extend `test_supervisor.py`.
- **Phase 3 — wiring**: context-manager L1 lessons block; triggers from
  `task_log`/failed-tool threshold; TUI `/refine` commands and notices;
  config keys + yaml.
- **Phase 4 — hardening pass**: prompt budget checks (trajectory digest
  capped, review prompt ≤ 4k tokens), end-to-end test with a scripted
  fake client (loop detection → stuck verdict → interjection → next review
  sees the after-effect), and a refine round on a fake trajectory.

Estimated: Phase 1 ~450 lines, Phase 2 ~250 lines rework, Phase 3 ~150
lines, Phase 4 ~200 lines of tests.

## 4. Why this answers "actually useful"

1. Loop/stuck detection is now *computed*, not prompted — the failure mode
   users actually care about becomes reliable.
2. The supervisor has memory and self-silencing — it gets quiet when its
   advice is being ignored instead of repeating itself, and it can prove
   what it said via `/refine status` (history is queryable).
3. `should_stop` gives a visible *non-interrupting* channel — the user is
   informed without the assistant being hijacked mid-turn.
4. Refinement reuses prime-agent's exact mental model (harness state →
   local edits / global lessons, audit + rollback) but on nbchat's existing
   memory stack, so lessons actually get injected into future prompts —
   which is the part that makes a refine feature "cool": the agent gets
   measurably better at the user's recurring task types across sessions.
5. Every LLM output is strict JSON with deterministic fallbacks; no more
   prose-prefix parsing.
