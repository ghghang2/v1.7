# nbchat Code Review — Progress & Findings

**Objective:** Identify major core bugs and issues in nbchat. Goal: the most performant,
light-weight LLM inference harness possible. Fix broken code and document here.

**Status: COMPLETE** — all identified fixes applied, verified, committed, pushed.

## Plan (done)
1. [x] Review core inference path: `run.py`, `nbchat/core/` (client, compressor, db, monitoring, remote, retry, utils, config)
2. [x] Review UI / agent loop: `nbchat/ui/` (chatui, conversation, context_manager, chat_builder, tool_executor)
3. [x] Review tools: `nbchat/tools/`
4. [x] Review channels: `nbchat/channels/`
5. [x] Fix bugs found; note each fix below.
6. [x] Run test suite (0 tests exist → clean pass) + commit + push.

---

## Critical bugs fixed

### C1. Streaming loop crashed on the final "usage" chunk — every turn
**File:** `nbchat/ui/conversation.py` — `_stream_response`

**Problem.** `MetricsLoggingClient.create()` (core/client.py) forces
`stream_options.include_usage = True` on every streaming request. The OpenAI
SDK then appends a **final chunk with an empty `choices` list** (carrying only
`usage`). The loop did `choice = chunk.choices[0]` unconditionally →
`IndexError` on that final chunk, which was raised out of
`_process_conversation_turn` and killed the **entire agentic loop at the end
of every single LLM turn** (content was already appended to the UI, but the
loop stopped instead of continuing to tool calls / next turn).

**Fix.** Skip chunks with no `choices`:
```python
if not chunk.choices:
    continue
choice = chunk.choices[0]
```
Verified: old loop raises `IndexError` on the usage chunk; new loop returns
content + `finish_reason` correctly.

---

### C2. Tool executor: wrong timeouts + blanket retry of deterministic errors
**File:** `nbchat/ui/tool_executor.py` (+ `nbchat/core/retry.py`)

**Problems.**
1. **Hard-coded timeouts made core tools unusable:** `browser` and `run_tests`
   got 10 s, everything else 5 s. The browser tool's own default navigation
   timeout is 30 s and a real pytest run exceeds 5 s, so those tools timed
   out on virtually every real invocation — then got **retried 3× with
   backoff**, multiplying the wasted wall-clock time.
2. **All tool exceptions were re-wrapped** in `Exception("Tool execution
   error: <e>")`, stripping the original message. Combined with the retry
   policy's "default = retry" fallback, *deterministic* failures (non-zero
   exit code, unknown selector, git push rejection) were retried up to 4
   times with exponential backoff (1 s → 2 s → 4 s + jitter) — pure dead
   time in an inference harness whose whole point is low latency.

**Fix.**
- Timeouts now come from `repo_config.yaml`
  (`browser_timeout=60`, `tests_timeout=60`, `other_tools_timeout=30`),
  which matches the tools' own internal budgets.
- Tool exceptions are **no longer re-wrapped** — the original message and
  type reach the retry classifier.
- `retry.py`: unknown/unrecognised errors are now **non-retryable by default**
  (only explicitly transient messages — timeout/timed out, connection,
  network, 5xx — are retried). A `NonRetryableError` sentinel allows any
  caller to short-circuit retry.
- Retry delays/backoff now read from config instead of hard-coded values.

Verified: `exit 3` returns immediately (0 retries); deterministic error
raises after exactly 1 attempt; a flaky timeout is retried and succeeds.

---

### C3. WhatsApp channel crashed on import (and would at runtime)
**File:** `nbchat/channels/whatsapp_agent.py`

**Problems.**
1. `WINDOW_TURNS = config.WINDOW_TURNS` at class body → `AttributeError` at
   import time (the constant was removed from `config.py` / `repo_config.yaml`
   but this reference was left behind). `import nbchat.channels.whatsapp_agent`
   raised, which also breaks `whatsapp_server.py` at startup.
2. The class inherits `ContextMixin` + `ConversationMixin` but `__init__`
   never created `_importance_tracker`, `_summary_futures`, `_stop_event`, or
   `_history_lock` — all required by the mixins (`_window()`,
   `_hard_trim()`, `_run_conversation_loop()`). Any WhatsApp message would
   have crashed with `AttributeError`.

**Fix.** Removed the dead `WINDOW_TURNS` reference; added the missing
`_importance_tracker` (with `PERSIST_FRACTION` from config),
`_summary_futures`, `_stop_event` and `_history_lock` in `__init__`
(mirroring ChatUI's thread primitives). Module now imports and the agent is
instantiable.

---

## Secondary fixes / hardening

### M1. `.gitignore` was missing — repo bloat risk
`push_to_github` uses `commit_all()` → `git add -A`. With no `.gitignore`,
every push swept in the **80 MB `llama-server` binary**, `llama_server.log`,
`chat_history.db`, `__pycache__/`, `.ipynb_checkpoints/` and
`Untitled.ipynb` (pack was already 57 MB, mostly node_modules from a previous
sweep). Added a `.gitignore` covering all of these so future pushes stay
light (directly serves the "light-weight harness" goal).

### M2. Tool-timeout config keys now actually used
`repo_config.yaml` already declared `browser_timeout` / `tests_timeout` /
`other_tools_timeout` but nothing consumed them. C2 wires them in — the
config is now the single source of truth for tool wall-clock budgets.

---

## Reviewed, no action needed (noted for future perf work)

- **`nbchat/core/db.py`** — `get_session_ids` (`ORDER BY ts DESC` on
  `DISTINCT session_id`) is valid SQLite; verified empirically. Fine.
- **`nbchat/core/monitoring.py`** — `record_tool_call()` increments
  `total_output_chars` only when `input_chars` is truthy, so an
  empty-output call loses its output bytes in the ratio. Cosmetic
  (monitoring only, not on the inference path).
- **`nbchat/core/compressor.py`** — `LOSSLESS_WINDOW` is read from config but
  the module hard-codes `_LOSSLESS_WINDOW = 10`. Currently equal; the config
  knob is inert. Consider deleting one of the two.
- **`nbchat/core/monitoring.py`** — module-level thresholds are hard-coded
  and duplicate `repo_config.yaml` values (config loads them but monitoring
  never uses them). Same duplication pattern.
- **`nbchat/tools/browser.py`** — spins up a full Chromium + playwright
  context per call (~1–2 s overhead). Fine for statelessness, but a shared
  browser process would be a meaningful win for agentic multi-step browsing.
- **`nbchat/ui/context_manager.py`** — `_hard_trim` uses
  `getattr(config, "KEEP_RECENT_EXCHANGES", 5)` — the config key is absent,
  so it silently defaults to 5. Works, but the YAML has a
  `keep_recent_exchanges: 30` that is never read (config.py doesn't load
  it). Reconcile config.py `__all__` with the YAML or drop the dead keys.
- **KV-cache design** (`chat_builder.py`) — system prompt pinned at
  `messages[0]`, volatile context in `messages[1-2]`. Correct and intentional;
  the monitoring module exists precisely to measure its stability.
- **`run.py`** — `os._exit(0)` after successful start is deliberate (detach
  the launcher so the notebook/terminal doesn't hold the child processes).

## Files changed
- `nbchat/ui/conversation.py` — C1 (empty-choices guard in stream loop)
- `nbchat/ui/tool_executor.py` — C2 (config timeouts, no exception re-wrap)
- `nbchat/core/retry.py` — C2 (non-retryable default, `NonRetryableError`, "timed out" pattern)
- `nbchat/channels/whatsapp_agent.py` — C3 (import crash + missing mixin state)
- `.gitignore` — M1 (new; prevents binary/log/DB/pycache bloat on push)

## Test suite
`run_tests` → `no tests ran in 0.01s` (repo has no pytest suite; 0 failed).
Ad-hoc verification of all fixes performed above (stream chunk handling,
retry semantics, import of every touched module, py_compile of all changes).
