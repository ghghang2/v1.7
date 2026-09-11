# nbchat

nbchat is a lightweight LLM inference harness: an agentic chat loop (tool
calling, streaming, L1/L2 memory, context windowing and output compression)
that talks to a local [llama.cpp](https://github.com/ggml-org/llama.cpp)
server over the OpenAI-compatible API.

The same agent stack (tool loop, memory, context management) powers every
front-end. Only the input/output layer changes:

| Front-end | Start with | Needs |
|-----------|-----------|-------|
| **Terminal UI (TUI)** | `python -m nbchat.tui` | a plain terminal |
| **Email bridge** (extends the TUI) | `python -m nbchat.tui --email` | the TUI + Gmail app password |
| **Supervisor** (extends the TUI) | `python -m nbchat.tui --supervisor` | the TUI (uses the 2nd parallel slot) |
| **Alfred voice bridge** (extends the TUI) | `python -m nbchat.tui --voice` | the TUI + laptop WS client (§13) |
| **WhatsApp channel** | `python -m nbchat.channels.whatsapp_server` | FastAPI + a Node bridge |
| **Jupyter notebook UI** | open a `.ipynb` and `from nbchat.ui.chatui import ChatUI` | Jupyter + `ipywidgets` |

---

## 1. Start the LLM server

```bash
python run.py           # downloads/starts llama-server + installs deps
python run.py --status  # show service status
python run.py --stop    # stop the services
```

The server URL, model and context size come from `repo_config.yaml`. Verify
connectivity without launching a chat:

```bash
python -m nbchat.tui --check     # prints "reachable" / "NOT reachable"
```

---

## 2. Chat from the terminal (TUI)

```bash
python -m nbchat.tui            # or: python nbchat_tui.py
```

Type a message and press Enter — the reply (and the model's reasoning) stream
in live. The agent can call tools repeatedly until the task is done.

```
You:
  List the Python files in the repo and tell me which define a Supervisor class.

  [thinking] Let me list the files, then search for the class…           (dim)
» I'll list the files first.
  [tool] run_command(command="find . -name '*.py' …")
         {"stdout": "./nbchat/core/supervisor.py …"}
» The `Supervisor` class is defined in `nbchat/core/supervisor.py`.

➊
```

### Command-line options

```
--new          force a brand-new session
--session ID   resume a specific session id (see /sessions)
--no-color     disable ANSI colours
--check        only check the llama-server is reachable, then exit
--email        poll the Gmail inbox and inject replies into the chat (§3)
--no-auto-reply with --email: do NOT email the agent's reply back
--supervisor   start the always-on supervisor watchdog (§4, 2nd slot)
--voice        start the Alfred voice bridge on port 8765 (§13); reach it
               from your laptop via: ssh -L 8765:127.0.0.1:8765 user@server
--v2           use the TUI v2 engine (prime-agent-style fullscreen raw-mode
               UI; add --demo after it for the Phase 1 rendering demo)
```

### In-session commands

```
/help            show help
/new             start a new session
/sessions        list terminal sessions (ids start with 'tui:')
/load <id>       load one of the sessions from /sessions
/history         print the current session's messages
/model           show the active model and server
/clear           clear the screen
/sup <question>  ask the supervisor about system state (needs --supervisor)
/team <goal>   run a goal as a team of parallel agents
/team             show team run status / stop
/effort [lvl]   show/set reasoning effort for this session (none|low|medium|xhigh)
/status          show session state (model, tools, effort, memory size)
/stats [N]       task statistics — last N tasks (default all)
/quit            exit (Ctrl+C / Ctrl+D also work)
```

**Input techniques**

- **Multi-line** — end a line with a trailing backslash `\` to wrap it.
- **Interrupt / redirect** — type a new message (or press `Ctrl+C`) while a
  reply is streaming to stop it and redirect the agent immediately.

Sessions persist in `nbchat/chat_history.db`; the most recent one is resumed
on the next start.

### TUI v2 — the fullscreen engine

```bash
python -m nbchat.tui --v2        # or: python -m nbchat.tui2
python -m nbchat.tui2 --demo     # Phase 1 rendering demo (no LLM needed)
python -m nbchat.tui2 --supervisor   # + the always-on watchdog (§ /sup)
python -m nbchat.tui2 --voice        # + the Alfred voice bridge (§ /voice)
```

A prime-agent-style fullscreen UI: a fixed layout
(header, chat log, input box, status line) drawn in raw mode on the
alternate screen with a differential renderer — the conversation streams
in live (thinking blocks, tool panels and the answer text all update as
they arrive) instead of being printed as the v1 REPL does.

It drives the **same** agent stack, so sessions are shared with the v1
REPL: the last session is resumed on start, `--new` forces a fresh one and
`--session <id>` resumes a specific one. Slash commands (`/help`, `/new`,
`/sessions`, `/load`, `/history`, `/effort`, `/quit`, …) work exactly as in
v1 and their output renders inside the UI.

Keys: `Enter` submits · `Esc` interrupts a running turn / cancels a modal ·
`Ctrl+C` interrupts while a turn runs (quits when idle, cancels an open
modal) · `Ctrl+D` submits when the input has text (quits when empty) ·
`Ctrl+L` opens the session picker · `Ctrl+P` opens the command palette ·
`Ctrl+R` reverse-searches the input history · `Up`/`Down` arrows recall the
previous / next input (typing again returns to the in-progress draft) ·
`Ctrl+T` shows / hides thinking
blocks · `PgUp`/`PgDn` page the log, `Home`/`End` jump to top/bottom (a `↑N`
marker shows how far up you are) · `Ctrl+Z/Ctrl+A/Ctrl+E/Ctrl+U/Ctrl+K/Ctrl+W/Ctrl+Y` line
editing.  Typing a new message while a reply is streaming stops that reply
and redirects the agent to the new text.  Lines starting with `!` run a local
shell command (`!ls`); `!!` also stores the output for later.

**tui2-native commands** (handled inside the UI, not the v1 REPL):

- `/context` — model, session, context bar, tool-output compression and
  turn count.
- `/monitor` — live per-session metrics from the monitoring engine
  (cache similarity / invalidation, per-tool call counts and error rates,
  any detected warnings).
- `/inbox [n]` — browse unseen email: `/inbox` lists headers (position,
  sender, subject, date), `/inbox 2` reads the body of unseen message #2.
  Read-only (nothing is marked read — the `--email` bridge owns that) and
  runs the IMAP round-trip off the UI thread, so the interface never
  freezes.  Requires `GHG_APP_PASSWORD` (Gmail app password); with no
  credential it notes that and stops cleanly.
- `/team [goal]` — run a goal as a **team of parallel agents** (the
  multi-agent coordinator in `nbchat.core.team`).  `/team <goal>` starts a
  coordinated run in the background: the coordinator decomposes the goal
  into independent tasks and fans them out to worker agents.  Each worker's
  output is relayed into the log off the UI thread (never to the raw
  screen), so the interface stays live and intact.  `/team` shows the
  current/last status and final report; `/team stop` interrupts a running
  team; `/team roster` shows the **live task-queue view** — every planner
  task and the subtasks it delegated, each with its current status
  (`pending`/`claimed`/`done`/`failed`/`interrupted`), so you can poll
  progress while a run is in flight.  Pure/read-only (reads the live
  `TaskQueue`; never mutates the run).
- `/browse <url>` — fetch a web page with the `nbchat.tools.browser` engine
  (headless Chromium) and show its title + text in the log.  Runs off the UI
  thread (the Chromium launch + fetch never freezes the interface); a missing
  scheme is auto-corrected to `https://`.  `/search <query>` is a convenience
  wrapper that browses a DuckDuckGo search for the query.
- `/sup [question]` — supervisor state query.  `/sup` shows whether the
  always-on watchdog is running, its review/cooldown cadence and how many
  corrective interjections it has pushed; `/sup <question>` asks the
  supervisor about the live system state (server, git, tasks, the
  assistant's current progress).  The answer is an LLM call on the
  supervisor's own slot, so it runs off the UI thread and is delivered into
  the log.  Requires the supervisor to be started (`--supervisor` /
  `NBCHAT_SUPERVISOR=1`).
- `/voice` — status of the Alfred voice bridge.  `--voice` (or
  `NBCHAT_VOICE=1`) starts the bridge on `localhost:8765`; a laptop Alfred
  client reaches it over an SSH tunnel (`ssh -L 8765:127.0.0.1:8765
  user@server`) and POSTs transcripts, which are auto-submitted as user
  turns exactly like keyboard input (off the UI thread, so the raw screen
  is never touched from a background thread).
- `/fork [n]` — branch this conversation into a new session.  Bare `/fork`
  copies the entire current history; `/fork <n>` copies everything up to and
  including your Nth message, so you can steer the branch differently from
  the point you asked it.  The original session is left completely untouched
  and the app switches to the fork (the fork is titled `fork <src> …`).
- `/checkpoint [label]` — record a restorable snapshot of the working tree
  (tracked files) via `git stash create` (a non-destructive record; falls back
  to `HEAD` on a clean tree).  A checkpoint is also recorded automatically
  before the first file edit of a turn, so a botched agent edit is always
  revertible.
- `/undo [label]` — with no label it is **preview only** (lists checkpoints
  and shows what restoring the latest would change); with a label it restores
  tracked files to that checkpoint via `git restore --source=…` (untracked
  files are left alone).  Reverting is itself reversible: take a fresh
  `/checkpoint` first.
- `/find <query> [session]` — case-insensitive full-text search over message
  content, across **all** sessions by default (append `session` to search only
  the current one).  Each hit shows the session, role, and a snippet; the
  current session is marked `*`.  Open a hit with `/load <sid>`.
- `/diff [--stat] [label]` — review tracked-file changes as a colorized diff
  (`+` green / `-` red).  Bare `/diff` shows the working tree vs `HEAD`;
  `/diff --stat` shows the per-file summary; `/diff <label>` diffs against a
  `/checkpoint`.  Pairs with `/undo` (review, then revert).
- `/export [html] [path]` — save the current session as a clean markdown file, or a self-contained HTML page (`/export html`): role-coloured message blocks, tool panels, HTML-escaped content
  (user/assistant/tool sections, tool output fenced).  Defaults to
  `~/.nbchat/exports/` so it never pollutes the working tree; pass a path to
  choose.  Pairs with `/find` (search, then export a conversation).
- `/plan [on|off]` — read-only research mode.  While on, file-mutating tools
  (`create_file` / `make_change_to_file` / `run_command`) are blocked at the
  tool gate and a read-only note is added to the system prompt, so the agent
  researches and proposes a plan instead of editing.  The mode bar shows
  `plan`.  Pairs with the safety suite (`/checkpoint` → `/diff` → `/undo`).
- `@<file>` — file completion.  Type `@` then a filename; a fuzzy-ranked
  box appears above the editor: `↑`/`↓` pick, `Enter`/`Tab` inserts the path
  in place of the `@`-token (undoable), `Esc` cancels.  The `@` inside an
  email address is ignored; directories carry a trailing `/`.
  Recently-used `@`-files jump to the top of the match list (frecency;
  best-effort `~/.nbchat/tui3-filecomp-recency.json`, override
  `NBCHAT_FILECOMP_RECENCY`).
- **Auto-compact** — when the context window crosses a threshold (default
  80% of the budget), the session is compacted automatically right after the
  turn finishes (off the render path; a dim note confirms it).  Toggle/tune with
  `NBCHAT_AUTO_COMPACT` (`0`/`off` disables, a float like `0.6` sets the threshold);
  `/context` shows the current setting.
- `/retry [text]` — re-run your last message (or a new one) without retyping it;
  handy after a failed/unsatisfying turn.  Refuses while a turn is in flight.
- **Steering queue (Ctrl+Q)** — while a turn runs, type a follow-up and
  press `Ctrl+Q` to queue it; queued messages run one at a time as each
  turn finishes.  `/queue` lists them, `/queue clear` empties the queue.
  (Pressing `Enter` while a turn runs still interjects/interrupts, as
  before — queueing is a separate, opt-in path.)
- **Prompt templates** — drop `*.md` files in `~/.nbchat/prompts/` (or
  `NBCHAT_PROMPTS_DIR`); `/tpl` lists them, `/tpl <name> [args...]` renders
  and sends one (`$1` `$2` … are positional args, `$0`/`$ARG` = all of
  them).  While a turn runs the rendered prompt is queued, not sent.
- **External editor** — `/editor` or `Ctrl+E` opens the current draft in
  `$EDITOR` (`$VISUAL` fallback) to compose a long prompt; the result is
  loaded back into the input.  Requires `$EDITOR`/`$VISUAL` to be set.
- **Git overview** — `/gstatus` shows the working-tree state at a glance
  (branch, staged / unstaged / untracked), complementing `/diff` and
  `/checkpoint`.  Read-only.
- **Prompt stash** — `/stash push [label]` saves the draft you are
  composing (persists to `~/.nbchat/tui3-stash.jsonl`, up to 50 entries);
  `/stash pop [n]` loads a stashed draft back into the input; `/stash`
  lists them and `/stash clear` empties them.  Complements the Ctrl+Q
  steering queue (which queues messages to *send*; the stash holds
  *drafts to compose later* and survives restarts).
- **Rewind** — `/rewind [n|restore]` drops the last *n* user turn(s) and
  everything after them from the current session (the #1-ranked survey
  feature; complements `/checkpoint`+`/undo`, which revert *files*).
  Bare `/rewind` lists your recent user turns with the number to pass.
  The removed slice is kept recoverable (one level) via
  `/rewind restore` until you start a new turn.
- **Session pin** — `/pin` pins the current session to the top of the
  session picker (Ctrl+L); `/unpin` un-pins it.  Pinned sessions are
  marked with a ★ and sort above the rest, so important sessions are
  always one jump away even as your session list grows.
- **Settings** — `/settings` shows your TUI preferences and `/settings <key>
  <value>` tunes one live (theme, scroll, thinking, toasts, bell, sound,
  approve, risky) — persisted to `~/.nbchat/tui3.json` and applied without
  a restart.
- **Todo / progress pill** — the agent keeps a short live task list (via the
  `todo` tool) shown as a `tasks N/M` pill in the status bar; `/todos` prints
  the full list.  Works on any multi-step task so you can watch progress.
  The steering queue shows up the same way: a `N queued` pill appears in the
  status bar while you have messages queued (Ctrl+Q) to run after the turn.
- `/hotkeys` — the keybinding reference.
- `/copy` — copies the last assistant message to the clipboard (OSC 52;
  silent no-op where the terminal lacks clipboard support).
- `/compact [focus]` — a manual, one-shot compaction of the context window
  (reuses the engine's summarization) with a before/after report; the
  per-turn auto-windowing is left untouched.
- `/refine [instructions]` — schedule a manual refinement round over the
  last task; `/refine rollback` reverts the most recent round.
- `/lessons` — the applied refinement lessons.
- `/memory` — the L1 core memory block plus L2 episodic stats.
- `/btw <question>` — a throwaway side question on an isolated agent so
  the current session/history is not touched.
- `/approve [on|off|add <t>|rm <t>|list]` — a herdr-style tool-approval
  gate: risky tools (shell / push / email by default) show a confirm
  prompt (`y` approve, `n`/`Esc` decline, `a` **always** — approve now and
  stop prompting for that tool this session) before they run.
- `/notify [toasts|bel|sound on|off]` — the in-TUI notification stack:
  transient toast cards (above the input box) plus a terminal `BEL` and an
  optional `.wav` (set `NBCHAT_SOUND_DIR`, kill switch `NBCHAT_NO_SOUND=1`).
  Fires on turn-complete (quiet), a pending approval, and a failed shell
  command.  `/notify test [kind]` fires a sample.
- `/goal <objective>` — a running goal (prime-agent style): after each
  turn the app auto-continues toward the objective until the turn budget
  is exhausted, the model replies with `GOAL COMPLETE`, or you run
  `/goal stop`.  `/goal` for status · `/goal stop` · `/goal clear` ·
  `/goal budget <n>`.  A `goal K/N` pill tracks progress on the status
  line.
- `/autonomous <objective> [--auto]` — an autonomous run with an **approval
  gate** (prime-agent): like `/goal` it auto-continues toward the objective,
  but **pauses after each turn** and asks you to continue. `/autonomous go`
  runs the next turn (gate stays on), `/autonomous auto` switches to silent
  auto-continue (like `/goal`), `/autonomous stop` halts. `--auto` starts it
  silently. The gate is the difference from `/goal`: you keep a hand on the
  wheel without retyping the objective.
- `/name <title>` — alias for v1's `/title`.
- Bare `/load` (no id) opens the **session picker**: type to fuzzy-filter,
  `↑/↓` move, `Enter` loads, `Esc`/`Ctrl+C` cancel.
- `!cmd` / `!!cmd` — run a local shell command and show its output as a
  bordered panel (exit code in the title); `!!` also stores the combined
  output on the app for later reference.
- `Ctrl+P` — a fuzzy **command palette** over every command above; pick one
  and it is inserted into the editor for confirmation.  `Ctrl+R` — fuzzy
  **reverse search** over the input history; pick a line and it is re-entered
  for editing.

**TUI v3 — keymap substrate + browse mode** (on the same engine):

- A **mode bar** (one line above the status line) shows the active mode and
  its key hints.  Both the mode bar and `/hotkeys` are generated from a
  single data-driven `KEYMAP`, so they can never desync.
- **Browse mode** — press `Ctrl+O` to read/scroll the log without typing:
  `j`/`k` step down/up, `PgUp`/`PgDn` page, `Home`/`End` jump; `Esc` (or
  `Ctrl+O`) leaves and snaps back to the bottom.  Inside browse mode,
  `/` **searches the log** (type a query, Enter to find all matches,
  `n`/`N` cycle to the next/previous match — the log jumps to it), and
  `v` **copies the visible log** to your clipboard.
- **Mouse wheel scrolling** — the mouse wheel (SGR-encoded) scrolls the
  conversation log up/down in any mode, with the `↑N` indicator showing how
  far you've scrolled from the bottom.  Opt out with `NBCHAT_NO_MOUSE=1`
  (for links that mangle the mouse-report escape).
- **Click / drag-to-copy** — click a log line (or drag across several) and
  release to copy that line / line-range to your clipboard (the `v` key in
  browse mode copies the whole visible viewport).  A small "copied" toast
  confirms it.
- **Colour theming** — `/theme` switches the whole UI between the built-in
  `dark`, `light`, and `prime` colour sets live (every component re-colours
  on the next render), and the choice is remembered across sessions.
  `/theme auto` (or `/settings theme auto`) auto-detects your terminal's
  light/dark appearance (DECSTERA) and picks the matching theme at each start,
  so a light terminal gets the light theme with no fuss.
- **Persistent settings** — a few preferences survive across sessions,
  saved to `~/.nbchat/tui3.json` (override with `NBCHAT_TUI3_CONFIG`):
  thinking-block visibility (`Ctrl+T`), the toast / BEL / sound channels
  (`/notify`), the tool-approval gate and its risky-tool list (`/approve`),
  the mouse-wheel scroll tick, and the active theme (`/theme`).  They load
  at startup and save on each toggle and on exit.
- **Project instructions auto-load** — at start, nbchat looks for an
  `AGENTS.md` / `CLAUDE.md` (or the lowercase variants) in the working
  directory and the git repo root and loads it into the system prompt so the
  agent follows repo conventions (capped at 16 KB).  `/project` shows what
  was loaded; `NBCHAT_NO_PROJECT_INSTRUCTIONS=1` disables it.
- **Recurring instruction (`/heartbeat`)** — `/heartbeat every <dur> <instruction>`
  fires the instruction as a turn every `<dur>` (5 / 30s / 5m / 1h) whenever the
  session is idle — poll CI, watch a build, nudge a long-running task. It never
  interrupts a running turn (defers to the next idle moment). `/heartbeat` shows
  the current heartbeat; `/heartbeat clear` stops it.
- **Debug log (`/log [N]`)** — the TUI2 redirects stderr (mid-stream retries,
  warnings, logging noise) to `~/.nbchat/tui2-stderr.log` so it never corrupts
  the raw screen. `/log` (or `/log [N]`, default 30 lines) tails that file in
  the TUI for debugging. Read-only.
- **External control socket (`nbchat-ctl`)** — a running TUI listens on a
  local Unix socket (`~/.nbchat/tui2-ctl.sock`; override `NBCHAT_CTL_SOCKET`,
  disable `NBCHAT_NO_CTL=1`) that a script or another process can drive:

  ```
  python -m nbchat.tui2.ctl status            # busy / session / model / turns / theme
  python -m nbchat.tui2.ctl sessions          # list tui: sessions
  python -m nbchat.tui2.ctl result            # last assistant reply (this session)
  python -m nbchat.tui2.ctl theme light       # switch the colour theme
  python -m nbchat.tui2.ctl send "hello"      # submit a message
  python -m nbchat.tui2.ctl quit              # request a clean exit
  ```

  Together these support a **headless / background agent** loop: submit a
  task with `send`, poll `status` until `busy` is false, then read the
  answer with `result` (and `quit` when done).

  **Detached background agent.**  `--bg` runs the TUI headless (a stdin EOF
  does not quit it — it stays alive on its heartbeat and is driven through
  the socket).  `nbchat-ctl bg` launches one in its own session so it
  survives the launcher exiting (detach, tmux-style) and optionally submits
  a first task:

  ```
  python -m nbchat.tui2 --bg --session tui:abc        # run it headless
  python -m nbchat.tui2.ctl bg --session tui:abc "summarise the repo"
  #   -> {"ok": true, "pid": …, "socket": ~/.nbchat/tui2-bg.sock, …}
  NBCHAT_CTL_SOCKET=~/.nbchat/tui2-bg.sock python -m nbchat.tui2.ctl status
  NBCHAT_CTL_SOCKET=~/.nbchat/tui2-bg.sock python -m nbchat.tui2.ctl result
  NBCHAT_CTL_SOCKET=~/.nbchat/tui2-bg.sock python -m nbchat.tui2.ctl quit
  ```

  Read-only commands answer synchronously; mutating ones are enqueued onto
  the UI thread and acked immediately (`{"queued": true}`), so a control
  client can never block or crash the TUI.
- The remaining tui3 scope (detachable background agent) is tracked in
  `docs/tui3_roadmap.md`.

The v1 print-based REPL is unchanged and remains the default; v2 is
opt-in via the flag above.  Voice / email / supervisor / team surfaces
start from the v1 entry point (their status output is print-based); the
chat, sessions and commands work in v2.  See `docs/tui2_issues.md` for the
2026-07-10 fix log, `docs/tui3_roadmap.md` for the v3 plan, and
`docs/prime_tui_port_tracker.md` for the port plan.

---

## 3. Email bridge — your inbox becomes the chat input box

A daemon thread polls your Gmail inbox (IMAP) and injects **matching** emails
into the chat stream as user interjections — exactly as if you typed them.
Optionally it sends the agent's reply back by email.

```bash
export GHG_APP_PASSWORD="your-16-char-app-password"
python -m nbchat.tui --email            # auto-reply ON (default)
python -m nbchat.tui --email --no-auto-reply   # inject only, don't email back
```

### Example

While the TUI is open, send yourself a Gmail email:

```
From:    ghghang2@gmail.com
Subject: nbchat: what files are dirty in git right now?
Body:    check git status and tell me.
```

The bridge detects it within one poll interval (default 3 s) and the agent
answers in the terminal — and (with auto-reply) emails you the answer back.

You get three emails back, all in the **same Gmail thread** (replies carry
`In-Reply-To`/`References` headers, so nothing scatters into a new thread):

1. **Ack** — sent the moment the email is queued:
   `Received: <subject> / Priority: low / You are in the queue.`
2. **Working** — sent when the worker starts on it:
   `Working on: <subject> / This may take a moment.`
3. **The answer** — the agent's reply, under `Re: <subject>`.

Replying to any of these system emails in Gmail is safe and works as a
normal new command — the bridge identifies its own outbound mail by the
`X-Nbchat: outbound` header (added to every message it sends, including
`send_email` tool output), not by the subject line.

### What gets injected

Only emails that satisfy **all** of these are processed:

1. **Not** one of the bridge's own outbound emails (no `X-Nbchat` header).
2. Sent **from your own address** (`ghghang2@gmail.com`).
3. Subject contains `nbchat` (routes to the assistant) **or** `supervisor`
   (routes to the supervisor, when one is running — see §4).
4. Sent **since this chat session started** (a 60 s grace window). Older
   unread mail is left untouched — never read, never replied to.

Everything else is silently marked read and ignored. Emails are marked read
only **after** they have been injected, so a crash never discloses a message.

### Priority & preemption

Emails whose subject contains `supervisor`, `urgent` or `high priority`
(case-insensitive) are **high priority**: they jump ahead of queued
low-priority emails, and if a low-priority email is *already being
processed*, the in-flight turn is interrupted and the low-priority email is
re-queued to be retried afterwards. High-priority emails also get a
`Priority: high` line in the ack.

Under the hood, detection (a fast header-only IMAP peek, with batched
mark-read) and processing (the LLM turn) run in separate threads on a
priority queue, so a slow turn never delays pickup of the next command.

### Configuration

| Key in `repo_config.yaml` | Default | Effect |
|---------------------------|---------|--------|
| `email_poll_interval`     | `3`     | seconds between IMAP polls. |
| `email_auto_reply`        | `true`  | send the agent's reply back to the sender. |

---

## 4. Supervisor — a second, always-on LLM on the 2nd parallel slot

The supervisor is an independent LLM instance that runs on the server's
second parallel slot (`n_parallel: 2`), so it never blocks the assistant's
in-flight turn. It has two capabilities:

1. **State queries** — answer a question about the server, git status, task
   stats, or the assistant's current progress, with one non-streaming call.
2. **Watchdog** — periodically review the assistant's in-flight work and, if
   it looks off-track, inject a one-line corrective instruction into the
   assistant's interjection queue (drained at the next safe point).

```bash
python -m nbchat.tui --supervisor
```

### Ask the supervisor from the terminal

```
➊ /sup what model and context size are we running?
  [supervisor] asking: what model and context size are we running?
  [supervisor] Model Qwen3.8-27B-GGUF:UD-Q4_K_XL, ctx 131072, n_parallel 2.
```

The call runs on a background thread, so the prompt returns immediately and
you can keep typing while the supervisor answers.

### Ask the supervisor by email

With `--supervisor` and `--email` both on, an email whose subject contains
`supervisor` is routed to the supervisor instead of the assistant. The
**body** is treated as the question (the subject is used only for routing
and threading), and the answer is emailed back in the same thread:

```
From:    ghghang2@gmail.com
Subject: supervisor: how many errors in the chat log?
Body:    give me the task stats.
```

Supervisor emails are given **queue priority** (see §3) over normal emails,
so a question is answered in real-time even if it arrives while a long
normal email turn is still queued — the in-flight low-priority turn is
interrupted and re-queued.

### Configuration

| Key in `repo_config.yaml` | Default | Effect |
|---------------------------|---------|--------|
| `supervisor_enabled`       | `false` | start the watchdog by default (also gated by `--supervisor`). |
| `supervisor_interval`      | `60`    | seconds between watchdog reviews. |
| `supervisor_cooldown`      | `300`   | min seconds between two interjections. |
| `supervisor_max_output_tokens` | `512` | max tokens for a supervisor answer. |

---

## 5. Multi-agent team — parallel task execution

For large or decomposable goals, `/team` spawns a team of parallel worker
agents coordinated by a planner LLM. Each worker is a full `TerminalAgent`
with its own session, history, and tool access — they run concurrently and
their output is prefixed `[Wn]` so interleaved streams stay readable.

```
⺷ /team refactor all db.py calls to use the new get_history helper
  [team] planning run a3f2b1c9 ...
  [team] task 1 (t1): Update db.py to add the get_history function
  [team] task 2 (t2): Replace all direct sqlite cursor calls in agent.py with get_history
  [W1] [thinking] Let me look at db.py first...
  [W2] [tool] run_command(command="grep -n cursor nbchat/tui/agent.py")
  ...
  [team] report (done):
  Both tasks completed successfully. get_history was added to db.py and
  14 call-sites in agent.py were migrated. No failures.
```

### How it works

1. **Plan** — the coordinator makes one non-streaming LLM call to decompose
   the goal into 2-8 independent tasks.
2. **Dispatch** — up to `team_max_workers` (default 4) threads pull tasks
   from a shared `TaskQueue` and execute them in parallel.
3. **Arbitrate** — a `ToolArbiter` wraps the tool executor at module level
   to serialize repo-mutating tools (`run_command`, `make_change_to_file`,
   `create_file`, `push_to_github`) and test runs (`run_tests`) with
   per-resource locks, so concurrent writes never interleave.
4. **Synthesize** — after all tasks finish (or the timeout expires), the
   coordinator makes a final LLM call to produce a user-facing report.

### Commands

```
/team <goal>       start a coordinated run (background; live [Wn] output)
/team              status of the last run
/team stop         stop the current run (tasks wind down gracefully)
/team roster       live task-queue view (per-task status + subtasks)
```

### Configuration (`repo_config.yaml`)

| Key | Default | Effect |
|-----|---------|--------|
| `team_enabled` | `true` | master switch; if `false`, `/team` prints a notice and exits. |
| `team_max_workers` | `4` | max parallel workers per run. |
| `team_max_tasks` | `8` | max tasks the planner may emit per run. |
| `team_task_timeout` | `900` | seconds before a single task is interrupted. |
| `team_plan_max_tokens` | `2048` | max tokens for the planner LLM call. |
| `team_synthesis_max_tokens` | `1536` | max tokens for the synthesis LLM call. |

> **Note** — the team shares the same `n_parallel` LLM slots as the main
> assistant. With `n_parallel: 2`, at most 2 workers generate at once; the
> rest queue inside llama.cpp. Raise `n_parallel` for higher throughput.

---

## 6. WhatsApp channel

A headless agent + FastAPI bridge serves WhatsApp messages over HTTP. Each
sender JID gets its own isolated session (prefixed `wa:`) in the shared
`chat_history.db`.

```bash
python -m nbchat.channels.whatsapp_server
# or:
uvicorn nbchat.channels.whatsapp_server:app --host 127.0.0.1 --port 8764
```

### Example

The Node bridge (`whatsapp_bridge.js`) forwards an inbound message:

```bash
curl -X POST http://127.0.0.1:8764/message \
  -H "Content-Type: application/json" \
  -d '{"jid": "+15551234567@s.whatsapp.net", "text": "what is the repo status?"}'
```

Response:

```json
{"reply": "The repo is on branch main, 2 files dirty, all tests passing."}
```

---

## 7. Jupyter notebook UI

The full widget-based chat interface. In a notebook cell:

```python
from nbchat.ui.chatui import ChatUI
chat = ChatUI()
```

This renders the streaming chat UI (reasoning, tool calls, monitoring panel)
using `ipywidgets`. It reuses the exact same `ContextMixin` +
`ConversationMixin` stack as the TUI and WhatsApp channel.

---

## 8. Tools

Tools are auto-discovered from `nbchat/tools/` (any module exposing a `func`
callable plus `name`/`description`). They are shared by **all** front-ends.
The model decides when to call them; you don't invoke them directly.

| Tool | What it does | Example call the model makes |
|------|--------------|------------------------------|
| `run_command` | Run a shell command in the repo, return stdout/stderr/exit code. | `run_command(command="git status --porcelain")` |
| `run_tests` | Run the pytest suite, return pass/fail counts. | `run_tests()` |
| `create_file` | Create a new file under the repo root. | `create_file(path="notes.md", content="…")` |
| `make_change_to_file` | Apply a unified diff (create/update/delete). | `make_change_to_file(path="a.py", op_type="update", diff="…")` |
| `get_weather` | Current/forecast weather for a city. | `get_weather(city="Berlin")` |
| `browser` | Visit a URL, perform actions, extract page text. | `browser(url="https://example.com")` |
| `push_to_github` | Commit **staged** changes (pass `stage_all=true` for everything), run the pytest suite and refuse to push on failure, then push the active branch. `repo_name` targets a different repo; `dry_run=true` previews the plan. | `push_to_github(commit_message="…")` |
| `repo_overview` | Build a markdown table of all Python functions. | `repo_overview()` |
| `send_email` | Send a plain-text Gmail email (stamped `X-Nbchat: outbound` so the email bridge never re-injects it). | `send_email(subject="…", body="…")` |

### Example: a multi-tool task

```
You:
  Find the tests that touch the email bridge, run them, and if any fail, fix the
  first one and push.

» I'll locate the tests, run them, then act on the result.
  [tool] run_command(command="grep -rl email_bridge tests/")
  [tool] run_tests()
  [tool] make_change_to_file(path="tests/test_email_bridge.py", …)
  [tool] run_tests()
  [tool] push_to_github(commit_message="Fix email bridge test")
» Done — all 95 tests pass and the fix is pushed.
```

---

## 9. Memory (L1 core + L2 episodic)

The agent keeps long conversations coherent with two memory layers, shared by
every front-end:

- **L1 core memory** — a compact, always-injected block holding the current
  goal, constraints, active entities and recent errors. Updated as the
  conversation progresses so context survives very long sessions.
- **L2 episodic memory** — importance-scored past tool exchanges, persisted to
  `chat_history.db` and retrieved back into context when topically relevant
  (matched against the active entities).

Tuning lives in `repo_config.yaml`:

| Key | Default | Effect |
|-----|---------|--------|
| `l2_retrieval_limit` | `5` | max episodic exchanges retrieved per turn. |
| `persist_fraction` | `0.40` | top 40% of exchanges by importance go to L2. |
| `core_memory_active_entities_limit` | `20` | max entities tracked in L1. |

---

## 10. Context windowing & output compression

- **Token-budget windowing** — history is walked back to fit the model
  context (`context_headroom_ratio`, `prefix_token_reserve`). Evicted turns
  are asynchronously summarised into a structured prior-context block; a hard
  trim is the last resort.
- **Output compression** — large tool outputs are compressed (skeletons for
  code/JSON/YAML, head/tail otherwise) before going back to the model to save
  context.
- **Silent-exit fallback** — the agentic loop is designed to end only via an
  explicit `break` (final answer, `max_tool_turns`, or user stop). If a turn
  ever falls through the loop without one (e.g. a malformed `tool_calls`
  payload that skips every handling branch), the loop's `else` clause posts a
  visible notice ("Turn ended without a final answer. Send any message to
  pick up where I left off.") and logs a warning, so the session can never
  appear to hang with no output.

---

## 11. Configuration

All runtime values live in **`repo_config.yaml`** at the repo root. The most
commonly edited keys:

| Key | Effect |
|-----|--------|
| `SERVER_URL`, `MODEL_NAME` | Where and what to talk to. |
| `n_parallel` | Parallel slots (2 = assistant + supervisor). |
| `max_tool_turns`, `stall_turns` | Agentic loop limits + stall detection. |
| `context_headroom_ratio`, `prefix_token_reserve`, `persist_fraction` | Context / memory tuning. |
| `browser_timeout`, `tests_timeout`, `other_tools_timeout` | Per-tool wall-clock budgets (s). |
| `email_poll_interval`, `email_auto_reply` | Email bridge. |
| `supervisor_enabled`, `supervisor_interval`, `supervisor_cooldown` | Supervisor. |
| `voice_enabled`, `voice_port`, `voice_status_min_interval` | Alfred voice bridge (§13). |
| `refine_hook_enabled`, `refine_llm_timeout` | Refinement engine (§12). |

---

## 12. Refinement engine — post-task self-review

After every task turn the conversation loop evaluates a pure trigger
(`refinement.should_refine_task`): did the task end in failure, hit a tool
error, or take an unusually long path? If so, a single background thread runs
one refine round against the LLM (never on the critical path, never blocking
the user's prompt).

The round does three things:

1. **Review** — the LLM reviews the task summary, harness state, and recent
   lessons, and proposes *sanitised* edits: a new lesson, a prompt tweak, or
   a config note. Raw LLM output is parsed by `parse_refine_response`, which
   tolerates fenced blocks plus a small repair ladder for common LLM JSON
   malformations (trailing commas, `//` comments, unescaped quotes inside
   string values); the `repaired` flag is recorded on the round's audit
   event. Every edit passes `sanitize_edits` — deduplicated against existing
   lessons, size-capped, and stripped of anything that is not a plain text
   record. Nothing the LLM says is executed.
2. **Apply** — accepted edits are written to the `lessons` table in
   `chat_history.db` with a per-round snapshot. `lessons` feed back into the
   next review and into L1 core memory, so the agent's self-corrections
   compound across sessions.
3. **Audit & rollback** — every round is logged to the audit tables, and
   `undo_last_round(session_id)` restores the previous snapshot if an edit
   turns out to be wrong.

Wiring and kill-switch live in `nbchat/core/refine_hook.py`; the pure engine
is in `nbchat/core/refinement.py` (fully unit-tested in
`tests/test_refinement.py`). Disable with `refine_hook_enabled: false`.

---

## 13. Alfred voice bridge — talk to the agent from your laptop

`--voice` (or `voice_enabled: true`) starts a localhost FastAPI bridge
(default port 8765). The laptop runs a tiny WebSocket client that turns
speech in and speech out; the agent speaks as **Alfred** — a composed,
dry-witted butler who states task *state* (never task content), in
one or two sentences, addressed as "sir".

```bash
ssh -L 8765:127.0.0.1:8765 user@server   # forward the bridge port
```

The agent is told about the voice channel by the `ALFRED_VOICE_PROMPT`
appended to its system prompt, so it knows when and what to say aloud
(task start, milestones, completion, failure, being blocked) and when to
stay silent.
Change a value and restart the server / front-end for it to take effect.

---

## Layout

```
nbchat/
  core/     config, OpenAI client, SQLite db, compressor, monitoring, retry,
            supervisor, email_inbox (IMAP), email_smtp (SMTP),
            remote (git + GitHub client for push_to_github)
  tools/    auto-discovered tool functions (run_command, git, browser, …)
  ui/       context_manager (L1/L2 memory + windowing), conversation (agentic
            loop), chatui (Jupyter), tool executor, styles
  tui/      terminal UI (TerminalAgent + REPL + email bridge)
  channels/ WhatsApp bridge (FastAPI + Node)
run.py      start/stop the local llama-server
repo_config.yaml   all runtime configuration

bench/      throughput harnesses + probe scripts; bench/lab/c8lab holds the
            C=8 saturation simulation; bench/results/ has raw run data
docs/       design docs & guides (multi_agent, tui_usage, voice_setup, …);
            docs/archive/ holds completed review/tracker documents
tests/      pytest suite (network mocked; no live server needed)
```

---

## Tests

```bash
python -m pytest -q
```

The TUI, email-bridge and supervisor tests (`tests/test_tui.py`,
`tests/test_email_bridge.py`, `tests/test_supervisor.py`) do **not** require a
running llama-server or a real IMAP/SMTP connection — network calls are
mocked.
