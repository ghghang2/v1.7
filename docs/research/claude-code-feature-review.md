# Claude Code → nbchat.tui: Feature Review (research/design phase)

Date: 2026-06-16
Scope: comprehensive review of Claude Code (https://github.com/anthropics/claude-code,
docs at https://code.claude.com/docs) to identify features worth porting to `nbchat.tui`.
No code changes in this phase.

## 1. Context / design constraints (from the team)

- **Moderately small** codebase — pick high-value, low-footprint interaction patterns,
  not infrastructure.
- **User-friendly & intuitive** — discoverable commands, visible state, forgiving input.
- **Highly performant & resilient** — minimize failure modes, guard against external
  failures (the local `llama-server` is the key external dependency here).
- **Maximize the communication channel** — a system that *reports* status and
  *statistics* is preferred over one that is silent.

## 2. What Claude Code actually is (baseline)

A large TypeScript agentic harness (install ≈ multi-MB binary) with: an agentic loop
(gather context → act → verify), a huge built-in tool set, ~100+ slash commands/skills,
MCP, plugins, hooks, subagents, checkpoints, permission modes, web/cloud/desktop/
remote-control surfaces, full-screen and classic terminal renderers.

Implication: we are **not** porting Claude Code — we are mining its *interaction
patterns*. The features that matter for nbchat are those that fit a lean, line-based,
local-LLM TUI and that serve the "never be silent" and "show me the numbers" values.

### What nbchat.tui already has (verified in code, 2026-06)

- Commands: `/help /new /sessions /load /title /history /effort /stats` (plus
  quit/exit). `handle_command()` in `nbchat/tui/app.py`.
- Strong stats backend already: `nbchat/core/monitoring.py` (SessionMonitor, cache
  metrics, tool redundancy, warnings, `format_report`, `suggest_config`) and
  `nbchat/core/task_tracker.py` (per-task records: latency, prompt chars, tool errors,
  user interventions, redundancy). `/stats` already surfaces task summaries + tok/s.
- `compressor.py` (context compaction) exists in core.
- Line-based input with `\`+Enter continuation; turn runs in a thread with Ctrl+C
  interrupt that requests a clean stop (`wait_for_turn`).
- `--check` flag that verifies llama-server reachability.
- Extensions already present: `--email` inbox bridging, `--supervisor`, `--voice`.

Gaps vs. Claude Code (the opportunities): no usage/cost visibility, no context-window
visualization, no auto-compact notification, no session recap, no side questions, no
command completion, no permission modes, no checkpoints/rewind, no diagnostic command
beyond `--check`, no structured failure/backoff messaging.

## 3. Claude Code feature inventory (relevant slices)

### 3.1 Communication / status (highest alignment with our values)

- **Session recap**: after ≥3 min idle and ≥3 turns, a one-line recap (≤400 chars) is
  generated *in the background* and shown when you return to the terminal. Never twice
  in a row. `/recap` on demand.
- **Task list / checklist**: the agent's to-do list (pending / in progress / done),
  toggle with Ctrl+T, persists across compaction, shared across sessions via a named
  dir. Separate from the background-task view (`/tasks`).
- **`/context [all]`**: visualizes context usage as a colored grid per category, with
  optimization suggestions and a capacity warning showing how far over the limit you
  are and which command frees space.
- **Auto-compact with visible policy**: `/autocompact auto|<tokens>` sets the window
  fullness before auto-compaction; `/compact [instructions]` frees space with optional
  focus guidance; summary keeps a "Summarized conversation" marker in place.
- **Usage-limit wait with visible countdown**: instead of failing, shows
  `Usage limit reached · continuing automatically at 3:45pm · esc to cancel`;
  auto-continue is **capped at 2 retries in a row**, after which it stops and offers
  `/rate-limit-options`. Sleep/resume is handled with an explicit
  `press enter to continue` state. Every state transition is announced.
- **Footer/status indicators**: model, permission mode indicator, prompt-bar color;
  PR/MR badge with colored review state (green/yellow/red/gray) refreshed on `git
  push` / `gh pr` success; graceful degradation to plain text over SSH when hyperlink
  support can't be detected.
- **Transcript viewer (Ctrl+O)**: collapsible detail — tool usage per message with
  timestamp + model; MCP calls collapse to one line (`Called slack 3 times`).
- **PR review status** and cross-session message previews: one-line "Message from
  @sender" previews instead of full dumps.

### 3.2 Reliability / failure-mode engineering

- **Bounded retries with visible policy**: spellcheck — if the external checker fails
  twice (at startup or mid-session), it restarts once and then *stops checking* until
  restart; three 15s timeouts also stop it. Debug log records *why* it stopped.
- **`/doctor`**: setup checkup that diagnoses and can fix installation/config issues.
- **`/debug`**: turn on debug logging mid-session with an optional issue description.
- **`/feedback`**: bug report with explicit consent screen controlling how much
  history is included; if credentials aren't available, degrades to writing a local
  bundle under `~/.claude/feedback-bundles/` for the user to forward manually
  (graceful degradation to offline behavior).
- **Checkpoints / `/rewind`**: file snapshots before each user prompt (100 most
  recent kept, 30-day retention); menu offers: restore code+conversation, restore
  conversation only, restore code only, summarize-from-here, summarize-up-to-here.
  Explicit limitations documented (bash-modified files, subagent edits, symlinks,
  "not a replacement for git") with recovery steps.
- **Interrupt discipline**: Esc stops a turn *keeping work done so far*; Ctrl+C
  first clears input, second exits; Ctrl+D requires a double-press within 800ms to
  exit; destructive ops require confirmation.
- **Session persistence**: JSONL transcripts written continuously; resume restores
  model, permission mode, active goal; failure to find a session produces a precise
  error (`No conversation found with session ID ...`).

### 3.3 Performance / context economy

- **Deferred loading**: MCP tool schemas deferred until needed (tool search); skill
  full content only loads when invoked (descriptions cost a few hundred tokens);
  path-scoped rules load only when matching files are read.
- **Subagents as context isolation**: research runs in a fresh context; only the final
  summary returns, *with token metadata* ("read 6,100 tokens; you got a 420-token
  result"). This is the headline context-savings pattern.
- **`/btw` side questions**: answered from existing context with *no tool access*,
  never enter conversation history, shown in a dismissible overlay; cheap when the
  prompt cache is warm; last 20 side-question exchanges replayed for follow-ups.
- **`/effort` / model / fast mode**: per-session reasoning-effort and model switching
  without clearing the prompt (nbchat already has `/effort` ✓).
- **Prompt caching** with per-message metrics surfaced in the transcript.

### 3.4 Input UX / friendliness

- **`/` menu**: prefix filtering, typo tolerance, fuzzy highlights; a few commands
  hidden from the menu but runnable by full name; commands queued while a turn is
  running, while `/status`, `/tasks`, `/usage` run **immediately** without interrupting.
- **Quick prefixes**: `!` shell mode (output enters context), `@` file-path mention
  with autocomplete, `:` emoji, `?` on empty input opens shortcut help.
- **Editing**: readline conventions, undo, stash/restore prompt (Ctrl+S),
  paste-from-clipboard images as `[Image #N]` chips, `Ctrl+G` open in $EDITOR,
  `Ctrl+L` force redraw (recover from garbled screen), Ctrl+R reverse history search.
- **Prompt suggestions** + optional spell-as-you-type.
- **Permission modes cycled with one key** (Shift+Tab): manual → acceptEdits → plan →
  bypass → auto; mode shown in the indicator; permission answers can carry a free-text
  comment ("Yes, and allow this for the rest of the session").
- **Queueing with take-back**: messages typed while the agent works are queued; Up
  arrow takes a queued message back for editing.
- **`/copy N`** with a code-block picker; `w` writes to file (SSH-friendly).
- **`/branch`, `/fork`, `/clear [name]`, `/resume` picker**: named, branchable,
  resumable conversations; naming the previous conversation when clearing.

### 3.5 Things to explicitly NOT port (size/dependency/fit penalties)

- MCP servers, plugins, hooks, skills infra — extension machinery; nbchat has its own
  tool layer and a small surface is a feature.
- Web/cloud sessions, Remote Control, desktop app, cross-session messaging, `/teleport`,
  agent teams.
- Full-screen renderer + mouse support — keep the lean line-based UI (performance,
  simplicity, SSH-friendliness).
- Vim mode, emoji shortcodes, spellcheck (external binary dependency), voice
  dictation (nbchat has its own voice bridge), `/design`, `/dataviz`, GitHub-specific
  badges (gh CLI dependency) — low value for our user base.

## 4. Recommendations for nbchat.tui (ranked)

### Tier 1 — high value, small footprint, directly serves our values

1. **Persistent status line** (the single biggest "channel" win).
   One line, always updated: `model · mode · context ▓▓▓░ 42% · 12.3k/32k tok ·
   cache 87% · 14.2 tok/s · turn 3 (tool: pytest 2.1s)`. States: `idle`, `thinking`,
   `tool: <name> <elapsed>`, `waiting (retry 2/3 in 5s)`, `compacting…`, `error: …`.
   Leans entirely on existing `monitoring.py`/`task_tracker.py` data.
2. **`/usage`** — tokens in/out, cost-equivalent, cache-hit rate, latency percentiles,
   tool call counts, error count; session and lifetime variants. Reuses
   `SessionMonitor.get_session_report()` + `format_report()`. (Claude Code has this as
   a first-class, immediately-runnable command.)
3. **`/context`** — per-category token/char breakdown of the current window (system,
   memory/instructions, history, tool results) with a bar and a capacity warning +
   suggestion when > ~80% ("compact with /compact or start /new"). Feeds resilience:
   predict the failure instead of hitting it.
4. **Auto-compact with loud narration** — threshold-triggered compaction (compressor.py
   exists) that *always announces*: `context 84% → compacted, ~9k chars freed,
   summary kept N key facts`. Never silent.
5. **Session recap** — on user input after ≥3 min idle and ≥3 turns, show a one-line
   recap (≤200 chars) of what happened, generated from the existing history (one cheap
   local-LLM call; gated so it never fires twice in a row and never blocks input).
6. **`/btw <question>`** — side question answered from current context with *no tools
   and no history pollution*; rendered as a clearly-labeled overlay block
   (`[btw] q: … a: …`). Near-zero cost on a local LLM. Great communication feature.
7. **`/` command completion** — typing `/` lists matching commands with a one-line
   description (prefix/substring match is enough); `?` shows help. Replaces the
   static `/help` dump with discoverability.

### Tier 2 — medium effort, strong reliability/UX payoff

8. **`/doctor`** — checklist self-diagnosis: llama-server reachable (we have `--check`),
   model loaded + expected size, context window fit, disk space, DB writable, config
   sanity; prints `✓/✗/⚠` rows with one-line fixes. Small; huge resilience value.
9. **Visible failure policy** — every tool/LLM error printed as
   `<tool> failed: <reason> · retry 1/3 in 2s`, with a final bounded failure message
   (adopt CC's patterns: bounded retries — 2 for background checks, 3 timeouts — then
   *stop and say why*, e.g. "spellcheck disabled after 2 failures"). Guard the
   llama-server dependency (the main external failure mode) with reconnect + state
   announcement.
10. **Checkpoints / `/rewind` (conversation-first)** — the DB already stores rows;
    snapshot a cheap marker per user prompt. `/rewind` menu: list prompts, choose
    restore-conversation / summarize-from-here. File-state restoration can come later
    (and we document the same limitation CC documents: "not a replacement for git").
11. **Tool-output collapse** — long tool results render as one line
    (`▸ pytest: 142 passed (1.2s) — full output: /toolout <id>`), keeping the channel
    readable; mirrors CC's collapsing of verbose calls.
12. **Permission/safety mode** (Shift+Tab-style, or `/mode`) — e.g.
    `ask` (confirm tool calls) / `auto` (execute) / `plan` (no execution); mode shown
    in the status line. Builds trust and reduces bad-action blast radius.

### Tier 3 — consider later / design inspiration only

13. **Subagent-style delegation** (context isolation): run a research task in a
    separate context, return only a summary *plus token/time metadata*. Even one
    `delegate: <question>` tool would capture the pattern.
14. **Background task list** (`/tasks`-like): nbchat has `--supervisor`; a light list
    of running/finished background jobs with status + elapsed would extend it.
15. **`/copy N`** (copy last assistant message / specific code block to file — SSH
    friendly).
16. **Named branches/forks of a session** (`/branch`): copy the conversation into a
    new session id to try another direction; `/sessions` already lists sessions, so
    this is a modest extension.
17. **Queue-with-take-back** for input typed mid-turn (nbchat currently interrupts;
    CC's pattern: queue, send next turn, Up-arrow to take back).

## 5. Design principles to adopt (the meta-findings)

1. **Silence is a bug.** Every state transition gets a line: starting, tool running,
   error+backoff, retry-exhausted, compacting, waiting, resuming.
2. **Bounded, announced retries.** Always: retry ≤N, backoff visible, then stop with a
   reason and a pointer to the fix. (CC: auto-continue caps at 2; spellcheck at 2
   failures / 3 timeouts.)
3. **Predict failures, don't hit them.** Context metering + auto-compact threshold +
   `/doctor` turn silent crashes into early warnings.
4. **Context is a budget; manage it lazily.** Load on demand, summarize with markers,
   keep side conversations out of history, isolate heavy research.
5. **Metadata everywhere.** Every completed unit of work (task, compaction, even a
   delegated subagent) reports tokens/chars, latency, and cache effectiveness.
6. **Everything reversible, and the user always sees the lever.** Checkpoints, stash,
   take-back, confirmation for destructive ops (double-press patterns).
7. **Graceful degradation.** When an external service/credential is unavailable, fall
   back to a local artifact and say so (CC's offline feedback-bundle pattern applies
   to our email bridge, voice bridge, and llama-server too).

## 6. Open questions for the design phase

- Status line: static bottom row vs. re-drawn per event (line-based UI — what's the
  redraw strategy without full-screen mode; `Ctrl+L`-style manual redraw as escape)?
- Recap generation: piggyback on an existing small/fast model setting, or a fixed
  small prompt to the same model? (Cost is local-time, but latency to the *next*
  prompt must be ~0 → precompute on idle.)
- `/context` accounting: token counts vs. char counts (nbchat currently records chars
  in task_tracker; a char→token heuristic or tiktoken-lite estimate suffices).
- Checkpoints: marker rows in the existing DB vs. JSONL snapshots (CC uses JSONL
  transcripts; our DB approach may be simpler).
- Which of Tier 1 lands first? Suggested order: 1 (status line) → 3/4 (context +
  auto-compact) → 2 (/usage) → 7 (completion) → 6 (/btw) → 5 (recap).
