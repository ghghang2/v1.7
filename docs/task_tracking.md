# Task Completion Tracking

Status: implemented (v1).

## 1. Purpose

We need to know, quantitatively, how effectively the agent completes tasks —
under different conditions, across time. Without per-task records, the only
available signal is anecdotical ("it seemed to work"), which cannot drive
improvements: we cannot tell a prompt regression from a flaky inference
backend, we cannot see that stall-recovery nudges silently cost 40% of turns
extra tool calls, and we cannot measure whether a change to compression
reduced redundant re-reads.

This feature adds a systematic, per-turn **task record**: one row per
user-initiated turn of the agentic loop, written to the existing SQLite
store, queryable later for analysis.

## 2. Unit of analysis: the "task"

A **task** is one user-initiated turn: from the user message (or an
injected email / voice transcript) until the agentic loop terminates —
whether by a final reply, by the turn being interrupted, or by an error.

Rationale:

- It matches how work arrives in this system (one prompt, one loop run).
- Its boundaries are unambiguous and machine-observable: the loop start is
  `_run_conversation_loop()` entry, the loop end is any `break`/exception.
- It is the natural unit for the metrics the user asked for (completion,
  duration, interventions, redundancy) — all of which are turn-scoped.

A multi-turn project (e.g. a long development task spanning several
messages) is analyzed as a *sequence of tasks*; session-level rollups are a
trivial `GROUP BY session_id` query over that sequence and are not the
storage model.

## 3. What is recorded

Fields split into two groups: **machine-derived** (counted by hooks in the
conversation loop, always available) and **analytic** (derived from the
persisted history at close time, plus fields reserved for later annotation).

| Field | Source | Notes |
|---|---|---|
| `session_id` | agent | Prefixed id (`tui:` / `wa:` / bare) |
| `request_text` | user message | First 2000 chars |
| `request_chars` | user message | Full length (pre-truncation) |
| `status` | loop exit path | `complete` / `interrupted` / `failed` / `in_progress` (orphan) |
| `completion` | analytic, v1: auto | `complete` / `partial` / `not_completed` / `unknown`; later overridable by annotation |
| `nature` / `difficulty` | analytic | `unknown` in v1 (auto-classification is a follow-up); overridable |
| `started_at`, `ended_at`, `duration_s` | hooks | Wall clock |
| `num_llm_calls` | hook | Every `create()` in the turn, retries included |
| `num_tool_turns` | hook | LLM responses that requested tools |
| `tool_calls_total` | hook | Individual tool calls executed |
| `tool_calls_by_name` | hook | JSON `{name: n}` |
| `tool_calls_failed` | hook | Structured-error results (reuses `db.is_tool_error`) |
| `redundant_tool_calls` | analytic | Calls whose (name, normalized args) fingerprint was already issued *earlier in the same turn* |
| `redundant_reads` | analytic | Subset of the above with a read tool |
| `redundant_writes` | analytic | Subset with a write tool |
| `stall_events` | hook | Stall-nudges injected this turn |
| `truncation_events` | hook | Continue-nudges injected this turn |
| `stream_retries` | hook | Mid-stream transport drops recovered this turn |
| `text_toolcall_recovery` | hook | Legacy XML tool calls recovered from text |
| `user_interventions` | analytic | Non-task user rows logged in-session between start and end (redirects; supervisor interjections excluded — agent-side) |
| `llm_latency_s` | hook | Sum of per-call wall time |
| `prompt_chars` / `completion_chars` | hook | Approximate token load per call (context is not token-counted in the loop) |
| `max_context_chars` | hook | Peak `len(messages)` context seen |
| `error_count` | hook | Tool failures + surfaced stream errors |
| `final_response_chars` | analytic | Length of the last assistant row of the turn |
| `turn_ids` | analytic | JSON list of chat_log ids touched by the turn |
| `annotations` | JSON | Free-form: completion override, nature, difficulty, notes |

### 3.1 Redundancy metric (definition)

A tool call is **redundant** in a turn if its fingerprint —
`(tool_name, json.dumps(json.loads(args), sort_keys=True))`, falling back to
raw args when not valid JSON — was already issued earlier *in the same
turn*. This intentionally counts:

- Re-reading a file the agent already read (the dominant waste observed).
- Re-running an identical shell command.
- Re-issuing the same broken call after a stall (which is *also* what
  `stall_events` captures — the two metrics are complementary: redundancy
  says *how much* was wasted, stall events say *that the agent looped*).

It does **not** count:

- Two calls to the same tool with different arguments (normal exploration).
- The *same* file read in *different* tasks (each task may legitimately
  need fresh state). Cross-task redundancy is a known future extension
  (see §7).

Read/write classification is by tool name: reads = `read_file`,
`get_weather`, `repo_overview`; everything else executed counts as a write
for this purpose. (The tool set is small and stable; the sets are constants
in `task_tracker.py`.)

### 3.2 Completion semantics

`status` is the mechanical fact of how the turn ended:

- `complete` — the loop exited on a final assistant reply (the normal path).
- `interrupted` — the user set the stop event (redirect / stop button /
  Ctrl+C). A premature stoppage.
- `failed` — an unhandled exception escaped the loop.
- `in_progress` — the process died mid-turn (orphan record). Orphans are
  swept to `in_progress` on the next `init_db()` if they are older than 15
  minutes; the 15-minute bound protects a long-running background turn from
  being mislabelled.

`completion` is the *outcome* verdict. In v1 it is auto-derived:
`status == complete` maps to `complete`, `interrupted` maps to `partial`,
`failed` maps to `not_completed`, `in_progress` maps to `unknown`. The
field is separated from `status` because the eventual goal is human/LLM
annotation ("the task was actually done even though the turn was
interrupted") and the two concerns must not be conflated.

## 4. Architecture

```
user turn
   |
   v
_process_conversation_turn (ConversationMixin)
   |-- tracker = start_task(self, user_text)        [hook 1: loop start]
   |-- _run_conversation_loop(client)
   |      |-- every LLM call ........ record_llm_call()   [hook 2]
   |      |-- every tool execution .. record_tool_call()  [hook 3]
   |      |-- stall/trunc/stream ... record_event()      [hook 4]
   |-- finish_task(tracker, final_response)          [hook 5: loop end]
   |      |-- analytic pass over chat_log rows in the turn's id window
   |      |-- INSERT task_log row (one UPDATE if the process was killed)
```

- `nbchat/core/task_tracker.py` — the record lifecycle and the analytic
  pass. Pure core; no UI imports.
- `nbchat/core/db.py` — `task_log` table + insert/update/query/sweep.
- `nbchat/ui/conversation.py` — the five hooks (the only file that knows a
  "turn" is happening).
- `nbchat/tui/app.py` — `/stats` slash command for in-TUI inspection.
- Orphan sweep — runs inside `db.init_db()` (once per process start), so
  any entry point (TUI, Jupyter UI, tests) gets it for free.

### 4.1 Invariants

1. **One row per task.** The insert happens exactly once, in
   `finish_task`, on both the normal exit and the exception exit
   (try/finally). `turn_ids` in the row make re-analysis possible.
2. **Hooks are best-effort.** Every hook site is wrapped so a failure in
   telemetry cannot break the conversation loop (same philosophy as the
   existing monitoring hooks).
3. **Telemetry never blocks the turn.** One indexed SELECT + one INSERT per
   turn; no per-token or per-chunk writes.
4. **The loop is the single source of truth for machine-derived counts.**
   The analytic pass only re-reads persisted rows to compute what the hooks
   cannot see (redirects, final reply length, redundancy windows).

## 5. Storage

`task_log` (chat_history.db, WAL, same file as the rest — no second
database to drift out of sync):

```sql
CREATE TABLE IF NOT EXISTS task_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    request_text  TEXT DEFAULT '',
    request_chars INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'in_progress',
    completion    TEXT DEFAULT 'unknown',
    nature        TEXT DEFAULT 'unknown',
    difficulty    TEXT DEFAULT 'unknown',
    started_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at      TIMESTAMP,
    duration_s    REAL,
    num_llm_calls INTEGER DEFAULT 0,
    num_tool_turns INTEGER DEFAULT 0,
    tool_calls_total INTEGER DEFAULT 0,
    tool_calls_by_name TEXT DEFAULT '{}',
    tool_calls_failed INTEGER DEFAULT 0,
    redundant_tool_calls INTEGER DEFAULT 0,
    redundant_reads INTEGER DEFAULT 0,
    redundant_writes INTEGER DEFAULT 0,
    stall_events INTEGER DEFAULT 0,
    truncation_events INTEGER DEFAULT 0,
    stream_retries INTEGER DEFAULT 0,
    text_toolcall_recovery INTEGER DEFAULT 0,
    user_interventions INTEGER DEFAULT 0,
    llm_latency_s REAL DEFAULT 0,
    prompt_chars INTEGER DEFAULT 0,
    completion_chars INTEGER DEFAULT 0,
    max_context_chars INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    final_response_chars INTEGER DEFAULT 0,
    turn_ids TEXT DEFAULT '[]',
    annotations TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_task_session ON task_log(session_id);
CREATE INDEX IF NOT EXISTS idx_task_status ON task_log(status);
```

`db.record_task(**fields)` upserts by primary key (the insert happens with
an explicit id only on the orphan-recovery path; the normal path uses
AUTOINCREMENT and the returned id is stored on the record).

## 6. Analysis surface

- `db.query_tasks(session_id=..., status=..., limit=...)` → rows as dicts.
- `task_tracker.summarize_tasks(rows)` → aggregate dict: counts by
  status/completion, mean/median duration, mean tool calls, redundant-call
  ratio, stall/truncation totals, failure rate.
- TUI: `/stats` (overall) and `/stats <n>` (last n tasks of the current
  session) print the summary.
- Raw SQL is always available against chat_history.db for ad-hoc analysis
  (e.g. completion rate bucketed by `tool_calls_total`).

## 7. Known limitations and follow-ups

- **Redirects truncate tasks.** A user interrupt closes the in-flight task
  as `interrupted` and the redirect opens a new one. The interrupted
  record keeps its own counts; cross-task stitching is a follow-up
  (joinable on `session_id` + `ended_at`/`started_at`).
- **Nature/difficulty are `unknown` in v1.** Automatic classification
  (heuristic keyword bucketing, or a cheap LLM rubric at close time) and
  manual override through `annotations` are planned; the columns exist now
  so no schema migration is needed later.
- **Completion is auto-derived in v1.** Human/LLM annotation via
  `annotations` is the override path; the `/annotate` tool is a follow-up.
- **Cross-task redundancy** (same file re-read across two tasks in a
  session) is not counted; the in-turn fingerprint window is deliberately
  per-task.
- **`prompt_chars`/`completion_chars` are character counts**, not tokens —
  the loop does not have token counts for requests; good enough for
  relative comparison, flagged as approximate.
- **ChatUI (Jupyter) and WhatsApp hosts** share the mixin, so they get the
  rows automatically once their loop path is exercised; only the TUI has
  the `/stats` viewer in v1.

## 8. Testing

`tests/test_task_tracker.py` exercises the lifecycle end-to-end against a
temporary `db.DB_PATH`: a scripted client (same pattern as
`test_tui.py::_StreamingClient`) drives `_run_conversation_loop` through
(a) a plain completion, (b) a tool turn with a duplicated read + a failed
call, and (c) a stop-event interruption — asserting the persisted row's
status, counts, redundancy and intervention fields. Direct unit tests cover
the fingerprint helper, the orphan sweep, and `summarize_tasks`.
