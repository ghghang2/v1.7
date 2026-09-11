# tui2 — issues found and fixes applied

Status of the `nbchat/tui2` implementation found during the 2026-07-10 review,
verified against a real pty session (live model at the configured endpoint),
plus the fixes applied to make `tui2` a working upgrade of the v1 REPL.

Method: the v1 REPL (`python -m nbchat.tui`) was run in a pty against the live
endpoint first to prove the backend works end-to-end (it did: thinking, a
`run_command(echo PING)` tool call, final reply `PING`). The tui2 app was then
run in the same pty with the same prompt, and its render/event loop, key
handling and agent-hook plumbing were exercised directly (unit-level) to
isolate the failures.

---

## Critical (conversation unusable)

### C1. The assistant's final reply is never shown when a turn had thinking or tool calls
`ChatMessage.render` (tui2/chat.py) renders *either* `blocks` *or* `text`:

    if self.blocks:      render blocks
    elif self.text:      render markdown text

`ChatApp._finalize_turn` sets **both** (the streamed content in `text`, the
thinking/tool panels in `blocks`), so on any turn containing thinking or a
tool call the answer text is silently dropped. Verified in a pty run: after
"Say exactly: PING" the screen showed the thinking blocks and the tool panel
but the reply `PING` was nowhere to be found.

**Fix:** render `blocks` first, then the `text` below them (always, when
non-empty).

### C2. Every streamed thinking token inserts a *new* full thinking block
`ChatApp._on_stream_reasoning` inserted a new `ChatBlock` carrying the whole
accumulated reasoning on **every token** (and at index 0, i.e. newest-first).
A ~12-token thinking phase produced ~12 duplicated, ever-longer blocks, and
each new LLM call in a tool loop added its own set. Verified in pty: 4+
duplicated "· thinking" blocks for one short thinking phase.

**Fix:** one thinking block per LLM call, updated in place. A block is opened
on the first reasoning token, its text is replaced as more arrives, and it is
closed when content starts, a tool runs, or the stream completes — the next
call opens a fresh block in chronological order.

### C3. Live streaming is not rendered at all during a turn
`ChatApp._build_frame` composes only `self.log` + rule + editor + status.
The live turn state (`_stream_text`, `_stream_blocks`) and the (constructed
but unused) `Loader` spinner are never put into the frame, so during a turn
—which can take 10–30 s—the screen is frozen and only the status line
changes ("turn 1" / "thinking"). This is the biggest UX gap versus v1, which
streams tokens live.

**Fix:** the frame builder now appends the live turn (thinking blocks, tool
panels, streamed answer text) below the log, and the status line shows the
spinner glyph while busy. A ~1 Hz clock tick in the event loop keeps the
spinner advancing between events.

### C4. Space is a dead key — user messages lose every space
`KeyReader` emits `Key("space")` for the space bar, but
`app._key_text` returns `""` for any named key that isn't a single printable
character, so the editor never receives a space. A pty run of "Say exactly:
PING" rendered as `Sayexactly:PING`. This made the TUI effectively unusable
for real prompts.

**Fix:** `_key_text` maps `Key("space")` → `" "`.

### C5. Ctrl+C / Ctrl+D kill the app even mid-turn, contradicting the documented keys
`tui2/raw.py` `TUIApp._handle_input` treats an exact `\x03`/`\x04` chunk as
an unconditional app exit — before the key ever reaches the editor/app.
Consequences:

* Ctrl+C during streaming (the universal "stop" expectation, and v1's
  semantics) quits the whole UI; the documented "Esc interrupts" was the
  only stop key.
* The editor keymap and `app.py` docstring both document "Ctrl+D with text:
  submit", but in a real terminal a lone Ctrl+D always quit (the unit tests
  bypass the loop by calling `_on_input` directly, so they passed).

**Fix:** `TUIApp._handle_input` may now return `None` meaning "consumed,
keep running" (True = exit, False = dispatch to keys — the old contract is
unchanged for the Phase 1 demo). `ChatApp` overrides it: Ctrl+C interrupts
the in-flight turn when busy, quits when idle; Ctrl+D submits when the
editor has text, quits when empty — same on both the raw-byte path and the
key path.

### C6. Every CSI key (arrows, PgUp/PgDn, Home/End, F-keys, paste) mis-parsed
`tui2/keys.py` `KeyReader._take_one` found the CSI *final byte* by scanning
`for i in range(1, len(b))` for the first byte in `0x40-0x7E`. But the very
first byte checked is the `[` introducer itself (`0x5B`), which lies inside
that range — so every `ESC[...` sequence was "terminated" immediately after
the `[`, eaten as an *unknown* key, and the remainder leaked into the editor
as literal text. Result: **no** arrow key, PgUp/PgDn, Home/End, F1–F4, or
bracketed-paste sequence ever parsed; `ESC[A` produced `Key("unknown",
"ESC[")` + a stray `A`. The basic E2E (typing + Enter) never exercised a CSI
key, so it slipped through.

**Fix:** the terminator scan now starts *past* the `[` introducer
(`range(2, len(b))`) and the matchable body includes the final byte
(`b[2:end+1]`). Params (`0x30-0x3F`) and intermediates (`0x20-0x2F`) can
never be mistaken for the final byte, so the first `0x40-0x7E` byte after
the `[` is unambiguous. All CSI keys now parse, and a sequence split across
read chunks is held until the final byte arrives. Covered by six new
KeyReader regression tests.

---

## Major (functional gaps vs. the v1 REPL)

### M1. No session continuity
v1 resumes the last session by default (`TerminalAgent.last_session`),
supports `--new` / `--session <id>`, and calls `remember_session` after every
turn. `ChatApp` always minted a fresh session, never remembered one, and
`python -m nbchat.tui2` ignored `--new` / `--session` silently.

**Fix:** `ChatApp(term, events, resume_last=True, session_id=None)` resumes
the last (or a resolved) session, re-renders its history rows into the log,
and remembers the session after each turn. `__main__` parses `--new` and
`--session` (with `resolve_session` + error message on no match).

### M2. Slash commands don't exist in tui2
v1 handles `/quit /exit /new /sessions /title /status /load /history
/effort ...` — always, even mid-stream. In tui2 a line starting with `/` was
submitted to the LLM as a chat message (`/quit` was sent to the model).

**Fix:** `_on_input` routes `/...` lines to `nbchat.tui.app.handle_command`
with `sys.stdout` captured into a buffer and the output rendered as a dim
"system" message in the log (the command code prints; capturing keeps the
raw-mode screen intact and the command logic untouched). `/quit` exits;
`/new` / `/load` switch sessions and reset the log/stream state, re-rendering
the new session's history.

### M3. Mid-stream interjection is dropped
v1: typing a new message while a turn is running interrupts the turn and
starts a fresh one with the new text ("redirecting — stopping current
response"). tui2 `_start_turn` counted `_queued` and threw the text away.

**Fix:** Enter while busy now interrupts the running turn and stores the
text as a pending redirect; when the interrupted turn finalises, the worker
immediately starts the redirect turn. Latest message wins if the user types
again while one is pending.

### M4. No header — model and session id are invisible
The frame had no header row at all (the docstring claims one): the model in
use and the session id were nowhere on screen.

**Fix:** a thin header row: `nbchat · <model> · session <id-short>`.

### M5. Context budget and throughput are discarded
`TerminalAgent._status_window(used, budget)` and the status-bar tok/s
accounting are print-based; in tui2 `_status()` returns `None` so both were
no-ops and the status line's right half was permanently empty.

**Fix:** `ChatApp` overrides `_status_window` to store the numbers and
computes a rolling 1 s tok/s in `_on_stream_token`; the status right side
shows `<model> · ctx ▰▰▱▱ 12% · 18.2 tok/s` (bar from the existing
`status._ctx_bar`).

### M6. `--v2` launches the demo, not the app
The v1 entry point's `--v2` flag ("use the new TUI v2 engine") launched the
Phase 1 rendering demo; the real app was only reachable by running
`python -m nbchat.tui2` directly — a contradiction of the documented UX.

**Fix:** `nbchat.tui.app.run(--v2)` now launches the real tui2 app;
`python -m nbchat.tui2 --demo` still runs the demo.

### M7. stderr corruption in raw mode
The conversation loop's `logging` warnings (e.g. mid-stream retry messages)
fall through Python's lastResort handler to **stderr**, which in raw mode
writes straight onto the alternate screen and corrupts the frame.

**Fix:** `ChatApp.run()` redirects `sys.stderr` to
`~/.nbchat/tui2-stderr.log` for the duration of the session (restored on
exit); errors still surface in the status line via the hooks.

---

## Minor

* **m1. Placeholder never visible.** `LineEditor.render` draws the cursor
  line first, so the empty-buffer placeholder (which only rendered on the
  `elif` branch) was never shown. Fix: when the buffer is empty the cursor
  block overdraws the first placeholder character.
* **m2. Tool panels show raw single-line JSON.** `_on_tool_display` passed
  `body=[preview]` (one 300-char line, clamped, no line structure). Fix: the
  result is split into real lines and capped (8 lines + "… N more"), matching
  v1's readable tool output.
* **m3. `st._enabled` monkeypatched globally.** `ChatApp.__init__` replaced
  `nbchat.tui.status._enabled` process-wide. The status-bar singleton never
  prints on its own (the v1 ticker thread is what renders it, and tui2 never
  starts one), so the patch is dropped.
* **m4. `ChatLog.scroll` re-rendered the whole history at width 1** per call
  (O(history) markdown re-parse on a throwaway cache key). Not user-visible
  yet (no scroll keys wired), left for the scrollback pass.
* **m5. `queued: N` status could never clear** (no consumer). Replaced by the
  redirect mechanism (M3).
* **m6. `Turn worker swallows exceptions silently`** in the normal (no-UI)
  path — now also surfaced as an error block + status.

## Features added in this pass (2026-07-11)

A second wave of prime-agent / herdr-inspired features, all additive and
built on the existing engine (no new subsystems; v1 REPL untouched).

**tui2-native slash commands** (handled in `ChatApp._run_command` before v1
delegation, so v1's `handle_command` is never reached for these):

* `/context` — model, session, context bar, tool-output compression, turns.
* `/hotkeys` — the keybinding reference (generated, so it can't desync).
* `/copy` — last assistant message → clipboard (OSC 52; no-op elsewhere).
* `/compact [focus]` — a manual one-shot compaction via the new
  `ContextMixin.force_compact()`, with a before/after report. The per-turn
  auto-windowing is left untouched.
* `/refine [instructions]` / `/refine rollback` — schedule a manual
  refinement round (`refine_hook.schedule_manual_refine`) / revert the last.
* `/lessons` — applied refinement lessons.
* `/memory` — L1 core memory + L2 episodic stats.
* `/btw <question>` — a throwaway side question on an isolated agent
  (separate `btw:` session) so the current history is not touched.
* `/name <title>` — alias for v1's `/title`.

**Session picker modal** (herdr's navigator overlay): bare `/load` or
`Ctrl+L` opens a fuzzy-filterable `SelectList` over the `tui:` sessions
(type to filter, `↑/↓` move, `Enter` loads, `Esc`/`Ctrl+C` cancel). Reuses
the existing `SelectList` + `fuzzy_rank` and the v1 `_switch_session` path.

**Thinking toggle** (`Ctrl+T`): shows/hides reasoning blocks in the live
turn *and* committed turns, losslessly (full blocks are kept and restored).

**Scrollback** (herdr's copy-mode precursor): `PgUp`/`PgDn` page the log,
`Home`/`End` jump to top/bottom, a `↑N` indicator shows how far up you are,
and new content snaps back to the bottom. Built on `ChatLog.offset` — and
only works because of the C6 KeyReader fix (PgUp/PgDn are CSI keys).

**Shell prefixes** (`!cmd` / `!!cmd`): a line starting with `!` runs a local
shell command on a worker thread (so the UI never blocks) and renders the
output as a bordered tool panel with the exit code in the title; `!!` also
stores the combined output on the app. `ChatApp._run_shell` /
`_shell_worker`.

**Command palette** (`Ctrl+P`, herdr's command palette): `ChatApp
._open_palette` opens the shared picker modal (kind `"palette"`) over a
curated list of every command; typing fuzzy-filters, `Enter` inserts the
chosen command into the editor for confirmation. Reuses the same
`SelectList` + `fuzzy_rank` + modal machinery as the session picker.

**Reverse search** (`Ctrl+R`, herdr's search): `ChatApp._open_search` opens
the picker (kind `"search"`) over the input history (`ChatApp._history`,
recorded on every submit, newest first); `Enter` re-enters a line for
editing. The `LineEditor` previously used `Ctrl+R` for redo; the app now
intercepts it first, so `Ctrl+R` is reverse search (a deliberate trade).

The picker modal was generalized with a `_modal_kind` field
(`"session"` / `"palette"` / `"search"`) so all three share one
`SelectList` slot, one fuzzy filter, and one key handler; `_picker_select`
dispatches on the kind.

**`ContextMixin.force_compact()`** (`core/context_manager.py`): recomputes
the token-budget window, persists the summary cache, logs a `FORCE_COMPACT`
context event, and returns a before/after report (rows, estimated tokens,
budget). **`refine_hook.schedule_manual_refine()`** (`core/refine_hook.py`):
bypasses the automatic predicate, still honors the `REFINE_HOOK_ENABLED`
kill switch, runs a background round, and reports via `_on_agent_message`.

**Tool-approval gate** (`/approve`, herdr's interactive tool approval):
`ChatApp._install_approval_gate` wraps the module-level
`tool_executor.run_tool` — the exact seam `team.py`'s `ToolArbiter` uses —
so risky tool calls (`run_command`, `push_to_github`, `send_email` by
default) show a confirm modal before executing. The worker thread parks on a
`threading.Event` while the UI renders the prompt; `y`/`Enter` approve,
`n`/`Esc`/`Ctrl+C` decline (a declined tool gets an actionable
"DECLINED" result so the model stops retrying). A 5-minute safety timeout
auto-declines so a parked turn can never wedge. The gate is installed in
`run()` and removed (restoring the true original) on exit; `/approve
on|off|add <t>|rm <t>|list` configures it at runtime.

**`/goal`** (prime-agent's flagship): a running objective the app keeps
auto-continuing toward. `/goal <objective>` sets the goal and starts the
first turn; after each turn `_finalize_turn` chains the next turn (same
re-entrancy as the mid-stream redirect) until the turn budget is exhausted,
the model replies with a completion phrase (`GOAL COMPLETE`, configurable
via `ChatApp._goal_done_markers`), or the user runs `/goal stop`.
`/goal` shows status, `/goal clear` clears it, `/goal budget <n>` sets the
default auto-turn budget (20). A `goal K/N` pill tracks progress on the
status line.

**Notification stack** (`/notify`, herdr's attention system): a new
`nbchat/tui2/notify.py` `NotifyStack` holds a bounded queue of transient
*toasts*. Each toast renders as a small bordered card (coloured by kind:
ok/warn/error) just above the input box for ~5 s; auto-dismiss is
timestamp-based and checked in the per-frame pass (no timers or threads).
Side channels are best-effort and dependency-free: a terminal `BEL`
(`\a`) always works, and a `.wav` plays on a daemon thread only when
`NBCHAT_SOUND_DIR` is set and `aplay`/`afplay` exists (kill switch
`NBCHAT_NO_SOUND=1`). Events wired: **turn-complete** (a quiet "done"
card — no BEL/sound, matching herdr's suppression of the focused pane),
**approval pending** (a warn card + forced BEL), and **shell failure**
(an error card + forced BEL). `/notify toasts|bel|sound on|off` toggles
the channels; `/notify test [kind]` fires a sample. `/help` now appends a
"TUI v2 extras" addendum listing the tui2-native commands, and `/hotkeys`
covers the new bindings.

**tui3 wave 1 — keymap substrate + browse mode + mode bar:** a single
data-driven `KEYMAP` (module-level in `app.py`) is the source of truth for
both the `/hotkeys` reference and the new **mode bar** — one line above the
status line showing the active mode and its key hints — so the two can
never desync. **Browse mode** is toggled with `Ctrl+O` (a free key;
`Ctrl+B` is the editor's backward): while active it captures keys so the
log can be read without typing — `j`/`k` step down/up one line,
`PgUp`/`PgDn` page, `Home`/`End` jump to the top/bottom, and `Esc` (or
`Ctrl+O`) leaves the mode and snaps the offset back to the bottom. The
frame budget accounts for the extra mode-bar line (the log region shrinks
by one). The remaining tui3 scope (in-log `/` search, `v` visual copy,
mouse, theming, user config, JSON socket API, detachable agent) is scoped
in `docs/tui3_roadmap.md`.

**tui3 wave 2 — in-log search + visual copy** (inside browse mode, purely
additive, self-contained in `app.py`): `/` opens a query prompt rendered in
the mode bar (type a case-insensitive substring, Enter to run; consecutive
matching lines group into one match; the mode bar shows the live query then
`match i/N`). `n`/`N` cycle to the next/previous match and the log jumps to
bring it to the viewport bottom (offset clamped to content height); `Esc`
clears the search, and `n` with no matches starts a fresh query. `v` copies
the currently **visible** log viewport (rendered lines at the current
offset) to the clipboard via the same OSC 52 escape `/copy` uses, and pushes
a small "copied" toast. The `KEYMAP["browse"]` rows for `/` and `v` drive
both the mode-bar hints and `/hotkeys`.

**tui3 wave 3 — mouse wheel scrolling** (SGR-extended mouse reporting,
purely additive): `RawTerminal.enter()` now enables SGR mouse reporting
(`ESC[?1000h` button tracking + `ESC[?1006h` SGR encoding) after the alt
screen / bracketed-paste setup, and `restore()` disables it (so no mouse
bytes leak into the shell); opt out with `NBCHAT_NO_MOUSE=1`. `KeyReader`
parses SGR mouse reports (`ESC[<btn;col;rowM/m`) into `wheel-up` /
`wheel-down` / `mouse-press` / `mouse-release` keys (buttons 64/66 = up,
65/67 = down); unrecognised `<`-CSI still falls through to the `unknown`
swallow. `ChatApp._on_input` maps wheel-up/down to a 3-line `_scroll_log`
in any mode (normal or browse); button press/release are consumed for now
(click-to-select is a later wave). The existing `↑N` indicator shows how far
the log is scrolled from the bottom.

**tui3 wave 3b — click / drag-to-copy over the log:** a mouse **press** in
the log/live region records the anchor frame row; a **release** copies the
line range `[anchor .. release]` (a single click = one line) to the
clipboard with a small "copied" toast.  The text is read from the last
rendered frame (`_last_frame_rows`, populated in `_build_frame`) so no
log-index math is needed and it always matches what the user saw.  Presses
outside the log region (header / editor / status, rows beyond `_last_log_end`)
are ignored, and a wheel scroll drops any pending anchor. `KEYMAP["normal"]`
documents the binding.

**tui3 wave 4 — persistent user settings:** `nbchat/tui2/config.py` loads
and saves a small JSON settings file (default `~/.nbchat/tui3.json`,
override `NBCHAT_TUI3_CONFIG`) holding the thinking-block visibility, the
toast/BEL/sound channels, the tool-approval gate + risky-tool list, and the
mouse-wheel scroll tick.  `ChatApp.__init__` merges the file over defaults
(never raising); `_save_cfg()` runs on each toggle (`Ctrl+T`, `/notify`,
`/approve`) and on exit (`run()` finally).  Purely additive; loading is
defensive so a config problem cannot block the TUI.

**tui3 wave 5 — colour theming:** `/theme [dark|light|prime]` switches the
whole UI's colours live and persists the choice.  Implemented with a
`_ThemeRef` proxy in `theme.py`: the public `DARK` name is a stable object
(forwarding every attribute to the currently-active theme), so components
keep writing `DARK.<attr>` unchanged while `theme.set_active(name)`
retargets the proxy in place — the switch is live everywhere with zero
call-site edits.  `/theme` invalidates the logged turns (so they re-render
with the new colours), fires a toast, and saves the choice to the settings
file (a new `theme` key, applied at startup).  `LIGHT` / `PRIME` stay
concrete `Theme` objects for lookup.

**tui3 wave 6 — external control socket (`nbchat-ctl`):** a running TUI
binds a local Unix socket (`~/.nbchat/tui2-ctl.sock`; override
`NBCHAT_CTL_SOCKET`, disable `NBCHAT_NO_CTL=1`) driven by
`python -m nbchat.tui2.ctl <cmd> [arg]` (newline-delimited JSON).  Commands:
`status`, `sessions`, `result` (last assistant reply for the current
session, enabling a headless send→status→result background loop),
`theme <name>`, `send <text>`, `quit`.  Read-only commands answer on the
socket thread; mutating ones enqueue a closure onto
the UI thread via a new `"call"` event type in the render loop and ack
`{"queued": true}` immediately, so a control client can never block or
crash the TUI.  The server is a daemon thread; `ControlServer.stop()`
unlinks the socket on exit; every socket operation is wrapped so a
control-client problem cannot take the TUI down.

**`--bg` headless / detached background agent (wave 6+):** `--bg` (or
`NBCHAT_BG=1`) makes a stdin EOF *not* end the render loop — the bg EOF
path sleeps briefly and falls through to the event drain, so the app stays
alive on its heartbeat and a control-socket `quit` is still processed
(earlier the EOF path `continue`d past the drain, which silently dropped
`quit`).  `nbchat-ctl bg [--session ID] [prompt]` launches a `--bg` TUI in
its own session (survives the launcher / an SSH drop), with its socket at
`~/.nbchat/tui2-bg.sock` and stdout logged to `~/.nbchat/tui2-bg.log`,
optionally submitting a first task.  This completes the "detach without
stopping work" roadmap item (row 7).

**`/monitor` (live per-session observability):** a tui2-native slash command
surfacing the existing `nbchat.core.monitoring` engine's per-session metrics
— cache similarity / invalidation rate, per-tool call counts, reread and
error-after-compression rates, and any detected warnings.  Read-only (no new
subsystem); the data is accumulated by the conversation loop as the session
runs, so it is live.  Returns a friendly note when nothing has been recorded
yet (e.g. a fresh session before the first turn).

**`/inbox` (unseen-email browsing):** a tui2-native slash command over the
existing `nbchat.core.email_inbox` IMAP engine.  `/inbox` lists unseen
messages (position, date, sender, subject); `/inbox <n>` fetches and shows the
full body of unseen message #*n*.  Read-only — nothing is marked read (the
`--email` auto-bridge owns marking read).  The IMAP round-trip runs on a
daemon thread and the result is delivered through the `"call"` event, so the
UI thread is never blocked by the network call.  Requires `GHG_APP_PASSWORD`;
without it the command reports the missing credential and stops cleanly (the
same guard `email_inbox.peek_unseen` already raises).

---

## Not addressed (out of scope for this pass, tracked in the port tracker)

* **Voice / email / supervisor / team surfaces.** These start in
  `nbchat.tui.app.run()` with print-based status output and print-based
  inbound loops; tui2 doesn't start them (their prints would corrupt raw
  mode). Chat + sessions + commands work; the other surfaces need a
  dedicated UI pass (see `docs/prime_tui_port_tracker.md`).
* **Mouse support, scroll *search* + visual copy mode, in-app theming /
  settings, JSON socket API, detachable background agent** — the larger
  herdr items (ranked 1–7, 9–10 in `herdr` research) that need new
  machinery rather than a surface; tracked in the port tracker.
* **CJK/wide-glyph column math** (`_clamp` counts codepoints, not cells).

---

## Verification (2026-07-10)

* **Unit:** `tests/test_tui2.py` — 50 → **63 passing** (13 new regression
  tests: space key, placeholder cursor, blocks+answer rendering,
  one-thinking-block-per-call with chronological order, live mid-stream
  frame, Ctrl+C busy/idle, Ctrl+D submit, `/new`, `/quit`,
  mid-stream redirect with latest-wins and the finalize-spawns-worker
  edge case, last-session resume with history re-render).
* **Full suite:** all green — 247 + 157 tests in two wall-clock chunks
  (the conftest 24 s session guard splits a single full run), zero
  failures, v1 `test_tui.py` untouched-behaviour confirmed.
* **Live pty end-to-end** (`python3 -m nbchat.tui2 --new` against a real
  model, real pty, 110x32): alternate screen entered; header shows model
  + session; a real turn streamed thinking → answer with the spinner
  animating in the status line; the final answer `PING` rendered on its
  own line below the thinking block; user message kept its spaces;
  `/sessions` rendered its list as a note; `/quit` exited with status 0
  and restored the terminal.
* **`--v2` flag:** `python -m nbchat.tui --v2` now launches this app
  (header confirmed over a pty); `python -m nbchat.tui2 --demo` still
  launches the Phase 1 demo.
