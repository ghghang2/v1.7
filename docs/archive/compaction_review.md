# Compaction Review — nbchat

Scope: the full context-compaction stack — token-budget windowing (`ContextMixin._window`),
L1 core memory, L2 episodic memory, structured prior-turn summaries, hard trim
(`_hard_trim`), tool-output compression (`compressor.py`), and the persistence layer
they depend on (`db.py`).

Criteria used to rank work: performance, resilience to edge cases and failure modes,
and maintenance simplicity. Items that would add complexity for marginal benefit are
explicitly listed under "Not recommended".

Status legend: `[ ]` open — `[x]` complete.

---

## Ordered by criticality

### 1. [x] WhatsApp `handle()` runs concurrently on shared agent state (data race / crash)
`nbchat/channels/whatsapp_server.py:39` — the `/message` endpoint is a **synchronous**
FastAPI `def`, so uvicorn executes it on a threadpool; two WhatsApp messages in flight
call `WhatsAppAgent.handle()` concurrently on the **same agent instance**.
`WhatsAppAgent` has no send lock (TUI's `TerminalAgent` has `_send_lock`).

Failure modes: two loops mutate `self.history` / `self.session_id` interleaved →
crossed histories, `db.log_message` to the wrong session, `build_messages` fed an
inconsistent sequence (orphan `tool` rows with no preceding `tool_calls` → API 400),
and a `KeyError` inside `_stream_response` when another thread swaps the session.

**Fix:** give `WhatsAppAgent` a `_send_lock` (threading.Lock) and run the
`_switch_session` → append user row → `_process_conversation_turn` sequence under it —
identical semantics to `TerminalAgent.send()`. Messages for the same sender serialize;
different senders also serialize on one shared agent (correct for a single-worker
deployment; the class docstring already states the single-worker assumption).

### 2. [x] SQLite connections are opened and closed on every DB call
`nbchat/core/db.py` — every function does `sqlite3.connect(DB_PATH)`. A turn with many
tool calls issues dozens of connects; each connect re-parses schema metadata and (since
the DB is not in WAL mode) takes an exclusive lock against readers. This is pure overhead
on the hottest path of the system.

**Fix:** one module-level lazy connection + `check_same_thread=False` + a
`threading.Lock` guarding writes was applied and **reverted** because it broke
`nbchat.tui` (a `threading.Lock` re-acquired from `init_db` while held deadlocks
every subsequent DB call). **Final decision (see `issues.md`):** keep per-call
`sqlite3.connect()`, but route every call through `db._connect()`, which sets
`PRAGMA busy_timeout=2000` so a contended database raises a catchable
`OperationalError` after ~2 s instead of blocking a tool thread indefinitely;
and enable `PRAGMA journal_mode=WAL` (persistent, set once in `init_db()`) so
readers never block the writer. `supervisor._task_stats()` was the last raw
`sqlite3.connect()` and now goes through `db._connect()` too. All existing
function signatures are unchanged.

### 3. [x] Stale config keys in `repo_config.yaml` change behavior silently
`window_turns: 8`, `max_window_rows: 30`, `max_exchanges: 50` are read by nothing
(config's `__all__` comments confirm they were removed), yet their section header still
says "Context management constants". `keep_recent_exchanges: 30` **is** read — and the
code default is `5` (`getattr(config, "KEEP_RECENT_EXCHANGES", 5)`) — so anyone
re-deriving behavior from the code alone gets a 6x different protection window.

**Fix:** delete the three dead keys with a comment; document the `keep_recent_exchanges`
override explicitly.

### 4. [x] `chat_builder.build_messages` can emit an API-invalid sequence for legacy rows
If history starts with a `tool` row (no preceding `assistant_full` with matching
`tool_calls`), or a legacy `assistant` row carries a `tool_id` while its tool result was
evicted, `build_messages` produces `{"role": "tool", ...}` with no matching `tool_call_id`
→ OpenAI-compatible API 400 → the whole turn dies. Current data is clean (0 orphans), so
this is a latent failure mode, not an active bug — but session switching + the
`LIMIT`-capped `load_history` path makes it reachable.

**Fix (minimal):** in `build_messages`, skip `tool` rows whose `tool_id` was not seen in
the current batch, and drop the synthetic `tool_calls` from a legacy `assistant` row when
its tool result is absent. One guard function, no new abstractions.

### 5. [x] `chat_history.db` grows without bound (analysis + context_events tables)
The live DB already shows the shape of the problem: `analysis` (reasoning traces)
rows total ~625 KB of text, `context_events` accumulates one row per compaction
decision. `analysis` rows are display-only (chat_builder drops them; `_window` skips
them in the token walkback) — yet they reloaded and re-rendered on every session load,
and `_render_history` re-parses every `assistant_full` JSON on every refresh.

**Fix (bounded, low complexity):** keep writing them (useful for debugging) but
- cap the `context_events` table: on insert, `DELETE` rows older than a configurable
  retention window (default: keep last 5000 rows per session);
- `load_history(limit=...)` is already available — use a row cap when the TUI/ChatUI
  loads a session (e.g. last 2000 rows; older turns are summarized by prior-context
  anyway, which is exactly what compaction is for).

### 6. [x] `compressor._LOSSLESS_WINDOW` ignores the config setting
`config.LOSSLESS_WINDOW` exists (yaml `lossless_window: 10`) but the compressor uses a
module constant `_LOSSLESS_WINDOW = 10`. The yaml key is dead — tuning it does nothing.

**Fix:** import `LOSSLESS_WINDOW` from config in the compressor (it already imports
`MAX_TOOL_OUTPUT_CHARS` the same way). One line.

### 7. [x] Truncation nudge / mid-stream recovery paths skip persistence + loop invariants
In `_run_conversation_loop` (conversation.py), the two continuation paths (truncation
nudge and `MidStreamError` recovery) append assistant/user rows and push to
`messages`, bypassing the normal post-turn handling:
- `monitor.flush_session_monitor` / `_refresh_monitoring_panel` are not called there
  (fine — turn continues), but
- the L1/L2 post-exchange update and the L2-write for the partial exchange never run,
  so an evicted partial exchange is lost from episodic memory, and
- on a *final* truncation (turn == MAX_TOOL_TURNS) with no content, the turn ends with
  the nudge as the last message — the model will "continue" into the next user message.

**Fix (minimal):** after each nudge injection, run the same L1/L2 update with the
partial content (wrapped in try/except like the tool path); do not add a new state
machine.

### 8. [x] `_hard_trim` Pass-2 can truncate an already-truncated tool result into
invalid JSON mid-object and the "largest" pick is O(n·m)
Pass 2 truncates the largest tool message to 200 chars. That is fine as a last resort,
but `est()`/`total()` recompute the full sum every iteration — the while loop can run
O(exchanges) times over O(messages) each → quadratic in long sessions. With
`max_tool_turns: 200` this is measurable.

**Fix:** maintain a running `total_tokens` updated on each mutation (incremental
subtraction/addition). No API change.

### 9. [x] `_window()` token walkback miscounts `assistant_full` rows
`_est_tokens` counts `content + tool_args`, but for `assistant_full` rows the
`tool_args` column holds the **full JSON message** (including the tool-call arguments
and, after fix #1-era changes, the assistant text duplicated). The window walkback
therefore overestimates assistant_full tokens by ~2x, shrinking the window unnecessarily
→ premature eviction of exchanges that the L2 store would have kept for free.

**Fix:** in `_est_tokens`, for `assistant_full` rows count the parsed JSON's content +
tool-call arguments once (fall back to current behavior on parse failure).

### 10. [x] Summarizer thread pool + futures survive session switches
`_summary_futures` is per-agent; on `_switch_session` the dict is replaced, but in-flight
`_call_summarizer` futures keep running against the *old* session's rows and call
`self.model_name` / `self.system_prompt` — both of which may now belong to the new
session. Harmless output (summaries are cached by content-hash, session-agnostic) but
wastes LLM calls for a session nobody is viewing, and in a WhatsApp multi-sender
deployment each switch abandons up to `max_workers=2` pending calls.

**Fix:** on `_switch_session` / `new_session`, cancel pending futures
(`future.cancel()` is a no-op on running ones — acceptable, they finish but their
result is discarded because the cache was replaced). Three lines per call site.

### 11. [x] `MAX_STREAM_RETRIES` loop can re-inject the same nudge rows on repeated
failure
The retry loop appends `assistant` + nudge rows to `self.history` and the DB **inside**
the `for _attempt` loop. If the stream drops again on attempt 2 with new partial
content, a second pair is appended — fine. But if attempt 2 fails *before any content*
(`exc.content` empty, no tool calls), the loop re-raises; the already-appended rows from
attempt 1 remain. That is correct today, but the partial content from attempt 1 is
already rendered on screen AND persisted as a finished assistant message — the next
`_window()` will see a "complete" assistant row that is in fact cut off. Acceptable
behavior, but worth a docstring so nobody "simplifies" the append placement.

**Fix (doc only):** add the invariant comment; no code change.

### 12. [x] `db.is_error_content` keyword list flags benign tool output as error
`is_error_content` matches `"not found"` and `"invalid"` anywhere in content. A `grep`
for "not found" in source code, or a successful test run printing "0 invalid", sets
`error_flag=1` on the *structured-outcome* tools too? No — structured tools skip it
(good), but non-structured tools (browser, get_weather JSON) still use keyword match on
the full text, and the flag feeds L2 importance scoring (+3) and the L1 error history.
Result: episodic memory over-weights benign results and L1 `error_history` fills with
false entries.

**Fix (bounded):** for tools returning JSON, only flag on parseable `status`/`error`
keys; keep keyword match as the non-JSON fallback. Reuse the existing
`_structured_error` parsing path.

### 13. [x] `compaction.log` FileHandler is added unconditionally at import
`compressor.py` attaches a DEBUG FileHandler to `nbchat.compaction` at import time,
writing to `compaction.log` (CWD-relative). In a server deployment CWD can be `/`, and
the logger is shared with the context_manager module — so context events land in the
file forever. Minor operational hazard, easy to trip.

**Fix:** make the handler attach lazily and only when `COMPACT_LOG_FILE` is set in the
yaml (default: off), or drop the module-level handler entirely and rely on the
existing `context_events` DB table (which already captures the same events).

### 14. [x] `_importance_score` keyword match is applied to *compressed* results in
the hard-trim path but *raw* results in the tool path
`_hard_trim.drop_least_important` scores `messages[s:e]` (compressed content), while
the per-tool call passes `raw_result`. A compression that drops the word "error"
changes the score → the L2 write decision differs depending on *when* the exchange is
scored (during the turn vs. during trim). The threshold is percentile-based, so one
outlier score rarely flips a decision — but it is an inconsistency that will bite when
persistence_fraction is tuned.

**Fix (minimal, no new state):** in `_hard_trim`, when scoring an exchange, append the
first tool message's content to the scoring input via the existing `raw_result`
parameter — i.e. pass the tool content explicitly. Keeps a single code path.

---

## Not recommended (evaluated, rejected)

- **Re-architecting the sliding window as a persistent checkpoint store** (the
  monitoring hint about "checkpoints erased" refers to llama-server's internal SWA
  cache, not to nbchat state). Adding a checkpoint layer would duplicate what the
  prior-context summary + L2 store already do, for no measurable win.
- **Per-token streaming window updates** — window is computed once per user turn,
  which is correct; re-windowing per tool turn would thrash the KV cache prefix.
- **Replacing the heuristic truncation guard with a parser** — the guard's false
  positives are bounded (one extra nudge round-trip) and a sentence-completeness
  parser is a rabbit hole with no resilience payoff.
- **Splitting `ConversationMixin` / `ContextMixin` further** — they are already the
  right granularity; further splitting would fragment the compaction pipeline across
  modules and hurt maintainability.
- **Vector-embedding L2 retrieval** — entity-keyed + top-importance retrieval is
  simple, deterministic, and the `l2_retrieval_limit: 5` budget keeps cost bounded.
  Embeddings add a model dependency and a failure mode for a marginal recall gain.

## Issues that block nothing (recorded for completeness)

None found that block the compaction task. See `issues.md` if separate concerns
emerge during implementation.
