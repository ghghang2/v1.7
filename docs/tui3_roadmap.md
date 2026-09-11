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

## tui3: auto light/dark theme (herdr #7, the low-risk slice)

**Auto light/dark** (herdr survey #7 "host-theme sync + auto light/dark"): the
full host-sync (truecolor re-palettizing) would need the 16-colour theme model
reworked, so this ships the high-value, low-risk slice — detecting the
terminal's light/dark appearance and picking the matching *existing* theme.

- **Terminal probe** — `RawTerminal.probe_appearance(deadline=0.35)` sends
  DECSTERA (`CSI ? 996 n`) and reads the report (`CSI ? 996 ; 1|2 ; …`),
  returning `'light'` / `'dark'` / `None` (unsupported).  Bounded read (short
  deadline), best-effort, never raises, so a non-responding terminal only adds
  a brief delay.  Local `re`/`select`/`time` imports keep `raw.py` deps clean.
- **Wiring** (all in `nbchat/tui2/app.py`) — `theme: auto` is a *preference*,
  not a concrete theme: `_apply_auto_theme()` runs once after raw mode is
  entered (before the render loop reads the input fd, so no reader contends
  for the report), probes, and switches light/dark (dark fallback if
  unsupported).  `theme.set_active('auto')` falls back to dark at init, so the
  app always has a valid theme before the probe resolves it.  `/theme auto` and
  `/settings theme auto` persist the preference.  Kill switch:
  `NBCHAT_NO_AUTO_THEME=1`.
- **`_save_cfg` auto-awareness** — a small fix so persisting settings does not
  clobber the `'auto'` preference with the current concrete theme name
  (otherwise auto-detect would silently revert to the last concrete theme).
- **Default behaviour unchanged** — the default theme stays `dark`; `auto` is
  opt-in.  On a real terminal (iTerm2/kitty/wezterm/…) DECSTERA answers in
  <100 ms, so the re-detect is effectively free each start.
- **Tests** — 7 new (probe light/dark/timeout/passthrough via a pty pair fed a
  synthetic report; `_apply_auto_theme` picks light and persists the preference;
  `/theme auto` and `/settings theme auto` persist `'auto'`; the no-arg `/theme`
  lists `auto`).  tui2 suite 304 passed; full suite 678 green (304+79+295).

## tui3: project instructions auto-load (AGENTS.md / CLAUDE.md)

**Project instructions** (prime-agent runner-up "AGENTS.md/CLAUDE.md
auto-load"): standard agentic-harness behaviour (Claude Code, goose, aider
and friends all do it) — repo convention files are loaded into the system
prompt so the agent follows project rules without the user pasting them.

- **Discovery** — `_find_project_instructions(cwd)` looks in the working
  directory first, then the git repo root (`git rev-parse --show-toplevel`),
  for `AGENTS.md`, `agents.md`, `CLAUDE.md`, `claude.md` (in that priority).
  Best-effort, never raises.  `_load_project_instructions(path)` reads the
  file, caps it at 16 KB, and wraps it in a `[PROJECT INSTRUCTIONS — …]`
  marker telling the model to obey it.
- **Wiring** (all in `nbchat/tui2/app.py`, so the v1 REPL is untouched) —
  `ChatApp.__init__` finds the file, stores it as `self._project_instr_path`,
  and appends the marker to `self.system_prompt` (alongside the todo/plan
  nudges).  Opt-out: `NBCHAT_NO_PROJECT_INSTRUCTIONS=1`.
- **`/project`** — shows the loaded file (path, byte/line count, first 10
  lines, "+N more" note) or a hint when none is found.  Additive tui2 command;
  the v1 REPL does not auto-load (kept byte-for-byte unchanged).
- **Default behaviour unchanged** — with no `AGENTS.md`/`CLAUDE.md` present
  nothing is loaded, so existing sessions are byte-identical.  Only repos
  that opt in by adding a convention file gain the behaviour.
- **Tests** — 5 new (discovery: no-file/AGENTS.md/priority/opt-out; system
  prompt contains the marker + content; `/project` shows the file; no-file
  hint; opt-out leaves the system prompt clean).  tui2 suite 309 passed;
  full suite 683 green (309+79+295).  E2E pty verified (boot in a dir with an
  AGENTS.md, `/project` shows the file + content, clean exit).

## tui3: /export html (self-contained HTML conversation export)

**`/export html`** (prime-agent gap: "`/export` HTML / `/share`"): the existing
`/export` writes a clean markdown file; this adds a self-contained HTML page so
a conversation can be shared / archived with formatting intact.

- **`_session_html(sid)`** — mirrors `_session_markdown` over the same
  `db.load_history` rows, but emits a single self-contained HTML document
  (inline `<style>`, no external assets).  Role-coloured message blocks
  (user / assistant / tool), a labelled tool panel, and **all content is
  HTML-escaped** via `html.escape` so `<script>`/`&`/quotes render literally
  (never as markup) — safe for arbitrary model output.
- **`/export [html] [path]`** — the `html`/`htm` first token selects the format;
  everything else is unchanged (markdown remains the default).  Default output
  path extension follows the format (`.html` vs `.md`); an explicit path is
  honoured.  Pure/read-only over the DB + one file write; no I/O in the frame
  path, so no render impact.
- **Default behaviour unchanged** — `/export` with no `html` token writes
  markdown exactly as before.  Only the new opt-in format is added.
- **Tests** — 2 new (full export writes a `.html` file with the DOCTYPE, title,
  escaped content, role/tool classes; `_session_html` escapes a `<script>` tag).
  tui2 suite 311 passed; full suite 685 green (311+79+295).

## tui3: /heartbeat (recurring instruction, prime-agent gap)

**`/heartbeat`** (prime-agent gap: a user-defined recurring instruction that
fires as a turn on a schedule): poll CI, watch a build, nudge a long-running
task, without keeping a terminal hand on the wheel.

- **`/heartbeat every <dur> <instruction>`** — fires `<instruction>` as a turn
  every `<dur>` (a bare number = seconds; `s`/`m`/`h` suffixes supported) while
  the session is idle.  `/heartbeat` shows the current heartbeat; `/heartbeat
  clear` (or `off`/`stop`) stops it.  Bad durations are rejected with a hint.
- **No new thread, no interrupt** — the check lives in `_tick_heartbeat()`,
  called from the frame builder on every render tick (the existing 1 s
  `clock_interval`).  When the interval has elapsed **and** no turn thread is
  alive, it fires via `_start_turn("[heartbeat] " + instruction)`.  If a turn is
  running it defers (the elapsed window is preserved, so it fires as soon as
  idle) — a heartbeat never interrupts a running turn.  The fired turn is
  prefixed `[heartbeat]` so it is distinguishable in history.  No new daemon
  thread, no render impact (the check is a few `time.monotonic()` reads).
- **Default off** — nothing is scheduled until the user sets it; no state
  persisted across restarts (a heartbeat is a live-session convenience).
- **Tests** — 6 new (duration parsing incl. bad/short values; set + status +
  clear; usage on bad args; fires when idle; does NOT fire when the window has
  not elapsed; defers while a turn thread is alive).  tui2 suite 317 passed;
  full suite 691 green (317+79+295).

## tui3: /autonomous (approval-gated auto-continue, prime-agent gap)

**`/autonomous <objective> [--auto]`** (prime-agent runner-up: "/autonomous
gate loop"): the existing `/goal` auto-continues toward an objective but does
so *silently* — the agent keeps chaining turns until the budget runs out or the
model says `GOAL COMPLETE`.  That is powerful but hard to keep in check.  This
adds the **approval gate**: the run still auto-continues, but **pauses after
each turn** and asks you to continue, so you keep a hand on the wheel without
retyping the objective.

- **`/autonomous <objective>`** — start a gated run (default).  **`--auto`**
  starts it silently (identical to `/goal`).  The gate is the differentiator:
  with it on, each turn completes and the app shows a gate note instead of
  chaining.
- **`/autonomous go`** — run the next continuation turn (gate stays on).  **
  `/autonomous auto`** — switch to silent auto-continue + run the next turn
  (now behaves like `/goal`).  **`/autonomous stop`** / **`/autonomous clear`**
  — stop and clear.  **`/autonomous`** — status (state, gate on/off, turns
  `K/N`).
- **Reuses the goal machinery, `/goal` is unchanged** — the gate is a single
  `self._autonomous_gate` flag.  The goal-completion decision (declared-done /
  gate-pause / silent-chain) was extracted from `_finalize_turn` into
  **`_goal_finish(msg_text) -> bool`** so the gate / done / chain logic is
  unit-testable in isolation; `/finalize_turn` now just calls it and sets
  `chained`.  When the gate is on, `_goal_finish` shows the note and returns
  False (no chain); `/autonomous go` / `auto` then call `_goal_next_prompt`
  (which advances the budget) and start the turn.  Budget accounting is
  identical to `/goal`.
- **Default off** — `/goal` keeps silently auto-continuing; `/autonomous` is
  opt-in.  No new threads; the gate is checked in the existing turn-complete
  path (worker thread) and driven by slash commands (UI thread).
- **Tests** — 10 new (start gated / start silent; status on / none; `go` with no
  goal; `go` continues + advances budget; `stop` clears; `_goal_finish` pauses
  when gated (no chain, note shown, budget NOT advanced); `_goal_finish` chains
  when silent; `_goal_finish` stops on `GOAL COMPLETE`).  tui2 suite 327 passed;
  full suite 701 green (327+79+295).

## tui3: /log (debug stderr tail, tui2-only)

**`/log [N]`** — a small, read-only debugging aid.  The TUI2 redirects
stderr (mid-stream retries, logging warnings, noise) to a log file so it never
corrupts the raw-mode screen; before this there was no way to see that log from
inside the TUI.  `/log` (or `/log [N]`, default 30 lines) tails it.

- **`_tui2_log_path()`** (module-level) — the single source of truth for the
  log path: `~/.nbchat/tui2-stderr.log`, overridable with `NBCHAT_TUI2_LOG`
  (used by tests).  `ChatApp.run()` now uses it to open the stderr redirect, so
  the command and the redirect can never point at different files.
- **`/log [N]`** — reads the file, shows the last N lines plus a header with the
  path, byte size, and `tail/total` line count.  Bad/missing args are rejected
  with a usage hint; a missing file is reported helpfully (created on start,
  nothing logged yet).  Pure/read-only — one file read, no I/O in the frame
  path, no render impact.
- **Tests** — 5 new (tail shows last line / truncates first with default 30;
  N-line limit; missing file; bad args; the path helper honors
  `NBCHAT_TUI2_LOG` and falls back to `tui2-stderr.log`).  tui2 suite 332
  passed; full suite 706 green (332+79+295).

## tui3: /team roster (live team-registry view, tui2-only)

**`/team roster`** — a live, read-only view of a team run task-queue.  `nbchat`
already runs multi-agent teams (`/team <goal>` spawns a `TeamCoordinator` that
plans the goal into tasks and fans them out to worker agents), but there was no
way to see the per-task state while a run was in flight - only the final
report.  `/team roster` renders the live `TaskQueue`.

- **`_team_roster()`** (tui2/app.py) - pure/read-only.  Reads
  `self._team_state["coordinator"]` (the live `TeamCoordinator`) and its
  `_pool_queue` (`TaskQueue`) + `_run_id`.  Groups tasks: top-level planner
  tasks first, then the subtasks each one delegated (indented with `+`).
  Each row shows `task_id [status] title`.  A header line shows the run id,
  current status, per-status counts (`claimed:2  done:2`), and total.  Safe
  at any time: no coordinator -> "no team run yet"; no queue (run not at its
  pool, or finished) -> "no live task queue"; queue read guarded against
  exceptions.  Never mutates the run.
- **Wiring** - `/team roster` branch in `_cmd_team` (before `stop`); the
  `_cmd_team` docstring and the `/help` list now document `/team` + the
  `roster` subcommand.  The no-arg `/team` path is unchanged (existing team
  tests still pass).
- **Tests** - 4 new (no run; no queue; a real `TaskQueue` with 2 top-level
  tasks + 2 delegated subtasks shows counts + per-status + parent/child
  indentation; the `/team roster` command path).  Tested with a real
  `Task`/`TaskQueue` from `nbchat.core.team` (no live team run exercised).
  tui2 suite 336 passed; full suite 710 green (336+79+295).

## tui3: /team stats (per-worker breakdown, tui2-only)

**`/team stats`** — a read-only per-worker breakdown for the current/last team
run.  The prime-agent research flagged a "per-subagent `/stats` breakdown" as a
runner-up (S).  Each team worker streams its messages under a distinct
session (`team:<run_id>-<tag>`), so the per-worker view is a DB read over those
sessions.  Complements `/team roster` (per-task status): together they give a
full team-observability picture.

- **`db.session_message_counts(prefix)`** (core/db.py, additive) — returns
  `{session_id: int}` message counts for every `chat_log` session under *prefix*
  (LIKE `prefix + "%"`, matching a family such as `team:<run_id>-*`).  Read-only;
  best-effort (returns `{}` on error).  Pure addition - no existing DB function
  touched.
- **`_team_stats()`** (tui2/app.py) - pure/read-only.  Reads the run id from
  `_team_state["coordinator"]._run_id`, lists the run worker sessions
  (`team:<run_id>-*`) via `db.list_sessions_with_title`, counts their messages
  via the new helper, and (best-effort) folds in the task-log metrics
  (`num_llm_calls` / `tool_calls_total`) per worker.  Renders one line per worker
  (tag, msg count, llm/tools when present) plus a total line.  No run -> "no team
  run yet"; no worker sessions -> a helpful "not streamed yet" note.  Other runs
  are excluded (prefix match).  DB reads only, no render impact.
- **Wiring** - `/team stats` branch in `_cmd_team`; the `_cmd_team` docstring and
  `/help` list now show the `stats` subcommand.
- **Tests** - 2 new (no run; a temp DB with two runX worker sessions + a
  different-run session shows per-worker counts, the 3+5=8 total, and excludes the
  other run; the `/team stats` command path).  tui2 suite 338 passed; full suite
  712 green (338+79+295).

## tui3: terminal window title (tui2-only)

**Window title** — the herdr research listed a "window-title template" as a
lower-value/fit item to fold into the config feature.  This ships it as a
trivial, safe polish: the terminal window/tab title reflects the current
session and working state, so an active agent is visible at a glance in a
tabbed terminal.

- **`_refresh_window_title()`** (tui2/app.py) - pure/side-channel.  Computes the
  title (`nbchat <session-short>` plus ` [working]` while the turn thread is
  alive) and writes a single OSC 2 escape (`\033]2;<title>\007`) straight to the
  terminal via `term._write`.  It is never part of the diffed frame, so it cannot
  disturb the differential renderer.  No-op in passthrough (non-TTY / test) mode
  and when `NBCHAT_TUI3_WINDOW_TITLE=0`.  "working" is derived from the turn
  thread liveness, so it is always accurate at refresh time.
- **Wiring** - called at startup (right after raw mode is entered, before the
  render loop), on session change (`_session_changed`), at turn start
  (`_turn_worker`), and at turn end (`_finalize_turn`).  The title therefore
  tracks the session and flips to `[working]` while a turn runs.
- **Tests** - 4 new (passthrough no-op; a fake capturing term gets the exact OSC 2
  sequence with the session id and no `[working]`; a live turn thread yields
  `[working]`; the `NBCHAT_TUI3_WINDOW_TITLE=0` kill switch disables the write).
  Plus an E2E pty probe confirming the startup title (`\033]2;nbchat <sid>\007`)
  reaches a real terminal.  tui2 suite 342 passed; full suite 716 green
  (342+79+295).

## tui3: status-line clock (tui2-only)

**Status-line clock** — the herdr research listed "status-line dynamic segments
like clock/command output" as a lower-value/fit item to fold into the config
feature.  This ships the clock as a trivial, safe, opt-in segment: the status
bar re-renders every render tick, so a `time.strftime("%H:%M:%S")` segment
updates live at zero cost.

- **`_status_right()`** (tui2/app.py) - when `NBCHAT_TUI3_CLOCK=1`, appends the
  current time to the right-hand status segments (after ctx / tok/s / goal /
  todo / queue pills).  Guarded so the default (env var unset) status layout is
  byte-for-byte unchanged; the `try/except` means a clock failure can never break
  the bar.
- **Tests** - 3 new (off by default - no `HH:MM:SS` pattern; on - the pattern is
  present; on - other segments like `tok/s` still render alongside it).  tui2
  suite 345 passed; full suite 719 green (345+79+295).

## tui3: bare /sessions opens the session picker (tui2-only)

**Bare `/sessions` = picker** — a consistency polish.  In tui2, no-arg
`/load` already opened the fuzzy session picker (Ctrl+L), but no-arg
`/sessions` fell through to the v1 text list.  Now bare `/sessions` opens the
same picker (consistent with `/load` and Ctrl+L); `/sessions <id>` still
falls through to v1 (list by id), so nothing is removed.

- **`_run_command`** (tui2/app.py) - a no-arg `/sessions` branch added right
  after the existing no-arg `/load` branch: both call `self._open_picker()` and
  return.  The tui2 `/help` Ctrl+L line and the Ctrl+P palette label for
  `/sessions` were updated to reflect the picker.
- **Tests** - 2 new (bare `/sessions` opens the session modal picker; the picker
  it opens is identical to the one `/load` opens).  tui2 suite 347 passed; full
  suite 721 green (347+79+295).

## tui3: /msg - note for the running team (tui2-only, additive core)

**`/msg <text>`** — the prime-agent research flagged "/msg Wn worker
steering" as a runner-up.  The nbchat team workers are generic claimers (not
task-bound, individually-addressable agents), so *mid-task* steering is not a
good fit (and is E2E-unsafe).  This ships the genuinely-useful, safe slice:
a note to the team that the coordinator weighs in its **final synthesis**.

- **core/team.py** (additive) - `TeamCoordinator` gains `_user_notes` (append-only,
  lock-protected, capped at 50) plus three methods:
  - `add_note(text)` - thread-safe append (strips; ignores empty; 400-char cap each).
  - `notes()` - a copy of the notes so far.
  - `_notes_block()` - the report section carrying the notes ("" if none).
  `TeamCoordinator.run()` appends `_notes_block()` to the synthesis report before the
  coordinator LLM call, so the notes are weighed in the final report.  Additive:
  no change when no notes are added; the notes are read at synthesis time only and
  never affect worker execution.  Existing team tests still pass (54 green).
- **tui2/app.py** - `_cmd_msg(arg)`: no team run -> "no team run"; no arg -> lists the
  notes (or a usage hint); with a text -> `add_note` + confirmation.  `/msg` added to
  the native set, the handler dict, and `/help`.
- **Tests** - 7 new (core: add_note/notes/strip/empty/cap; tui2: no run; add + list;
  usage when empty; dispatch).  tui2 suite 354 passed; full suite 728 green
  (354+79+295); core team suite 54 green (no regression).

## tui3: window title prefers the session title (polish)

**Window title now shows the session title** — a small polish on the window
title feature.  _refresh_window_title() now reads the session title via
db.load_session_title(sid) and prefers it over the raw session id (falling
back to the short id when no title is set, or if the DB read fails).  So a
titled session shows `nbchat <title>` in the tab; an untitled one shows
`nbchat <short-id>`.  Best-effort (a DB failure just means the id is shown).
+2 tests (title shown when set; fallback to id when unset). tui2 suite 356
passed; full suite 730 green (356+79+295).

## tui3: leader key / prefix command mode (herdr #5 completion)

**Leader key** — the last piece of herdr #5 (prefix key mode + contextual
mode bar + generated keybind help). The mode bar and generated KEYMAP were
already in place; this adds the opencode-style **leader key** (Ctrl+X) for
one-character command shortcuts.

- tui2/app.py:
  - `self._leader` flag (init).
  - `KEYMAP["leader"]` - the leader-mode key hints (l load, p palette, e editor,
    o browse, t thinking, r history search, q quit, esc cancel).
  - Ctrl+X added to the normal-mode KEYMAP (discoverable in the mode bar).
  - `_on_input`: while `_leader` is active, the next key is captured by
    `_leader_key` (never reaches the editor); Ctrl+X enters leader mode.
  - `_leader_key(key)` - maps the one-char shortcut to the command, exits
    leader mode after one key; Esc/Ctrl+X cancel without action.
  - `_mode_bar` - shows the leader key hints while active (reuses the existing
    KEYMAP-generated bar; no new row, so the frame height invariant holds).
- Safe: purely additive; the leader key is a Ctrl key (no typing conflict); the
  mode bar row already exists; the next key is always consumed (never typed to
  the editor). 6 tests (enter, p->palette, l->picker, esc cancels, key not
  typed to editor, mode bar shows leader). tui2 suite 362 passed; full suite
  736 green (362+79+295).

## tui3: graceful terminal disconnect (safe slice of herdr #10 detach)

**Graceful detach** - the safe, scoped slice of herdr #10 (detachable
background agent). The full detach (UI -> detach -> reattach to a live
process) is a major architectural bet; this ships the genuinely useful,
low-risk half: a terminal disconnect (SSH drop / terminal closed -> SIGHUP)
during a running turn no longer loses the work.

- tui2/app.py:
  - `self._sighup` flag (init).
  - `run()` installs a SIGHUP handler (best-effort, save/restore the old
    handler) that sets `self._sighup = True`.  Additive; a signal-handler
    problem can never take the TUI down.
  - `_graceful_detach(timeout=180.0)` - on the exit path, if the SIGHUP
    flag is set and a turn thread is still alive, join it (bounded) so the
    in-flight turn's result is persisted to the session before the process
    exits.  A no-op otherwise (normal quit, or no in-flight turn).
  - The `finally` block calls `_graceful_detach()` before the existing
    cleanup.  Bounded (180 s) so a stuck agent cannot hang the process.
- Safe: purely additive; the join is guarded by the SIGHUP flag, so the
  normal exit path (user quit) is unchanged; `NBCHAT_NO_GRACEFUL_DETACH=1
  kill switch; the handler installation is try/except (a failure just means
  the default SIGHUP behaviour).  The existing `--bg` mode already provides
  the "keep the agent alive headless" half (driven via the control socket);
  this adds the "dont lose the in-flight turn on a live-terminal disconnect
  " half. 5 tests (no-op when no SIGHUP; joins a live turn; no-op when no
  turn; kill switch; handler sets flag). tui2 suite 367 passed; full suite
  741 green (367+79+295).

## tui3: control-socket `log` command (reattach read path, herdr #10)

**`log` command** - the read path of the "reattach" half of herdr #10 (a
safe, scoped slice). The `--bg` mode already keeps the agent alive headless
(driven via the control socket); this adds a way to READ the running
agent's conversation, so a client can watch a background agent's output
without a second terminal.

- tui2/ctl.py:
  - `ControlServer.__init__` gains an optional `log_fn` (read-only).
  - `_process` handles `log [N]` (N = max messages; bad/empty N -> all).
  - CLI usage updated; `nbchat-ctl log [N]` works via the generic path.
- tui2/app.py:
  - `_log(limit)` closure in `_start_control` - reads the session
    conversation from the DB (capped), returns `{"session", "messages"}`.
  - Wired as `log_fn` on the `ControlServer`.
- Safe: purely additive, read-only (a DB read); never touches the UI thread;
  an absent `log_fn` -> `{"ok": false, "error": "log unsupported"}` (old
  behaviour for servers built without it). 3 tests (command dispatch +
  limit parsing; unsupported when no log_fn; the cap logic). tui2 suite 370
  passed; full suite 744 green (370+79+295).

## tui3: --attach reattach view (herdr #10)

**`--attach <socket>`** - the live "reattach" view of the background-agent
workflow (a safe, scoped slice of herdr #10). It tails a running (typically
`--bg`) TUI conversation in the foreground, printing new messages as they
appear (like `tail -f`). A pure read path (read-only; a socket error just
ends the tail).

- tui2/attach.py (new): `run(argv)` - connects to the control socket,
  issues the `log` command on a poll cadence (`--interval <s>`, default 1.0 s),
  and prints the new/changed tail of the conversation. Ctrl+C detaches (rc 0);
  a socket error ends the tail (rc 1).
- tui2/__main__.py: `--attach` / `-a` dispatches to `attach.run` (before the
  interactive app; `--v2` is stripped).
- Safe: a brand-new entry point; the interactive TUI is untouched; read-only
  (only the `log` command); a connection failure just prints a message and
  exits 1. Verified E2E (a fake ControlServer with a log_fn -> the attach
  tail printed the conversation). 3 tests (role prefix; connection error; main
  dispatch). tui2 suite 373 passed; full suite 747 green (373+79+295).

## tui3: frame command + --attach --frame (full reattach view, herdr #10)

**`frame` command + `--attach --frame`** - the full "reattach" view of the
background-agent workflow (a safe, scoped slice of herdr #10). The `frame`
command returns the current rendered frame as plain text (one line per frame
row); `--attach --frame` mirrors that frame live (clears and redraws each
poll). Together with the `log`/`--attach` tail, `send`, and graceful
disconnect, the background-agent workflow now has: launch + tail + full-frame
mirror + read + send + safety.

- nbchat/tui2/ctl.py: ControlServer.__init__ gains an optional frame_fn
  (read-only); _process handles "frame" (returns {"lines", "width", "height"};
  a frame_fn returning {"ok": False, ...} propagates the error); CLI usage
  updated.
- nbchat/tui2/app.py: _frame() closure in _start_control calls _build_frame()
  and returns the plain-text lines (one per frame row); wired as frame_fn.
- nbchat/tui2/attach.py: --frame flag - the initial probe and the poll loop
  use the frame command (instead of log); the loop clears the screen (CSI
  2J + home) and redraws the frame each poll; Ctrl+C leaves the alt-screen/
  styling (rc 0).
- Safe: purely additive, read-only (a _build_frame() call + a render); never
  touches the UI thread or mutates the TUI; a socket error just ends the
  mirror. Verified E2E (a fake ControlServer with a frame_fn -> the attach
  --frame view rendered the frame lines). 3 tests (command dispatch; unsupported
  when no frame_fn; error propagation). tui2 suite 376 passed; full suite 750
  green (376+79+295).

## tui3: version delineation + Phase 1 /trace (observability)

**The tui3 version boundary + `/trace`** - the new `nbchat.tui3` module is the
NEXT version after tui2. It subclasses the tui2 `ChatApp` (inheriting the full
tui2 feature set) and adds the tui3 feature wave. The version-to-feature-set
boundary is explicit: every tui3 feature lives in `nbchat/tui3`, and the tui2
base is left untouched. Entry points: `python -m nbchat.tui3` or
`python -m nbchat.tui --v3` (v3 takes precedence over v2).

**Phase 1 - `/trace` (live task-trace / observability view)** - a structured
view of the recent agent activity for the current session (user turns,
assistant turns, tool calls, and errors). `/trace` shows the last 40 steps + a
summary (N you / N nbchat / N tools / N err); `/trace N` shows the last N;
`/trace errors` shows only the errored steps; `/trace tools` shows only the
tool calls. Reads the EXISTING conversation history via `db.load_history`
(no new data plumbing). The error flag is only meaningful for tool rows
(structured `is_tool_error`), so `[ERR]` and the error count apply to tool
rows only. Inspired by CrewAI tracing & observability + LangGraph durable
execution.

- nbchat/tui3/__init__.py: documents the tui2/tui3 boundary, exports VERSION.
- nbchat/tui3/app.py: ChatApp subclasses the tui2 ChatApp; _TUI3_NATIVE
  ("/trace", "/budget"); _run_command intercepts tui3-native commands BEFORE
  the tui2 dispatch; _run_tui3_command dispatches to the handlers; _cmd_trace
  builds the trace view.
- nbchat/tui3/__main__.py: entry point for `python -m nbchat.tui3`.
- nbchat/tui2/app.py: run() gains an OPTIONAL chat_app_cls param (defaults to
  the tui2 ChatApp - zero behavior change) so tui3 reuses the tui2 entry
  plumbing.
- nbchat/tui/app.py: a --v3 flag dispatches to nbchat.tui3.run (precedence
  over --v2).
- tests/test_tui3.py: 10 tests (version, subclass, native-command delineation,
  trace empty/basic/errors-filter/tools-filter/limit, dispatch interception,
  pass-through).
- Safe: purely additive; the tui2 base and the v1 print REPL are unchanged.
  Verified E2E (a pty boot of `python -m nbchat.tui3 --new` renders the frame;
  `/trace` on a fresh session shows "no history"). tui3 suite 10 passed; tui2
  suite 376 passed; v1 suite 79 passed (no regression).

### Phase 2 - approval diff-preview (HITL upgrade) - DONE

The tool-approval modal now shows the tool's registry description + a short
preview of the intended change (the key detail a reviewer needs - the file
path, the command, or the URL), instead of just the tool name + a raw arg
blob. The tui2 approval gate is unchanged; this is a tui3-only rendering
upgrade (the tui3 `ChatApp` overrides `_approval_lines`). The preview parses
the tool args as JSON and surfaces the most relevant key (path/file/command/
url); it falls back to a truncated raw arg string when no known key is
present. Inspired by Google ADK tool confirmation + LangGraph
human-in-the-loop.

- nbchat/tui3/app.py: `_tool_description(tool)` (registry lookup),
  `_tool_preview(tool, args)` (JSON key extraction), and the `_approval_lines`
  override (tool + description + preview). 9 new tests.
- Safe: purely additive; the tui2 approval gate is unchanged. tui3 suite 19
  passed; tui2 376 + v1 79 (no regression).

### Phase 3 - /budget (cost / token tracking) - DONE

`/budget` shows the ACTUAL LLM token usage (across completions, from a new
main-agent token meter), the per-session conversation summary, and an
estimate of the current conversation's token footprint (chars / 4).
`/budget reset` zeroes the meter. The token meter is per TUI instance (a
close proxy for the current session).

- nbchat/core/team_metrics.py: added a process-global `_MAIN_METER`
  (separate from the /team run meter) + `main_token_stats()` +
  `reset_main_tokens()`. `record_tokens` now reports to BOTH the main-agent
  meter (always) and the active team meter (when a /team run is recording).
  This is the RIGHT home for token tracking (the client already reports
  `usage.total_tokens` on every LLM completion). 5 new tests in
  tests/test_team_metrics_main.py.
- nbchat/tui3/app.py: the `_cmd_budget` command (actual tokens + per-session
  summary + estimate). 3 new tests.
- Safe: the team run meter is unchanged (the main meter is additive). tui3
  suite 22 passed; team_metrics main 5 passed; tui2 376 + v1 79 + core team 54
  (no regression).

### Phase 4 (safe slice) - /reflect (deep-agent plan loop, reflect step) - DONE

`/reflect` asks the LLM (on an isolated throwaway agent, kept out of the
session history via a `refl:`-prefixed side question) to reflect on the recent
conversation: what has been accomplished, what remains, and the single next
concrete step. It runs in a background thread (never blocks the UI); the
reflection is appended to the log when it arrives. This is the SAFE slice of a
plan-act-reflect loop (the "reflect" step). Inspired by LangGraph deep agents.

- nbchat/tui3/app.py: the `_cmd_reflect` handler (checks busy, starts the
  thread) + the `_reflect_worker` (builds a concise transcript of the recent
  conversation, makes the LLM call via the existing `_send_side_question`
  mechanism, appends the reflection). 4 new tests.
- Safe: reuses the existing `_send_side_question` mechanism (no new LLM
  plumbing); the side question is kept out of the session history. tui3 suite
  26 passed; tui2 376 + v1 79 (no regression).

### Next phases (tracked)

- **Phase 4 (full) - deep-agent plan loop**: the full plan-act-reflect loop
  (autonomous multi-step execution with re-planning). A larger, more invasive
  change (it modifies the agent turn logic).
