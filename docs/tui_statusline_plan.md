# TUI Status Line & Agent Activity — Plan

Goal (from user):
1. Visualise how many agents are **actively processing tokens** vs sitting idle.
2. A **persistent status line** (claude-code style), e.g.:
   `model · mode · context ▓▓▓░ 42% · 12.3k/32k tok · cache 87% · 14.2 tok/s · turn 3 (tool: pytest 2.1s)`
   with states: `idle`, `thinking`, `tool: <name> <elapsed>`,
   `waiting (retry 2/3 in 5s)`, `compacting…`, `error: …`.

## 1. Current architecture (facts)

- The TUI is a plain stdin/stdout REPL: `nbchat/tui/app.py::run()` loops on
  `read_line(prompt)`; each user turn runs on a **daemon thread**
  (`agent.send_async(text)` → `TerminalAgent.send` →
  `ConversationMixin._process_conversation_turn` →
  `_run_conversation_loop`). The main thread *always* keeps reading input so
  the user can interject mid-stream.
- There is no terminal UI framework (no curses/rich/pty). All rendering is
  raw `print()`/ANSI from the turn thread.
- Background actors that consume model slots or run turns:
  - **Assistant** (this session's `TerminalAgent`), its turn thread.
  - **Supervisor** watchdog (`nbchat/core/supervisor.py`): periodic LLM review
    on a second parallel slot; has `running`, `_interval`, `_cooldown`,
    `_last_voice_status`.
  - **Email bridge** (`nbchat/tui/email_bridge.py`): polls inbox, may fire
    auto-replies through the assistant.
  - **Voice bridge** (`nbchat/voice/`): inbound transcripts become turns via
    `send_async` (shares the assistant's send lock).
  - **Team runs** (`nbchat/core/team.py`): `TeamCoordinator` +
    `_WorkerPool` with up to `team_max_workers` claimer threads, a shared
    `TaskQueue` (statuses: `pending/claimed/done/failed`), and a reaper.
    `_WorkerPool._inflight` already tracks in-flight worker count under a
    condition variable — the exact "busy agents" number we need.
- Token/context facts already available:
  - `config.MODEL_NAME`, `config.CTX_SIZE`, `config.N_PARALLEL`,
    `config.CONTEXT_BUDGET`, `config.CONTEXT_HEADROOM`.
  - Per-turn usage: `_InstrumentedStream` (`nbchat/core/client.py`) logs
    TTFT and `P/C/T` token counts to `inference_metrics.log`;
    `app.py` already computes a `speed  avg … tok/s` line from that log for
    the banner (`/stats`).
  - History rows carry a token count (6th tuple element); window building
    happens in `ConversationMixin._window()` / `chat_builder`.

## 2. Design

### 2.1 Mechanism (no curses, no pty)

A **bottom status line** drawn by a dedicated **status ticker thread**:

- The ticker renders the status string into a fixed "line at the bottom of
  the visible transcript" using ANSI escapes:
  - first paint: `\n` + line + store height in scrollback;
  - repaint: cursor-down N lines (N = number of lines the status previously
    occupied) + `\r` + clear-line + rewrite. Status is always **one
    terminal row** (truncated with `…`), so N is 1.
- Before every `read_line()` (i.e. before `input()`), the TUI **clears the
  status line** (cursor up 1, clear) so the prompt sits where the user
  expects and the prompt itself doubles as the "idle" indicator
  (`❯ model · idle`).
- Conflict rule: the status line is only rewritten by the ticker thread.
  Transcript `print()` output (assistant text, tool displays) happens above
  it; when the transcript grows, the terminal scrolls and the "previous
  status line" position shifts — so the ticker must **re-anchor**: after any
  transcript write it clears and repaints at the current bottom (the
  transcript writers call `status_bar.mark_dirty()` / bump a write counter;
  the ticker, on its next 200 ms tick, re-anchors with cursor-down-from-
  bottom instead of cursor-up-from-old-line). Concretely the ticker keeps:
  - `_last_transcript_marks`: count of prints since last paint;
  - if it changed, it does `\r\n` + clear + repaint instead of the
    cursor-up dance.
  This is the classic "no-UI-library status line" pattern (same approach as
  early Claude Code / aider).
- Safety: if the terminal is too narrow or non-tty (`--no-color`, piped),
  the ticker is disabled entirely (banner/stats unchanged).

### 2.2 State model

New module `nbchat/tui/status.py` (framework-free, unit-testable):

```
class AgentStatus:  # one per actor, registered by id
    id, label ("assistant", "supervisor", "worker-2"…)
    state: idle|thinking|tool|waiting|compacting|error|done
    detail: str            # e.g. tool name, error short text
    since: float           # monotonic time of state entry (for elapsed)
    tokens_seen: int       # tokens streamed while thinking (live tok/s)

class StatusBar:
    register/unregister agents
    set_state(agent_id, state, detail=...)
    # rendered line:
    #   <model> · <mode> · <ctxbar> 42% · 12.3k/32k tok · 14.2 tok/s · turn 3 (tool: pytest 2.1s) · agents 3/4
    #   when >1 agent:  agents 3 busy · 1 idle   [w2 tool:pytest 2.1s | w3 thinking 812 tok]
```

Rendering rules (fixed order, `·` separators, hard-truncated to term width):
1. `MODEL` (short: last path component of `MODEL_NAME`)
2. `mode` = current top-level state of the *assistant*: idle / thinking /
   tool / waiting / compacting / error
3. `context ▓▓▓░░░░░░░ 42%` — bar of 10 cells, `used/CtxBudget`
4. `12.3k/32k tok` — same numbers, humanised
5. `cache 87%` — only if we start tracking prompt-cache hits (Phase 3,
   optional; server must expose it — llama-server returns `prompt_eval`
   timing, not cache hits. **Mark as nice-to-have; skip if not available**)
6. `14.2 tok/s` — *live* rate: tokens streamed in the last 1 s window
   (rolling), falling back to the `inference_metrics.log` average for
   `/stats` (existing banner code reused).
7. `turn N (tool: pytest 2.1s)` — current loop iteration + current/last tool
8. `agents X/Y` — busy/total across assistant + supervisor + team workers
   (+ per-agent chips when >1 busy: `supv 3.2s`, `w2 tool:pytest 1.1s`)

State → colour (Palette): idle=dim, thinking=cyan, tool=green,
waiting/yellow, compacting=magenta, error=red.

### 2.3 Where states are set (hooks)

All state changes go through `StatusBar` (thread-safe; the bar owns its own
lock; the ticker reads a snapshot).

**Assistant** (`ConversationMixin._run_conversation_loop` /
`_stream_response` / tool-execution block):
- turn thread start → `thinking` (detail: "user turn")
- first streamed token → stays `thinking`, bump `tokens_seen`
- tool call dispatched → `tool` + tool name + `since=now`
- tool returns → back to `thinking` for next LLM call
- `MidStreamError` caught with retry remaining → `waiting (retry k/3)`
- `self._stop_event` interrupt → `waiting (stopping…)` until thread exits
- context compression (`_window()` cut / compressor path) → `compacting…`
   (set inside the compression branch of the loop; short-lived)
- exception in `_process_conversation_turn` → `error: <Type>`
- turn thread exit (finally) → `idle`
- `drain_interjections` injection → brief `tool: supervisor` chip.

**Supervisor** (`nbchat/core/supervisor.py`): register on start; its review
call sets `thinking` for the duration, back to `idle` (it already times its
own loop; wrap the LLM call). Cooldown wait → `idle` (it's asleep, not busy).

**Team workers** (`nbchat/core/team.py`):
- `TeamCoordinator` registers/unregisters a `StatusBar` per worker
  (`worker-N`, task title) in `_make_worker`/`_execute_task` finally.
- Each worker's `TerminalAgent` hooks already exist
  (`_on_stream_token`, `_on_tool_display` — see `_wrap_worker_hooks`);
  route them through `ctx.agent`'s bar registration instead of / in
  addition to the tagged print.
- Pool-level busy count = `_WorkerPool._inflight` (already maintained);
  the bar exposes `worker_busy` = max across pools, or counts registered
  agents whose state != idle (single source of truth = registry).

**Email/Voice**: they drive *assistant* turns; mark the assistant state as
`thinking (email)` / `thinking (voice)` via the existing
`send_async(text, source=...)` — add an optional `source` kwarg.

### 2.4 Ticker thread & main-loop integration (`nbchat/tui/app.py`)

- `StatusBar.attach_terminal()` spawns the ticker (200 ms cadence).
- `read_line()` → clears the status row before `input()` (when attached).
- Slash commands: `/status` (verbose multi-line agent table: state, elapsed,
  tokens, last tool, per-worker task title), `/statusbar on|off|quiet`.
- On `Bye.`: detach + restore.

## 3. Phases

| Phase | Deliverable | Effort |
|---|---|---|
| **P1** | `status.py`: `AgentStatus`/`StatusBar` + pure `render_line(snapshot, term_width)` (testable, no I/O). `/status` command in banner loop printing the agent table. State hooks for **assistant only** (thinking/tool/waiting/error/turn N). | 1 session |
| **P2** | Ticker thread + ANSI bottom line + `read_line` clear + re-anchoring after transcript prints. Context bar (from window token count / `CONTEXT_BUDGET`). Live tok/s from streamed tokens. | 1 session |
| **P3** | Multi-agent registration: supervisor, team workers (per-worker chips, `agents X/Y`), email/voice source tagging. Reuse `_inflight` for pool count sanity (log warn if disagrees with registry). | 1 session |
| **P4 (optional)** | Cache-hit % (only if server exposes it — verify llama-server
  response fields first), sparkline of tok/s, `/statusbar quiet` mode. | — |

### Progress

| Item | Status |
|---|---|
| **P1** `status.py`: `AgentStatus`/`StatusBar` + pure `render_line(snapshot, term_width)` | [x] done — `nbchat/tui/status.py` |
| **P1** `/status` command in banner loop printing the agent table | [x] done — `nbchat/tui/app.py` `_print_status()` |
| **P1** Assistant state hooks (thinking/tool/waiting/error/turn N) | [x] done — `ConversationMixin._status_*` hooks + `TerminalAgent` turn-thread finally (thinking/tool/error/done/stalled-interrupted) |
| **P1** Context estimator (`_est_window_tokens` / `set_context`) | [x] done — `nbchat/ui/context_manager.py` + `nbchat/tui/status.py` |
| **P1** Tests (`tests/test_status.py`) | [x] done — render_line widths/states, StatusBar thread-safety, `/status` output |
| **P2** Ticker thread + ANSI bottom line + `read_line` clear | [ ] |
| **P2** Context bar wired live + tok/s | [ ] |
| **P3** Supervisor + team worker registration | [ ] |
| **P4** Cache % / sparkline / quiet | [ ] (optional) |

**P1 complete** — 327/327 tests passing (12 in `tests/test_status.py`:
render_line widths/truncation, state handling, StatusBar thread-safety,
`/status` table output), committed in this repository (single P1 commit).

### Explicit non-goals
- No curses/rich/alternate-screen; transcript stays in scrollback.
- No change to agent behaviour/locks — read-only observers + a lock in
  `StatusBar` only.
- No persistence of status history (telemetry already exists via
  `task_tracker` / `inference_metrics.log`).

## 4. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Race: transcript print lands between ticker's cursor moves → garbled line | Single-writer rule: only the ticker touches the status row; transcript writers bump a counter, ticker re-anchors. `input()` prompt clears the row first. |
| Non-tty / piped output (`\| tee`) | Disable ticker when `not sys.stdout.isatty()` or `--no-color`. |
| State leak (agent dies → stuck "thinking") | `finally` blocks in every registered owner; ticker also reaps: state unchanged for >10 min with dead owner thread → `idle (stale)`. |
| Team workers: hooks already used for tagged prints | Bar registration is additive (also-called hook), prints unchanged. |
| tok/s when server streams no `usage` | Live rate = token count / wall time of the *current* stream segment (we count deltas in `_on_stream_token`); average from log only for `/stats`. |
| Narrow terminal | Fixed priority truncation (drop from right: tok/s → tok → bar → model full name); `render_line` takes `term_width`. |

## 5. Test plan (`tests/test_status.py`)

- `render_line`: each state, truncation at widths 40/80/200, multi-agent
  chips, bar cell count, humanisation (12.3k).
- `StatusBar` thread-safety: 4 threads setting state, no exceptions,
  snapshot consistent (state ∈ enum, timestamps monotonic).
- Assistant hooks: run `_process_conversation_turn` with stub client
  (pattern exists in `tests/test_tui.py` from line ~349) asserting the state
  sequence `thinking → tool → thinking → idle`, and `error` on raise.
- Ticker ANSI: capture to a `StringIO`-terminal stub: initial paint =
  `\n<line>`, repaint = cursor-down 1 + clear; after a transcript-mark,
  repaint = `\n<line>`.
- `/status` output contains per-agent rows.

## 6. Open questions (answer before P3)

1. Should the status line also render when the *terminal is at a prompt*
   (persistent like claude-code, prompt line above it) vs only mid-turn?
   → Plan: **only mid-turn** (ticker pauses while `input()` is active);
   the prompt itself shows `❯ model · idle`. Simpler, no prompt redraw.
2. Team runs from *other* sessions (team session id `team:<run_id>`) — bar
   shows only agents spawned in this process (yes — in-process registry).
3. `cache 87%`: confirm whether the local llama-server build exposes
   prompt-cache metrics; if not, drop from the default format.
