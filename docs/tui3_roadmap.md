# TUI v3 roadmap — assessment & scope

**Date:** 2026-07-10 · **Base branch:** `tui2-fullscreen-fixes`

## Verdict

tui2 is now a strong, feature-complete *surface*: it has prime-agent's core
UX (live streaming, interjection, session continuity, `/btw`, `/compact`,
`/refine`/`/lessons`/`/memory`, thinking toggle, session picker, scrollback,
reverse search, command palette, `!` shell) **plus** the high-value herdr
items that are surfaces rather than machinery — the interactive
**tool-approval gate** (`/approve`, with `a` always-allow) and the
**attention notification stack** (`/notify`: toast cards + BEL + optional
sound).

What remains from the herdr / prime-agent research is *machinery*: new
subsystems or cross-cutting refactors, not additive commands. Per the
thread goal ("if more features deserve a new version (tui3), we can do that"),
these warrant **tui3** rather than piling onto tui2:

| # | Feature | herdr rank | Effort | Why it is tui3 (not tui2) |
|---|---------|-----------|--------|---------------------------|
| 1 | **Keymap substrate + prefix/mode system** | 5 | S | Single data-driven keymap; unlocks customization + browse/copy. Foundation for 2–4. |
| 2 | **Browse / copy mode** (j/k, gg/G, `/` in-log search, visual copy) | 4 | S-M | A persistent *mode* that captures keys; needs the mode bar + a free entry key (Ctrl+O). |
| 3 | **Mouse support** (wheel scroll, click-select + OSC 52 copy, click-dismiss toasts) | 3 | M | New SGR-mouse decode + render-time hit map; reuses 2's selection. |
| 4 | **User config + settings overlay + hot reload** (theme/sounds/keybinds) | 6 | M | A user file + per-section validation + in-app overlay that writes it; live apply. |
| 5 | **Theming: host sync + auto light/dark + switcher** | 7 | S-M | Theme is hard-wired to `DARK` today; needs threading through every component + OSC 10/11 probe. |
| 6 | **Local JSON socket API + `nbchat-ctl` CLI** | 9 | M-L | A JSON-line RPC socket beside the running harness (status/prompt/events.wait) — makes nbchat observable & scriptable mid-run. |
| 7 | **Detachable background agent** (reattach after disconnect) ✅ shipped (wave 6+) | 10 | L | `--bg` runs the TUI headless (stdin EOF does not quit; it lives on the heartbeat + control socket). `nbchat-ctl bg [--session ID] [prompt]` launches it in its own session (survives the launcher / SSH drop) and submits a first task; `status` / `result` / `quit` drive and stop it. Session + in-flight work survive the terminal going away. |

**Build order** (follows herdr's, adjusted for what tui2 already has):
1 → 2 → 3 → (4, 5 in parallel) → 6 → 7.  **Shipped so far:** wave 1
(keymap + browse + mode bar), wave 2 (in-log search + visual copy),
wave 3 (mouse wheel scroll) + 3b (click / drag-to-copy),
wave 4 (persistent user settings), wave 5
(colour theming via the `_ThemeRef` proxy), wave 6 (external control
socket + `nbchat-ctl`), wave 6+ (detached background agent: `--bg` headless mode + `nbchat-ctl bg` launcher).  All seven roadmap items are now shipped.  Also added (surface-only, read-only, no new
subsystems): `/monitor` — live per-session observability (cache similarity /
invalidation, per-tool call counts and error rates, detected warnings) from
the `nbchat.core.monitoring` engine; `/inbox [n]` — unseen-email browsing
(list headers / read a body) from the `nbchat.core.email_inbox` engine, run
off the UI thread so the IMAP round-trip never freezes the interface; and
`/team [goal]` — the multi-agent team coordinator from `nbchat.core.team`
(`TeamCoordinator` + worker agents), run in the background with its output
relayed into the log off the UI thread (never to the raw screen), plus
`/team` status and `/team stop`; and **Up/Down arrow history recall** — the
arrows walk the submitted-input history (Up = older, Down = back to the
in-progress draft), complementing the existing `Ctrl+R` reverse search.  The
`LineEditor` gained a `set_text()` (buffer replace + undo point) so recall is
undoable; and **`/browse <url>` / `/search <query>`** — a web surface over the
`nbchat.tools.browser` engine (headless Chromium via Playwright).  The page
fetch runs off the UI thread (the Chromium launch never freezes the
interface); `/browse` shows the page title + truncated text, and `/search`
is a convenience wrapper that browses a DuckDuckGo search for the query.
  Also shipped: **`/sup [question]` + `--supervisor`** — the always-on
  supervisor watchdog (v1 `--supervisor` parity).  `ChatApp` already exposes
  the hooks the `Supervisor` needs (`_turn_active`, `interject()`, the
  interjection queue the conversation loop drains), so it binds directly:
  `--supervisor` starts the watchdog (it reviews in-flight work on a timer and
  pushes corrective interjections), and `/sup` shows status while
  `/sup <q>` asks the supervisor about live system state (off the UI thread).
  Also shipped: **`/voice` + `--voice`** — the Alfred voice bridge (v1
  `--voice` parity).  `--voice` starts the `nbchat.voice` bridge on
  `localhost:8765`; an off-thread daemon blocks on its inbound queue and, for
  each transcript, dispatches a tui2-native submit onto the UI thread (via the
  `"call"` event), so a voice turn reuses the exact same turn-launch /
  interjection machinery as keyboard input and the raw screen is never touched
  from a background thread.  `/voice` shows the bridge status.  (This closes
  the last of the v1-only surfaces — chat, sessions, commands, email,
  supervisor, team, and voice are all now tui2-native.)

## tui3 wave 1 (this pass)

**Keymap substrate + browse mode + contextual mode bar.** Purely additive —
no existing binding changes:

- A single data-driven `KEYMAP` (the source of truth) generates both the
  `/hotkeys` reference and the new **mode bar**, so they can never desync.
- A **mode bar** (one line, above the status line) shows the active mode and
  its exact keys.
- **Browse mode**, toggled with **Ctrl+O** (free today; Ctrl+B is the
  editor's backward): `j`/`k` scroll the log, `Home`/`End` jump, `Esc` exits.

## tui3 wave 2 (this pass)

**In-log search + visual copy** (both inside browse mode, purely additive):

- **`/` search the log** — opens a query prompt (rendered in the mode bar).
  Type a case-insensitive substring, Enter to run.  Consecutive matching
  lines are grouped into one match.  The mode bar shows the live query
  while typing and `match i/N` once found.  `n` / `N` cycle to the next /
  previous match and the log **jumps** to bring it into view (the offset is
  set so the match sits at the viewport bottom, clamped to the content
  height).  `Esc` clears the search; `n` with no matches starts a fresh
  query.
- **`v` visual copy** — copies the currently **visible** log viewport (the
  rendered lines at the current scroll offset) to the clipboard via the
  same best-effort OSC 52 escape `/copy` uses, and pushes a small "copied"
  toast.

Both are self-contained in `app.py` (no new files, no core changes).  The
`KEYMAP["browse"]` rows for `/` and `v` drive both the mode-bar hints and
`/hotkeys`.

## tui3 wave 3 (this pass)

**Mouse wheel scrolling** (SGR-extended mouse reporting, purely additive):

- `RawTerminal.enter()` now enables SGR mouse reporting
  (`ESC[?1000h` button tracking + `ESC[?1006h` SGR encoding) after the
  alternate screen / bracketed-paste setup, and `restore()` disables it —
  so no mouse bytes leak into the shell afterwards.  Gated by
  `NBCHAT_NO_MOUSE=1` (some SSH/serial links mangle the report).
- `KeyReader` parses SGR mouse reports (`ESC[<btn;col;rowM/m`) into
  `wheel-up` / `wheel-down` / `mouse-press` / `mouse-release` keys (with
  `col,row` payload).  Wheel buttons 64/66 = up, 65/67 = down.  Unrecognised
  `<`-CSI (bogus button codes) still fall through to the `unknown` swallow,
  so the input buffer can never be corrupted.
- `ChatApp._on_input` maps wheel-up/down to a 3-line `_scroll_log` in **any**
  mode (normal or browse); button press/release are consumed for now
  (click-to-select is a later wave).  The `↑N` indicator (already present for
  PgUp scrollback) shows how far the log is scrolled from the bottom.

**tui3 wave 3b — click / drag-to-copy over the log** (completes the mouse
story, purely additive): a mouse **press** in the log region records the
anchor frame row; a **release** copies the line range `[anchor .. release]`
(a single click copies one line) to the clipboard, with a small "copied"
toast.  The text is read from the *last rendered frame* (`_last_frame_rows`,
populated in `_build_frame`), so no log-index math is needed and it always
matches what the user saw.  The press is only honoured inside the log/live
region (rows `2 .. 1+region`, stored as `_last_log_end`); clicks on the
header / editor / status are ignored.  A wheel scroll drops any pending
anchor (the view is moving).  `KEYMAP["normal"]` documents "click/drag →
copy one / a range of log lines".

**tui3 wave 4 — persistent user settings** (self-contained, purely
additive): a small defensive JSON settings module (`nbchat/tui2/config.py`,
default `~/.nbchat/tui3.json`, override `NBCHAT_TUI3_CONFIG`) persists a
few preferences across sessions.  `ChatApp.__init__` loads them into
`_thinking_visible`, the `_notify` toast/BEL/sound channels,
`_approval_enabled` / `_risky_tools`, and the wheel `scroll_tick`;
`_save_cfg()` (best-effort, never raises) runs on each toggle
(`Ctrl+T`, `/notify`, `/approve`) and in `run()`'s `finally` on exit.
The wheel handler now scrolls by the configurable `scroll_tick`.  Loading
merges over defaults and swallows I/O errors, so a config problem can
never block the TUI.  Tests point `NBCHAT_TUI3_CONFIG` at a temp file so
the real home directory is untouched.

**tui3 wave 5 — colour theming** (self-contained, purely additive):
`/theme` switches the whole UI between the built-in `dark`, `light`, and
`prime` colour sets.  The trick is a small `_ThemeRef` proxy in
`theme.py`: the public `DARK` name is a stable object created once at
import time that forwards every attribute (`DARK.accent`, `DARK.border`, …)
to the *currently-active* theme.  Components keep writing `DARK.<attr>`
unchanged, so `theme.set_active(name)` retargets the proxy in place and the
switch is live everywhere with zero call-site edits.  `/theme` invalidates
the logged turns so they re-render with the new colours, fires a toast, and
persists the choice (`config.py` gained a `theme` key, applied at startup
via `config.set_theme_active`).  `LIGHT` / `PRIME` remain concrete `Theme`
objects for `get_theme` / `all_themes`.

**tui3 wave 6 — external control socket (`nbchat-ctl`)** (defensive,
optional): a running TUI binds a local Unix socket
(`~/.nbchat/tui2-ctl.sock`; override `NBCHAT_CTL_SOCKET`, disable
`NBCHAT_NO_CTL=1`) that an external process drives with
`python -m nbchat.tui2.ctl <cmd> [arg]`.  Protocol is newline-delimited
JSON: request `{"cmd", "arg"}`, response `{"ok", ...}`.  Commands:
`status` (busy/session/model/turns/theme), `sessions`, `result` (last
assistant reply for the current session), `theme <name>`, `send <text>`,
`quit`.  These support a headless / background-agent loop: `send` a task,
poll `status` until `busy` is false, read `result`, then `quit`.  Read-only commands answer on the socket thread
(scalar/atomic reads + read-only db); mutating ones enqueue a closure onto
the UI thread via a new `"call"` event type in the render loop
(`events.put("call", fn)`) and ack `{"queued": true}` immediately — so a
control client can never block or crash the TUI.  The server is a daemon
thread; every socket op is wrapped; `ControlServer.stop()` unlinks the
socket on exit.

**tui3 wave 6+ — detached background agent:** `--bg` (also `NBCHAT_BG=1`) puts the TUI in headless mode: a stdin EOF does not end the render loop (the bg EOF path sleeps briefly and falls through to the event drain, so a control-socket `quit` is still processed).  `nbchat-ctl bg [--session ID] [prompt]` launches a `--bg` TUI in its own session (`start_new_session=True`, so it survives the launcher and an SSH drop), points its control socket at `~/.nbchat/tui2-bg.sock`, logs stdout to `~/.nbchat/tui2-bg.log`, waits for the socket, and submits a trailing prompt as the first task.  The full workflow — detach, drive headlessly, read the result, quit — is verified by `test_bg_mode_quit_via_control_socket` and an E2E pty probe.


**Post-wave-7 additions (v1-surface parity closed, then conversation tooling):**
`/fork [n]` — branch the conversation into a new session.  Bare `/fork` copies
the entire current history; `/fork <n>` copies everything up to and including
your Nth user message, so you can steer the branch differently from the point
you asked it.  It is non-destructive (the original session is left untouched),
built entirely on the existing `nbchat.core.db` engine (`load_history` +
`replace_session_history` + `save_session_title`), and carries the in-flight
task list so the branch keeps its to-dos.  The app switches to the fork and
remembers it as the current session; the fork is titled `fork <src> …`.

**Code revert (rewind):** `/checkpoint [label]` + `/undo [label]` — safe,
git-backed working-tree snapshots.  A checkpoint is `git stash create`
(non-destructive; `HEAD` on a clean tree) and `/undo` restores tracked files
with `git restore --source=…` (untracked files untouched, so it cannot delete
data the user did not ask to touch).  `/undo` with no label previews; applying
requires naming a checkpoint.  Checkpoints are stored in
`~/.nbchat/tui3-checkpoints.json` (override `NBCHAT_TUI3_CHECKPOINTS`) and one
is recorded automatically before the first file edit of each turn.

**Cross-session search:** `/find <query> [session]` — read-only, case-insensitive
full-text search over `chat_log` content (newest first, capped at 25 hits).
Backed by `nbchat.core.db.search_messages()` (a small additive read-only query
helper).  The current session is marked `*`; append `session` to restrict the
search to it.  Jump to a hit with `/load <sid>`.

**Diff review:** `/diff [--stat] [label]` — read-only, colorized unified diff of
tracked-file changes (reuses the agent tool-diff renderer: `+` green, `-` red).
Bare `/diff` = working tree vs `HEAD`; `--stat` = per-file summary; `<label>` =
against a `/checkpoint` (or `last`).  This is the "review" step of the
`/checkpoint` → `/diff` → `/undo` safety workflow.  Capped at 200 lines with a
truncation hint (use `--stat` for large diffs).

**Session export:** `/export [path]` — read-only over the DB; writes one clean
markdown file (H1 title, an "Exported … · session … · N messages" line, then
`**user**` / `**assistant**` / `**tool** — \`name\`` sections with tool output
fenced).  No path → `~/.nbchat/exports/nbchat-<short-sid>-<ts>.md` (override dir
with `NBCHAT_EXPORT_DIR`); a relative or absolute path is honoured.  The
"share/document a conversation" companion to `/find`.

**Plan mode:** `/plan [on|off]` — read-only research mode.  Toggles a
`_plan_mode` flag that (1) blocks file-mutating tools at the tool gate with a
clear "PLAN MODE … blocked" result the model can read, (2) appends a read-only
note to the system prompt (removed cleanly on exit), and (3) shows `plan` in
the mode bar.  Safe: it only ever blocks tools and annotates — it can never
crash or corrupt state.  The "explore without risk" companion to the
`/checkpoint` → `/diff` → `/undo` safety workflow.

**@-file completion:** type `@` then a filename to get a fuzzy-ranked box of matching paths above the editor.  `↑`/`↓` move the selection, `Enter`/`Tab` replaces the `@`-token with the chosen path (via a new additive `LineEditor.replace_range()` that records an undo point), and `Esc` cancels.  Detection lives in `_active_at_token` (the `@` must be at a token start, so `user@example.com` is ignored); the cwd tree is walked once per cwd and cached (`_file_list`, pruning `.git`/`node_modules`/hidden dirs and capping at 20k), ranked with the existing `fuzzy_rank`. Printable chars and backspace pass through to the editor and re-rank live; the completion box is a `Box` rendered above the message editor and is height-capped so the frame stays exactly `term.height`.  The "reference a file by path" primitive the model/user both want in a coding harness.
**Auto-compact:** when the context window crosses a threshold (default 80% of the budget, via `NBCHAT_AUTO_COMPACT`), the session is compacted automatically right after the turn finishes.  Detection happens in `_status_window` (the context-usage hook, which only sets a `_auto_compact_due` flag); the actual compact runs from `_finalize_turn` in an off-thread worker (`_maybe_auto_compact`) so it never interrupts an in-flight turn or the render path.  The worker waits for the send lock to release and defers if a new turn has started; a dim note is delivered on the UI thread via the `"call"` event.  `/context` surfaces the current setting.

**`/retry`:** re-runs the last user message without retyping it (`/retry`, or `/retry <text>` to run a new one).  Captured in `_submit` (the one place every user turn passes through) into `_last_user_text`; the command refuses while a turn is in flight (interrupting is the interjection/Enter path, not retry).  Additive tui2-native command; no core changes.

**Steering queue (Ctrl+Q):** while a turn is running, type a follow-up and press `Ctrl+Q` to queue it (`_queue_key` appends it to `_queue` and clears the editor).  `_finalize_turn` calls `_process_next_queued` (in the `if not chained` block) after each turn winds down, popping the next queued message and submitting it — so queued follow-ups run sequentially.  It uses the same `_turn_thread = None` trick as the redirect path (we're inside the winding-down worker thread, whose `is_alive()` is still True).  `/queue` lists the queue; `/queue clear` empties it.  This is additive: Enter-while-busy still interjects/interrupts (the v1 semantics), so queueing is a separate opt-in path.  No core changes.

**Prompt templates (`/tpl`):** reuse saved prompts.  Drop `*.md` files in `~/.nbchat/prompts/` (override with `NBCHAT_PROMPTS_DIR`); the filename stem is the template name.  `/tpl` lists them; `/tpl <name> [args...]` renders the template and sends it as a turn.  `_render_template` substitutes `$1`/`$2`/… (positional, whitespace-split args) and `$0`/`$ARG` (all args), then flattens to a single line.  If a turn is in flight the rendered prompt is queued (via the steering queue) rather than sent, so `/tpl` never interrupts.  `_prompt_templates` reads the dir on demand (small, no cache). Additive tui2-native command; no core changes.

**External editor (`/editor` / Ctrl+E):** the tui2 input is a single-line editor, so composing a long prompt is awkward.  `/editor` (command) or Ctrl+E pauses the raw terminal (`_launch_external_editor` calls `term.restore()` to return to normal mode), runs `$EDITOR`/`$VISUAL` on a temp file seeded with the current draft (`subprocess.run`), reads the result back (flattened to a single line via `_render`-style whitespace collapse) into the input, then re-enters raw mode (`term.enter()`).  The `_restored` flag is reset to False before re-entering so the app's final `with term:` exit restore still works.  Best-effort: no `$EDITOR` → a note; an editor that errors → a note and the draft is preserved.  Additive; no core changes.  E2E verified: a $EDITOR script that appends to the draft round-trips through the real terminal (restore → editor → enter) and the app exits cleanly.

**Git overview (`/gstatus`):** rounds out the git tooling (`/diff`, `/checkpoint` + `/undo`) with a quick working-tree snapshot.  `_cmd_gstatus` runs `git rev-parse --abbrev-ref HEAD` (branch) and `git status --porcelain` (via `_undo._git`) and buckets each path by its XY code into staged / unstaged / untracked, rendered as a `git status` tool block (capped at `_GSTATUS_MAX` = 50 per category).  Clean tree → a single-line note.  Read-only; additive; no core changes.  E2E pty verified: `/gstatus` in a real repo reports the true working-tree state (branch + counts) and `/quit` exits 0.

**Prompt stash (`/stash`):** a git-stash for the input buffer (after opencode's prompt-stash).  Because the tui2 input is a single-line editor, you can't hold two drafts in memory; the stash lets you push the draft you are composing, work on something else, and pop it back.  `_cmd_stash` supports `push [label]` (pushes `editor.text()`, clears the input), `pop [n]` (loads entry n — default most recent — back into the input), no-arg list (renders a numbered `stash` tool block, truncated to 40 chars each), and `clear`.  Entries persist to `~/.nbchat/tui3-stash.jsonl` (override `NBCHAT_STASH_FILE`), capped at `_STASH_MAX` = 50, as best-effort JSONL (I/O never blocks or crashes).  It complements the Ctrl+Q steering queue, which is in-memory and holds messages to *send*; the stash persists and holds *drafts to compose later*.  Additive; no core changes.  Added a module-level `import json` to app.py (it was previously only a local import).  E2E pty verified: `/stash` renders the empty-stash note and `/quit` exits 0.

**Rewind (`/rewind`):** the #1-ranked feature from the OSS survey — go back N user turns and truncate the conversation to that point (the conversation-only part of the survey's "Rewind / undo with checkpoints"); the *code-revert* part is already covered by `/checkpoint`+`/undo`.  `_cmd_rewind` has three forms: no-arg (lists your recent user turns, newest first, with the number to pass), `<n>` (drop the last *n* user turn(s) and everything after them), and `restore` (put the last rewound slice back).  `_rewind_apply` computes the cut index as `user_idx[-n]` over the `db.load_history` 6-tuple list, keeps `rows[:cut]` and drops `rows[cut:]`, then rewrites the session via the same proven `db.replace_session_history` used by `/fork`/`/undo`.  A safety net: the removed slice is stored in `session_meta` under `rewind_ghost` (JSON, capped at `_REWIND_GHOST_MAX` = 200 rows) so `/rewind restore` can undo the last rewind — but only while the current user-turn count still matches the stored post-rewind count (i.e. you haven't started a new turn), after which the stale ghost is cleared.  `_refresh_history_cache` reloads `self.history`/`task_log`/`_turn_summary_cache` from the DB (same session — `_switch_session` no-ops on an unchanged id) and `_session_changed` resets the UI.  Guards: refuses while `self.busy`; clamps `n` to `[1, #user_turns]`.  Additive; no core changes.  10 new unit tests; E2E pty verified against a pre-populated session (list shows the turns, rewind applies + restore hint, clean exit 0).

**Session pin (`/pin` / `/unpin`):** keeps the sessions you care about at the top of the session picker (survey #6 "session browser pin").  `_cmd_pin` stores a per-session `pinned` flag in `session_meta` (value `'1'`); `_cmd_unpin` clears it.  `_open_picker` reads the pinned ids in a single `SELECT session_id FROM session_meta WHERE key='pinned' AND value='1'` pass and builds each row as `(sid, label, is_pinned)`; a stable `entries.sort(key=lambda e: e[2], reverse=True)` puts pinned rows first while preserving the picker's existing recency order within each group, and pinned rows get a `\u2605 ` (★) prefix.  Additive; the picker's navigation/filter are untouched (they index the same `_picker_sessions` list, now pre-sorted).  3 new unit tests (pin set/clear via a stateful meta patch, pinned-first sort + star glyph via a fake `db._connect`, help-listed); tui2 suite 286 passed; full suite 660 green (286+79+295). E2E pty verified: a fresh `--new` session is not in the picker (no messages yet — the picker only lists sessions with ≥1 message), so the star was confirmed against a pre-populated session (1 message, booted with `python -m nbchat.tui2 --session pine2e`): `/pin` renders, the picker opens and shows the ★, `/quit` exits 0; throwaway DB session cleaned up.

**@-completion frecency (survey #8):** recently-used `@`-files rank higher.  On `_filecomp_accept`, the chosen path is prepended to a recency list (capped at `_FILECOMP_RECENCY_MAX` = 50) persisted best-effort to `~/.nbchat/tui3-filecomp-recency.json` (override `NBCHAT_FILECOMP_RECENCY`) — all I/O wrapped so a hiccup never blocks completion.  `_file_matches` then does a stable post-sort of the `fuzzy_rank` top-match: files in the recency list (that also match the current query) come first, in recency order, with the rest keeping their fuzzy order.  `fuzzy_rank` itself is untouched; this is a clean additive re-sort of the already-ranked list.  3 new unit tests (note_accept records + persists most-recent-first, recency boost moves a recent-but-naturally-last match to the front for query 'a', missing recency file → empty list); tui2 suite 289 passed; full suite 663 green (289+79+295). E2E pty verified: booted in the repo, typed `@nbchat/t` (the fuzzy top match was `nbchat/tools/`), Tab accepted it, and the recency file was created with `['nbchat/tools/']`; /quit exited 0.

## tui3: in-app settings surface (/settings)

**Settings** (survey "in-app settings overlay + hot reload"): `/settings`
shows the current TUI preferences and `/settings <key> <value>` tunes one live.
Builds on the existing `~/.nbchat/tui3.json` persistence — no core change, pure
surface.  The keys are: `theme` (dark|light|prime), `scroll` (lines per
page up/down), `thinking` / `toasts` / `bell` / `sound` / `approve` (on|off),
and `risky` (space-separated risky tool names).  Each setter updates the live
instance attribute (theme via `theme.set_active`, the booleans via the app /
`self._notify` attrs, `scroll` via `self._scroll_tick`) and calls `_save_cfg()`
which re-reads the attributes and persists — so the change is applied AND
remembered with no restart.  A `_parse_onoff` helper normalises on/off
spellings.  Invalid values return a usage line.

Implementation lives entirely in `nbchat/tui2/app.py` (`_cmd_settings`,
_settings_view`, `_parse_onoff`), wired into the handlers dict, `_TUI2_NATIVE`,
and the `/help` addendum.  2 new unit tests (view + live-tune every key +
persistence + bad-value usage; settings listed in help/native set); tui2 suite
291 passed; full suite 665 green (291+79+295).  E2E pty verified.

## tui3: todo / progress pill (agent-maintained task list)

**Todo/progress pills** (survey #4): the agent maintains a short live task
list, surfaced as a `tasks N/M` pill in the status bar and viewable in full
with `/todos`.  This is the one remaining survey item that needed an engine,
so a small additive tool was added.

**New additive tool** — `nbchat/tools/todo.py` (auto-discovered by the tools
package; no existing tool touched): `todo(items)` sets the full list (an array
of `{text, done}` objects; bare strings accepted; `[]` clears; capped at 25).
It persists to a small JSON file (default `~/.nbchat/todos.json`, override
`NBCHAT_TODO_FILE`) via shared `todo_path()` / `load_todos()` / `save_todos()`
helpers the UI also uses, so tool and UI share one source of truth.  The tool
is stateless w.r.t. the session and never blocks/crashes the loop on an I/O
hiccup.  It is additive to the shared toolset, so v1 users also get it
(`nbchat.tui` untouched otherwise).

**Surface** — all in `nbchat/tui2/app.py`: `_todo_pill()` reads the list
(cached ~0.4 s) and appends `tasks N/M` to the status bar via `_status_right()`;
`_cmd_todos` prints the full list; a gentle `[TASK LIST]` nudge is appended to
the system prompt at init so the LLM actually maintains the list.  Wired into
the handlers, `_TUI2_NATIVE`, and `/help`.

**Tests** — 5 new (tool set/load/clear/bad; pill reads the file; `/todos`
renders the list + empty case; the note is in the system prompt; `/todos`
listed in help/native).  tui2 suite 296 passed; full suite 670 green
(296+79+295).  E2E pty verified (status-bar pill + `/todos` list, clean exit).

## tui3: queued pill (steering-queue count in the status bar)

**Queued pill** (harness-survey #4, the second half of "todo/progress pills +
queued pill"): the steering queue (Ctrl+Q) already lets you queue follow-up
messages while a turn runs, and `/queue` lists them.  Now the count is also
surfaced live in the status bar so a walk-away user can see at a glance that
N messages are waiting.  All in `nbchat/tui2/app.py`: `_queue_pill()` returns
`"N queued"` when `self._queue` is non-empty ("" otherwise) and is appended to
the status bar via `_status_right()`, right next to the `tasks N/M` todo pill.
Zero backend change — it reads the existing queue list.  1 new unit test
(`test_queue_pill_reflects_queue`); tui2 suite 297 passed; full suite 671 green
(297+79+295).
