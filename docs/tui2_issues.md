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

## Not addressed (out of scope for this pass, tracked in the port tracker)

* **Voice / email / supervisor / team surfaces.** These start in
  `nbchat.tui.app.run()` with print-based status output and print-based
  inbound loops; tui2 doesn't start them (their prints would corrupt raw
  mode). Chat + sessions + commands work; the other surfaces need a
  dedicated UI pass (see `docs/prime_tui_port_tracker.md`).
* **Scrollback / log scrolling** (`ChatLog.scroll`, PgUp/PgDn keys).
* **Full session picker UI** (fuzzy `SelectList` exists but is not wired to
  `/sessions`; the command renders the plain list instead).
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
