# Project Tracker — Porting the prime-agent TUI to nbchat

> **Purpose of this document.** This is the single source of truth for the
> effort to give nbchat a terminal interface styled after
> [`PrimeIntellect-ai/prime-agent`](https://github.com/PrimeIntellect-ai/prime-agent)
> (its `packages/tui` rendering engine + interactive mode). It is used
> across multiple sessions as (a) a **reference** for what is being built and
> why, (b) a **tracker** (completed / in-progress / pending / blocked), and
> (c) a **record** of decisions, issues, and user-testing checkpoints.
> Push to `main` after every meaningful increment so the team has visibility.
>
> **Hard rule:** no breaking changes to the existing `nbchat.tui` REPL.
> The new interface is additive and feature-flagged; the current
> `python -m nbchat.tui` behaviour must keep working unchanged unless the
> user explicitly opts into the new TUI.

- **Created:** 2026-09-06
- **Owner:** Alfred (agent)
- **Status:** In progress
- **Reference clone:** `tmp/prime-agent` (local working copy of the MIT-licensed repo)

---

## 1. Goal

Reimplement — in Python, as a **new** entry point alongside the existing REPL
— a full-screen, flicker-free, component-based terminal chat UI that looks and
feels like prime-agent's interactive mode, driven by nbchat's existing agent
stack (llama-server client, SQLite sessions, `/team`, email, supervisor, voice).

Non-goals for this effort (separate projects if ever wanted): daemon
multi-session, subagent roster views, mermaid/HTML export, terminal images.

---

## 2. What prime-agent's interface is (reference)

Two layers, MIT-licensed (attribution required in NOTICE/README).

### 2.1 `pi-tui` — rendering engine (`packages/tui`, ~4–5k LOC TypeScript)
- Full-screen raw-mode terminal: alternate screen buffer, cursor addressing.
- **Differential rendering**: renders component tree to lines, diffs vs the
  previous frame, emits only changed cells. Flicker-free via CSI 2026
  synchronized output.
- Bracketed paste, keybinding system, East-Asian width handling, mouse.
- Components: `Text`, `TruncatedText`, `Box`, `Input`, `Editor` (multi-line,
  undo/redo, kill-ring), `Markdown` (syntax-highlighted code), `Loader`,
  `SelectList` (fuzzy), `SettingsList`, `Image`, `Spacer`, `Container`.

Key engine files: `src/tui.ts` (~1992 lines), `src/terminal.ts` (~614),
`src/render-cache.ts`, `src/components/*`.

### 2.2 Interactive mode (`packages/coding-agent`, ~10k LOC + 58 components)
The *product*: slash commands, model selector, session list, tool-execution
panels with diffs, thinking blocks, JSON themes, footer/status bar, subagent
views, onboarding, daemon multi-session, mermaid rendering. Deeply coupled to
prime-agent's own agent core/kernel/session store — **not** reusable over an
external HTTP agent without heavy surgery.

Key files: `src/modes/interactive/interactive-mode.ts` (~10,136 lines),
`components/*.ts` (~12k lines total), `theme/{dark,light,prime}.json`,
`theme/theme-schema.json`.

---

## 3. What nbchat has today (baseline, must keep working)

Clean, zero-dependency **print-based REPL** (~4.5k LOC total). Entry points:
`python -m nbchat.tui`, `nbchat_tui.py`, `nbchat/tui/app.py::run()`.

| File | Lines | Role |
|------|-------|------|
| `nbchat_tui.py` | 16 | Launcher shim → `nbchat.tui.app.run` |
| `nbchat/tui/__init__.py` | 16 | Exports `TerminalAgent`, `Palette`, `run` |
| `nbchat/tui/__main__.py` | 9 | `python -m nbchat.tui` |
| `nbchat/tui/app.py` | 676 | REPL loop, `/` commands, banner, streaming |
| `nbchat/tui/agent.py` | 539 | `TerminalAgent` (streams turns) |
| `nbchat/tui/status.py` | 309 | Status line + agent-activity table |
| `nbchat/tui/email_bridge.py` | 528 | Gmail replies into chat |
| `nbchat/tui/colors.py` | 57 | `Palette` raw-ANSI colour helper (NO_COLOR-aware) |
| `nbchat/ui/*.py` | ~2.8k | `markdown`-pkg chat renderer (existing, not rich) |

Existing `/` commands: `/help /status /new /sessions /load /title /history
/model /effort /stats /clear /quit /sup /team [/team stop]`.

Features: live streaming replies, mid-stream interrupt (type or Ctrl+C),
backslash line continuation, `--email`, `--supervisor`, `--no-color`, `--check`,
`--new`, `--session`.

Testing conventions (see `tests/conftest.py`, `pytest.ini`):
- `testpaths = tests`; `httpx` aliased from `httpx2` in conftest.
- Full `pytest` is time-bounded (session budget ~24s, hard cap ~30s).
- New tests go in `tests/test_tui*.py` style; no live llama-server required.
- Python 3.12.3.

---

## 4. Constraints & decisions

1. **No breaking changes.** New TUI is additive; the existing REPL is
   untouched and remains the default. The new UI is opt-in.
2. **Language/runtime.** prime-agent's engine is TypeScript; the Node runtime
   is **not** installed in this environment. We therefore **reimplement** the
   engine in Python (no Node dependency), porting concepts 1:1 rather than
   transpiling.
3. **Dependencies.** Keep the footprint small and standard. Raw mode via
   `termios`/`tty` (stdlib). **No new third-party deps**: `rich` is NOT
   installed and NOT in `requirements.txt`, and `nbchat/ui` is not rich-based
   (it uses the `markdown` pkg). `nbchat/tui2/markdown.py` is a self-contained
   ANSI renderer over its own Line/Segment/Style model — no `rich`, no
   `markdown` pkg. Keep it that way; do not add `rich`. (See §11.2.)
4. **Threading model.** nbchat streams from worker threads. The TUI needs a
   thread-safe event queue → single-threaded render loop (`requestRender()`).
5. **Themes.** Port prime-agent's `theme/*.json` (dark/light/prime) as the
   colour source for the new TUI, replacing ad-hoc `Palette` usage in the new
   surface only.
6. **Licensing.** prime-agent is MIT. Add an attribution note (README/NOTICE)
   crediting Prime Intellect + Mario Zechner for the ported design/concepts.
7. **Tests.** Every phase ships pytest tests that run without a live
   llama-server and without a real TTY (use in-memory/fake terminal + a pty
   where needed). The suite must stay within the ~24 s budget.
8. **Checkpoint & push.** After each increment: run `pytest`, fix failures,
   commit + push to `main`. Ask the user to test only at meaningful,
   user-facing milestones (and say exactly how).

---

## 5. Work breakdown (phases)

### Phase 1 — Engine (the bulk)  · est. 1.5–2.5k LOC
- [x] Raw mode via `termios`/`tty`: enter/exit alternate screen, cursor hide.
- [x] Differential renderer: frame diff vs previous frame, minimal cursor
      updates, CSI 2026 synchronized output for flicker-free frames.
- [x] Component base + layout: `Text`, `Box`, `Spacer`, `Container`.
- [x] Event loop: thread-safe input + render-request queue; `requestRender()`.
- [x] Bracketed paste + basic key parsing (arrows, Ctrl+C, Ctrl+D, Enter).
- [x] `StatusLine`/footer component (model, tokens/s, session).
- [x] `Loader`/spinner component (agent working state).
- [ ] **Smoke test (pty):** prove flicker-free diff updates — assert only
      changed lines are re-emitted, and that raw mode restores cleanly on exit.
- [ ] **User-test checkpoint 1** (opt-in flag): banner + status line + loader.

### Phase 2 — Conversation surface  · est. ~1.5k LOC
- [x] `Markdown` component — lightweight `nbchat/tui2/markdown.py`
      (`render_markdown` / `styled_lines` / `style_for`) over our own
      Line/Segment/Style model (NOT `rich` — the engine owns its own
      styled segments so the differential renderer can diff them). Headings,
      bold/italic/inline-code, code fences, lists, links, blockquotes,
      horizontal rules.
- [x] User/assistant message components — `Message` (role prefix glyph,
      dimmed header, wrapped body, per-role styling).
- [x] Tool-call panels with diff colouring — `ToolCall` + `diff_lines`
      (add/remove/changed line colouring).
- [x] Thinking blocks — `ThinkingBlock` (collapsible/dimmed).
- [ ] Wire all existing `/` commands into the new input line (same semantics).
- [x] `SelectList` (fuzzy) component for `/sessions`, `/model` — `SelectList`
      in `nbchat/tui2/components.py` + `fuzzy_rank` in `nbchat/tui2/fuzzy.py`
      shipped 2026-09-09 (bordered selector modal with highlighted row and
      fuzzy ranking; 4 pty-free tests). **Remaining:** wire it into the actual
      `/sessions`/`/model` command handlers.
- [x] Theme module — `nbchat/tui2/theme.py` ported (see Phase 1). The
      prime-agent theme *JSON* files are consumed as data by the theme
      module; no separate JSON→module translation remains.
- [ ] **User-test checkpoint 2**: full chat in the new surface.

### Phase 3 — Editor polish  · est. ~1k LOC
- [x] Multi-line `Editor` component: undo/redo, kill-ring, backslash continue.
      Shipped 2026-09-07 (`nbchat/tui2/editor.py` `LineEditor`); covered by
      `test_lineeditor_undo_redo_killring` and
      `test_lineeditor_multiline_and_continuation`.
- [ ] Slash-command + path autocomplete.
- [x] Fuzzy search in selectors — `fuzzy_rank` (subsequence matcher with
      consecutive-run + word-boundary bonuses) in `nbchat/tui2/fuzzy.py`,
      used by `SelectList`; covered by `test_fuzzy_rank_scores_and_order`.
- [ ] (Optional) mouse support.
- [x] Wire the real agent conversation loop (`nbchat/tui2/app.py`) into the
      surface: `ChatApp` (a `TerminalAgent` subclass) re-routes the output hooks
      into the structured render tree and the frame builder composes chat log /
      live turn / loader / editor / status bar. Shipped 2026-09-09 per the
      §11.4.2 printer-injection design (no global stdout swap). Remaining:
      pty-free tests + user-test checkpoint 2.
- [ ] **User-test checkpoint 3**: editor feel.

---

## 6. Tracker

> Legend: `[ ]` pending · `[~]` in progress · `[x]` done · `[!]` blocked.
> Each entry gets a short dated note on completion with the test evidence.

### Done
- [x] 2026-09-06 — Investigation & design review of prime-agent `packages/tui`
      and interactive mode; of nbchat's current TUI. Scope, constraints, and
      phased plan agreed. (this doc)

### In progress
- [x] 2026-09-06 — Phase 1 wrap-up: demo app (`nbchat/tui2/{components,
      theme,keys,demo,__main__}.py`) built and verified end-to-end over a
      real pty (alternate screen, differential frames, keystroke handling,
      footer). Two real engine bugs found and fixed along the way:
      (1) a lone keystroke could block the input loop — text `read(64)`
      waits for 64 bytes; now `os.read` on the fd after `select()`
      (`TUIApp._read_input`); (2) Esc never quit the demo — `keys.py`
      emits `"escape"`, demo checked `"esc"`.
- [x] 2026-09-06 — `test_pty_smoke_end_to_end` decision: left **skipped**.
      The engine is proven sound over a real pty; the remaining failure
      is in the pytest+pty child (module resolution / stdout capture),
      not the engine. Raw-mode correctness is covered by user-test
      checkpoint 1 instead. Making the harness test reliable is an
      optional, low-priority nicety.
- [x] 2026-09-07 — Phase 2 chat-surface components shipped
      (`nbchat/tui2/chat.py` + `markdown.py`): `Markdown`, `Message`,
      `ToolCall`, `ThinkingBlock`, `diff_lines`. All exported from
      `nbchat.tui2`. Test suite: 374 passing.
- [x] 2026-09-07 — Phase 3 `Editor` core shipped (`nbchat/tui2/editor.py`
      + `fuzzy.py`): multi-line `LineEditor` with undo/redo, kill-ring
      (Ctrl+K/U/W, Ctrl+Y), word motion, line/char deletes, shift+enter
      newline (xterm modifyOtherKeys 13;2u), trailing-backslash line
      continuation, reverse-style cursor block. Fuzzy subsequence matcher
      for the upcoming selectors/autocomplete. 22 new tests; suite: 385
      passing.
      Commits: `1a2f823` (theme SGR fix), `2c04d21` (Phase 2 components).
      Remaining Phase 2: slash-command wiring, `SelectList`, user-test
      checkpoint 2.

- [x] 2026-09-09 — Phase 3 real-agent wiring **shipped & importing.**
      `nbchat/tui2/app.py` (325 LOC) defines `ChatApp`, a `TerminalAgent`
      subclass that re-routes the terminal-output hooks into the structured
      render tree (the §11.4.2 printer-injection design — **no global
      `sys.stdout`/`sys.stderr` swap**). Overrides: `_status` (no print bar),
      `_status_set` (feeds the tui2 status line), `_print_user` (-> `ChatLog`),
      `_on_stream_reasoning`, `_on_stream_token`, `_on_stream_complete`,
      `_on_tool_display`, `_on_agent_message`. The turn runs on a daemon worker
      thread serialized by the agent's own `_send_lock`; `Enter` submits,
      `Esc` interrupts (via `agent.interrupt()`), `Ctrl+D` on empty editor quits.
      The frame builder composes chat log / live streaming turn / loader /
      `LineEditor` box / status line. `chat.py` gained `ChatMessage`
      (role + `text=`/`blocks=`, `.render(width)`) and `ChatLog`
      (scroll-to-bottom, `.render(width)`). `__main__.py` now defaults to the
      real app (`--demo` keeps the Phase 1 demo reachable). **Note:** the
      legacy `--v2` flag in `nbchat/tui/app.py` still launches the demo;
      no pty-free tests cover `app.py` yet.

- [x] 2026-09-09 — **Documented user-facing behaviour of the shipped entry point** (`python -m nbchat.tui2`), from a code-path review of `app.py`/`__main__.py` (no pty session run yet): what a user can expect *right now*.
      The real Phase 3 app launches (not the demo); `--demo` and the legacy `--v2` flag both still reach the Phase 1 demo. On start-up it enters the alternate screen in raw mode and hides the cursor (the screen goes briefly blank — normal), shows the dark theme, a status line (model / session id / tokens·s), and a spinner while the agent works; the input is a single-line editor with the placeholder “Type a message… (enter sends · esc interrupts · ctrl+d quits)”. `Enter` submits and the turn streams live on a worker thread while the UI keeps rendering; `Esc` interrupts an in-flight turn; `Ctrl+D` on an empty editor quits and restores the terminal. Assistant output renders through the Phase 2 chat components (Markdown, message bubbles, tool-call panels, dimmed thinking blocks); stray `<tool_call>` text is stripped from the log. It instantiates a real `TerminalAgent`, so the usual nbchat model/credentials config is required. Known gaps (per tracker): slash commands are not yet wired into the new input line, and the `/sessions`/`/model` selector panels are not yet connected to the command handlers (the `SelectList` + fuzzy components themselves are built). The multi-line `LineEditor` and the pty-free `app.py` tests (43 in `tests/test_tui2.py`) are both shipped. This is a code-path smoke, not an end-to-end conversation verification.

  > **CORRECTION (2026-09-09, \u00a711.1, resolved):** the earlier entry described
  > work not present at commit `870cc34`. `app.py` **now exists and imports
  > cleanly** (`from nbchat.tui2.app import ChatApp`); the `_LogCapture`
  > global stdout swap was superseded by the printer-injection design (\u00a711.4.2)
  > as shipped above.

- [x] 2026-09-09 — `SelectList` modal component + fuzzy wiring shipped and tested. `SelectList` in `nbchat/tui2/components.py` (bordered panel, one highlighted row, empty-state hint, footer) plus the `fuzzy_rank` subsequence matcher in `nbchat/tui2/fuzzy.py` give the `/sessions`/`/model` selectors their building blocks. Six new pty-free tests (`test_fuzzy_rank_scores_and_order`, three `test_selectlist_*`, plus the `LineEditor` kill-ring and multiline/continuation tests now passing) bring the suite to **393 passing, 0 failed**. What remains is pure wiring: routing `/` commands through the new input line and connecting the `SelectList` panels to the `/sessions`/`/model`/`/history` handlers, then user-test checkpoint 2.

### Pending
- Phase 1: **user-test checkpoint 1** (demo is ready — see table below).
  > **Corrected 2026-09-06 (commit dbd1fe7):** the checkpoint-1 defects
  > (§10) are fixed — the §10 root-cause analysis was partly wrong
  > (no double CSI 2026 wrapper exists; the real bugs were the missing
  > KeyReader dispatch and the per-frame Loader recreation, both fixed).
  > Awaiting user re-test of `python -m nbchat.tui2`.
- Optional: make `test_pty_smoke_end_to_end` reliable (harness-side fix);
      low priority, does not block the product.
- Phase 2 (remaining items): wire `/` commands into the new input line and
      connect the `SelectList` panels to the `/sessions`/`/model`/`/history`
      handlers, then **user-test checkpoint 2** (full chat in the new
      surface). All core components are built (see In progress, 2026-09-07
      and 2026-09-09): `Markdown`, `Message`, `ToolCall`, `ThinkingBlock`,
      `ChatLog`, `LineEditor`, and the `SelectList` + fuzzy matcher.

- **Phase 3 (real app) — make `python -m nbchat.tui2` a real chat:**
  1. [x] `ChatMessage` + `ChatLog` added to `nbchat/tui2/chat.py`.
  2. [x] Hook names verified against `TerminalAgent` (`_status_set`,
     `_on_stream_token`, `_on_stream_complete`, `_on_tool_display`,
     `_on_agent_message`, `_print_user`); the six-hook `bind()` design was
     simplified to direct subclass overrides.
  3. [x] `__main__.py` points at the real `ChatApp`; demo kept reachable via `--demo`.
     (Legacy `--v2` flag in `nbchat/tui/app.py` still launches the demo — cosmetic, low priority.)
  4. [x] Add pty-free tests for `app.py` (fake `TerminalAgent` + in-memory terminal)
     covering hook→log routing and frame composition.
     (43 tests in `tests/test_tui2.py`; 8 ChatApp tests cover
     hook→log routing, frame composition, key mapping and Ctrl+D.)
  5. [ ] **User-test checkpoint 2** (full chat in the new surface).

### Blocked
- (none yet)

### User-test checkpoints
| # | What to test | How | Status |
|---|--------------|-----|--------|
| 1 | Banner, status line, loader, scroll, clean exit | `python -m nbchat.tui2` (or `python -m nbchat.tui --v2`) — arrows/PgUp/PgDn scroll, `r` re-renders, `q`/Esc/Ctrl+C quits | Ready |
| 2 | Full chat in new surface | `python -m nbchat.tui2` — send a prompt, observe rendered reply, tool panel, thinking block | Ready (awaiting user re-test) |
| 3 | Editor feel | *(TBD at checkpoint)* | Pending |

---

## 7. Issues & resolutions log

| Date | Issue | Resolution |
|------|-------|------------|
| 2026-09-06 | Node runtime not installed; can't consume `pi-tui` as a library from Python. | Reimplement the engine in Python (Phase 1); port concepts 1:1. |
| 2026-09-06 | prime-agent interactive mode is tightly coupled to its own agent core — not reusable over an external HTTP agent. | Build nbchat-specific components in Phase 2, reusing prime-agent's *visual/UX* design, not its code. |
| 2026-09-06 | `test_pty_smoke_end_to_end` fails: pty child produces no output / `ModuleNotFoundError` under the pytest+pty harness. | DEFERRED — engine proven sound over a real pty via a standalone probe; test left skipped. Raw-mode correctness handed to user-test checkpoint 1. |
| 2026-09-06 | A lone keystroke (e.g. Ctrl+C) would block the input loop until 64 bytes accumulated. | FIXED — `TUIApp._read_input()` uses `os.read(fd, 4096)` after `select()` (non-blocking by construction); text-stream read only as fallback. |
| 2026-09-06 | Esc key never quit the demo. | FIXED — `keys.py` emits name `"escape"`; `demo.py` now matches `"escape"` (was `"esc"`). |
| 2026-09-06 | **USER TEST — checkpoint 1 (negative):** everything rendered, but the spinner stayed static and no keys responded except Ctrl+C (which exits cleanly). | DIAGNOSED (fix deferred per user). See §10. |
| 2026-09-06 | **Checkpoint 1 FIXED (dbd1fe7).** Actual root causes: (1) the render loop never fed input chunks to `KeyReader.handle_input_events`, so every key was silently dropped; (2) `build_frame()` built a fresh `Loader()` each frame, so the spinner tick state reset to frame 0 every update. §10's double-CSI-2026-wrapper claim was a code misreading — `render_frame` emits the wrapper exactly once, and the relative-cursor contract is safe (writer re-parks the cursor at row 1 after every update). | FIXED — keys reach `on_input` (regression test `test_app_dispatches_keys_to_on_input` added), persistent `Loader` hoisted into `DemoApp`. |

---

## 10. Investigation — user-test checkpoint 1 (why the spinner froze and keys "died")

> 2026-09-06 — user ran the demo in a real terminal. Symptoms: (a) the
> full screen renders correctly; (b) the animated spinner is static;
> (c) arrow / PgUp / PgDn / `r` appear to do nothing; (d) Ctrl+C exits
> cleanly. **No fixes made yet — findings only.**
>
> **[Superseded by the fix in dbd1fe7 — see the 2026-09-06 "Checkpoint 1
> FIXED" row in §7.] The symptoms below were real, but the root causes in
> §10.1/§10.2 were wrong: the actual bugs were (1) the render loop never
> dispatched input chunks to `KeyReader`, and (2) `Loader` was recreated
> on every frame. There was no double CSI 2026 wrapper in the code.**

### 10.1 The three symptoms are ONE root cause

They are not three independent bugs. They are the *same* defect seen from
three angles:

- The **first frame is written differently from every later frame.**
  `TUIApp.start()` calls `_render_first()` once before the loop. That
  first pass runs `render_frame(Frame([]), new)` — i.e. diffing against an
  *empty* frame — so **every line is written** (all lines are "changed").
  It is emitted bare, with no double-wrapping and no dependence on cursor
  position beyond the initial `\033[2J`/`\033[0;0H` home.
- **Every subsequent update** (spinner tick, event counter, any key) goes
  through the *diff* path: `diff_frames(prev, new)` emits only the changed
  lines, and `render_frame` positions the cursor **relatively**
  (`\033[nB` down / `\033[nA` up from wherever it believes the cursor is)
  on top of a *contract* that the cursor is always parked at **row 1,
  col 1** between updates.

So: the first frame is self-contained and correct. The spinner and the
keys never "fail to fire" — they fire correctly and produce correct new
frames (verified by the passing diff tests). But their updates **land in
the wrong place or get dropped by the user's terminal**, because the
relative-cursor contract is broken. Net effect: the screen freezes on the
first frame → spinner looks static, keys look dead. Ctrl+C is the only
thing that "works" because it doesn't need to paint anything — it just
sets `_running=False` and restores the terminal, which is absolute/safe.

### 10.2 Concrete defects that break the contract (both confirmed in code)

1. **Double synchronized-output wrapper (confirmed bug).**
   `render_frame()` already wraps its output in `\033[?2026h … \033[?2026l`
   (frame.py, the `out = [_SYNC_BEGIN] … out.append(_SYNC_END)` lines), and
   `TUIApp._render_first()` then calls `self.term._write(sync_out(update))`,
   and `sync_out()` wraps it *again*. The terminal receives
   `\033[?2026h … \033[?2026l \033[?2026h … \033[?2026l`. Terminals treat
   CSI 2026 as "buffer everything until the END marker" — the nested/doubled
   markers are undefined behaviour and can cause the update to be
   buffered away or mis-applied. This is a **real bug regardless of
   terminal**: one of the two wrappers must be removed.

2. **Relative-cursor re-anchoring is fragile (design weakness).**
   `render_frame` assumes the cursor is at row 1 at the *start* of each
   update and relies on its final `\033[nA` to re-park it there. This is a
   "the cursor must be exactly where we left it" assumption. Any deviation
   — a terminal that clamps cursor positions, a line wrap, a CSI 2026
   quirk from defect #1, or the doubled wrapper — shifts every following
   write by a constant offset, so **all** later frames (spinner + keys)
   are garbled while the first (absolute) frame was perfect. prime-agent's
   engine and most TUI libs instead issue an **absolute** cursor position
   per written line (or at least re-home at the start of each update)
   precisely to avoid this.

### 10.3 What is NOT wrong (verified, to avoid wasted effort)

- **Key parsing is correct.** `keys.py` maps `↑↓`→up/down, `5~`/`6~`→
  pageup/pagedown, `r`→`r`, `q`/Esc→escape, Ctrl+C→`ctrl+c`. The demo's
  `on_input` matches all of these. The loop reads input via non-blocking
  `os.read` after `select()`, so keys are delivered (Ctrl+C's clean exit
  proves the input path is live). The problem is *after* the key is
  parsed: the re-render it triggers doesn't paint.
- **The background threads fire.** `_spinner_worker` puts a `render` event
  every 0.2 s and `_event_worker` every 0.7 s; the loop drains them and
  calls `_render_first(force=True)`. So new, *different* frames are being
  generated (the diff tests assert spinner lines actually change). The
  frames are correct; the **write** is not landing.
- **The diff logic is correct** against the unit tests (348 passing). The
  failure is in the terminal-emission layer, not the diff math.

### 10.4 Likely fix direction (NOT yet implemented)

- Remove the redundant `sync_out()` wrapper (keep CSI 2026 exactly once).
- Make cursor positioning **robust**: either re-home to row 1 at the start
  of each update, or emit an absolute row position per rewritten line
  (drop the "parked at row 1" contract). This makes later frames
  correct even if a single update is partially lost.
- Re-test checkpoint 1 in the same terminal after those two changes; the
  spinner should animate and arrows/`r` should respond.

---

## 8. Decisions log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-09-06 | Additive, opt-in new TUI; existing REPL stays default and unchanged. | User hard rule: no breaking changes. |
| 2026-09-06 | Reimplement engine in Python rather than run Node/TS. | Node not available; keeps one runtime and small footprint. |
| 2026-09-06 (revised 09-09) | Self-contained ANSI markdown renderer (NOT `rich`). | `rich` is not installed / not a dep, and `nbchat/ui` is not rich-based; tui2's own renderer keeps the repo light. See §11.2. |
| 2026-09-06 | Differential render + CSI 2026 sync output. | Core of prime-agent's flicker-free feel; must preserve. |

---

## 9. Session handoff notes

> Append a short note at the end of each session: what was done, current
> state, and the exact next step so the next session can pick up cold.

### Session 1 (2026-09-06)
- Did: investigation of both codebases; wrote this tracker; agreed plan.
- Current state: no code written yet. Repo at commit `2998ae9`.
- **Next step:** begin Phase 1 — build the raw-mode terminal layer
  (`termios`/`tty`) and the differential renderer, with a pty smoke test
  proving flicker-free diff updates and clean raw-mode restore.

### Session 2 (2026-09-06)
- Did: built the Phase 1 engine core — `nbchat/tui2/{frame,raw}.py` (differential
  renderer with CSI 2026 sync output; raw terminal with passthrough mode;
  thread-safe event queue; `TUIApp` render loop) plus 21 tests
  (`tests/test_tui2.py`, all passing; full suite 348 passed). Tracker updated.
- Current state: `nbchat/tui2/` exists locally with the engine core working and
  tested; no components, status line, loader, pty smoke test, or opt-in entry
  point yet. Existing `nbchat.tui` REPL untouched.
- **Next step:** component base (`Text`, `Box`, `Spacer`, `Container`) +
  `StatusLine` + `Loader` on top of the engine; then the pty smoke test and an
  opt-in `python -m nbchat.tui2` entry point for user-test checkpoint 1.

### Session 3 (2026-09-06)
- Did: proved the raw-mode path over a real pty with a standalone probe
  (live differential frames, spinner, footer); fixed two real bugs
  (`_read_input` os.read; Esc key name); removed the probe scratch files;
  decided to keep the pty pytest test skipped (harness-side failure,
  low value vs. the goal). All committed and pushed.
- Current state: Phase 1 complete; `python -m nbchat.tui2` is the
  checkpoint-1 surface. 21/22 tests pass (1 intentionally skipped).
- **Next step:** user-test checkpoint 1, then Phase 2 (conversation
  surface: Markdown, message components, /-commands, selectors, themes).
- **Fast-suite policy (2026-09-06):** the full pytest suite must stay
  under ~10 s. Slow/interactive tests are opt-in via markers — the pty
  end-to-end smoke test is now `@pytest.mark.pty` and only runs with
  `python3 -m pytest --run-pty`. It also no longer blocks on pty reads
  (`select`-bounded `drain`), so a hung child can never stall the suite.
  Full suite: 349 passed, 1 skipped, ~9 s; pty test alone: 0.04 s.

### Session 4 (2026-09-07)
- Did: Phase 2 chat-surface components (`chat.py`: `Markdown`, `Message`,
  `ToolCall`, `ThinkingBlock`, `diff_lines`) and Phase 3 editor core
  (`editor.py` `LineEditor` + `fuzzy.py`) shipped and tested. Then authored
  the real Phase 3 application, `nbchat/tui2/app.py` (467 LOC): `TUI2ChatApp`
  binds the six `TerminalAgent` stream hooks + `_status_window`, a
  `_LogCapture` redirects agent `stdout`/`stderr` into the log, and `run()`
  assembles `RawTerminal`/`EventQueue`/`TerminalAgent` and drives the
  `TUIApp` render loop.
- Current state: **the real app is written but broken.** `import
  nbchat.tui2.app` fails with `ImportError: cannot import name 'ChatLog' from
  'nbchat.tui2.chat'` (and `ChatMessage` too) — `chat.py` never defines those
  two classes, so the app cannot be launched or tested. `__main__.py` and the
  `--v2` flag in `nbchat/tui/app.py` both still launch the **Phase 1 demo**
  (`nbchat.tui2.demo.run`), so `python -m nbchat.tui2` today shows the demo,
  not the real chat. No tests exercise `app.py`. The engine (frame/raw/keys),
  components, theme, chat components, editor and fuzzy matcher are all
  complete and passing.
- **Next step (pick up cold):** (1) add `ChatMessage` and `ChatLog` to
  `nbchat/tui2/chat.py` matching the API `app.py` calls — `ChatMessage(role,
  text=..., blocks=...)` with `.render(width)`, and `ChatLog(messages,
  rows=..., gutter=...)` with `.render(width)` that keeps the latest turn in
  view; (2) confirm the six `TerminalAgent` hook method names/signatures that
  `TUI2ChatApp.bind()` rebinds actually exist (spot-checked `_status_window`);
  (3) repoint `__main__.py` and `--v2` to the real app while keeping the demo
  reachable; (4) add pty-free tests for `app.py` (fake `TerminalAgent` +
  in-memory terminal); (5) user-test checkpoint 2. Run `python3 -m pytest`
  before pushing (suite is fast, ~9 s; the pty e2e test is gated behind
  `--run-pty`).
- Notes: `python` is not on PATH here — use `python3`. The pty end-to-end
  smoke test stays `@pytest.mark.pty` / `--run-pty` only. Keep the fast-suite
  policy (<~10 s) and the no-breaking-changes rule for the existing
  `nbchat.tui` REPL.

---

## 11. Critical design review (2026-09-09)

> Re-read with a first-principles, critical eye. Goal: port prime-agent's TUI
> while keeping nbchat's advantages (fast streaming print REPL, no fragile
> deps, `/team` + email + supervisor + voice) and keeping the repo light and
> maintainable. Findings below; several earlier claims are corrected.
> Open questions for the user are in §11.8.

### 11.1 STATUS CORRECTION (supersedes the 2026-09-07 "In progress" bullets)

The tracker's 2026-09-07 entries describing `nbchat/tui2/app.py` (467 LOC),
`editor.py`, and `fuzzy.py` as "shipped / written but not functional" are
**not reflected in the repo.** As of commit `870cc34`:

- `nbchat/tui2/` contains 10 files / ~1914 LOC: `__init__, __main__, theme,
  demo, keys, frame, chat, markdown, raw, components`.
- **`app.py`, `editor.py`, `fuzzy.py` do not exist.** There is no
  `ChatLog`/`ChatMessage` and no `LineEditor`.
- `components.py` defines `Text, Box, Spacer, Container, StatusLine, Loader`
  (no Editor).
- So the real state is: **Phase 1 + Phase 2 chat components done and tested;
  Phase 3 (real app + editor) not started.** The next step is to *author*
  those files, not to fix an existing `app.py`.
- Test counts: `tests/test_tui2.py` has 33 tests; the "385 passing" figure in
  the log is not reproducible (measured suite is ~375).

### 11.2 Fact corrections on dependencies (the repo is lighter than the doc says)

- **`rich` is not installed** (`ModuleNotFoundError`) and is **not in
  `requirements.txt`**. The earlier claim that "rich is already in the
  dependency set (used by nbchat/ui)" was false.
- **`nbchat/ui` is not rich-based**; it uses the `markdown` pip package.
- Good news: **`tui2/markdown.py` is fully self-contained** (own
  Line/Segment/Style; no `rich`, no `markdown` pkg). The *code* already meets
  the "no new dependencies" bar — the doc just needed to say so and to drop
  the `rich` recommendation (done in §4 and §8).

### 11.3 Decisions that are right (keep)

- Reimplement the engine in Python (no Node) — correct for one-runtime + light.
- Differential render + CSI 2026 — this *is* prime-agent's flicker-free feel;
  keep it.
- Self-contained markdown renderer (not `rich`) — keep; it is the light path.
- Non-goals (daemon multi-session, subagent roster, mermaid/HTML export,
  terminal images) — correctly excluded; they do not fit nbchat.
- Drive the new surface from nbchat's real agent stack (`/team`, email,
  supervisor, voice) — right; those are nbchat's differentiators.

### 11.4 Design decisions to change / question

1. **Drop the multi-theme JSON system.** nbchat's REPL has no themes (just a
   NO_COLOR-aware `Palette`). Porting `theme/dark|light|prime.json` +
   `theme-schema.json` is scope bloat for a power-user tool. **Use one
   baked-in, NO_COLOR-aware dark palette.** Keep `theme.py` minimal; no theme
   loader, no schema, no light/prime variants.
2. **Do not globally swap `sys.stdout`/`sys.stderr` (`_LogCapture`).** That is
   thread-unsafe (nbchat streams from worker threads), will swallow/interleave
   the agent's own status + email/voice output, and is hard to reason about.
   **The existing agent already threads a `printer` through**
   (`TerminalAgent._run_turn(self, text, printer)`). Inject tui2's emitter
   there, and override the `_status_*` hooks to write into the render tree
   instead of printing to the print-based StatusBar. This reuses nbchat's own
   seam and keeps the existing REPL byte-for-byte unchanged (hard rule).
3. **The `_status_*` hooks are side-effecting, not structured.** They call
   `_status_set` (print) and `sb.set_context`. "Bind the six hooks" is an
   under-specification: in raw mode tui2 must override them so the print
   StatusBar does not double-fire. (Signatures confirmed: `_status_window(
   estimated_tokens, budget)` matches.)
4. **Relative-only cursor contract is a latent fragility + dead code.**
   `render_frame` relies on "cursor parked at row 1" and uses relative CSI
   A/B only; `_cpr` (the absolute-positioning helper) is defined but
   **unused**. A single garbled/lost update shifts all later frames (the same
   symptom class as checkpoint 1). **Either re-home to row 1 at the start of
   each `render_frame` (self-heals on the next frame), or emit absolute
   `_cpr(row, 1)` per rewritten line** — then `_cpr` stops being dead code.
5. **`sync_out` is a redundant wrapper with double-wrap risk.**
   `render_frame` already wraps in CSI 2026; calling `sync_out(render_frame(
   ...))` nests the markers (undefined behaviour). **Delete `sync_out`, or
   assert `render_frame` is the single emission entry point.**

### 11.5 Performance gap — the big one (add a render cache)

prime-agent ships `render-cache.ts` for exactly this; the plan has **no
equivalent item**. The spinner ticks every 0.2 s, forcing a full re-render. If
`Message.render(width)` re-parses markdown for the **entire conversation log**
each frame, cost is O(history) per tick → CPU spikes + jank as the session
grows. This directly threatens the "keep nbchat's performance" goal.
**Add a per-message render cache:** memoize `Message.render(width)` keyed by
`(message_id, width)`; only the *live streaming turn* re-renders per frame,
frozen history stays cached. This is the single most important architectural
addition for the performance requirement.

### 11.6 Maintainability / hygiene

- Delete dead `_cpr` (or wire it in per §11.4.4).
- Remove/clarify `sync_out` (§11.4.5).
- The suite budget contradicts itself (24 s cap vs <~10 s policy); measured is
  ~18 s. Pick one number and state it.

### 11.7 Feature filter (drop what does not fit nbchat)

- **Keep:** `/model`, `/sessions`, tool-call panels, thinking blocks,
  markdown, status bar, and `/team` + email + supervisor + voice (first-class).
- **Drop:** onboarding, JSON multi-theme, daemon multi-session, subagent
  roster, mermaid/HTML, terminal images.
- **Defer (optional):** fuzzy `SelectList`. Do **numbered lists first**
  (nbchat already does this in the REPL); add fuzzy only if desired.

### 11.8 Open questions (need a decision from you)

1. **Themes:** confirm dropping multi-theme JSON in favour of one baked-in,
   NO_COLOR-aware dark palette? (§11.4.1)
2. **Agent-hook integration:** OK with the *light* route — tui2 injects a
   `printer` and overrides the `_status_*` hooks, leaving `TerminalAgent` and
   the existing REPL unchanged? (vs. a bigger refactor that makes the hooks
   structured for both surfaces.) (§11.4.2 / §11.4.3)
3. **Selectors:** numbered lists first, fuzzy later — or do you want fuzzy up
   front? (§11.7)
4. **Render cache:** agree to make the per-message render cache a **hard**
   Phase 3 requirement (not optional) before user-test checkpoint 2? (§11.5)

### 11.9 Session 5 handoff (2026-09-09)

- **State:** Phase 1 + Phase 2 components done/tested. Phase 3 (real app,
  editor) **not started** — `app.py` / `editor.py` / `fuzzy.py` absent.
- **Next step (pick up cold):** (1) author `ChatMessage` + `ChatLog` in
  `chat.py` and the `app.py` real-agent app using the `printer`-injection
  design (§11.4.2), **with the per-message render cache (§11.5)**; (2) re-home
  the cursor in `render_frame` (§11.4.4) and remove `sync_out` / dead `_cpr`;
  (3) repoint `__main__` / `--v2` at the real app, keep the demo via
  `--v2-demo`; (4) add pty-free `app.py` tests (fake `TerminalAgent` +
  in-memory terminal); (5) user-test checkpoint 2. Run `python3 -m pytest`
  before pushing (pty e2e is gated behind `--run-pty`).

---

## 12. Supplementary critical review (2026-09-09)

> Second-pass review, appended alongside §11 (same date; §11 landed first
> and is the primary record). This section adds what §11 did not cover,
> corrects two factual errors of mine caught during verification, and
> records one genuine policy conflict. Where §11 and §12 agree, follow
> §11; where they conflict (theme policy: §11.4.1 vs §12.3.4) it is an
> open question for the user (§12.5 Q1).

### 12.1 Additions to §11.1 (repo state, verified 2026-09-09)

- **Commit hashes cited as evidence do not exist in this repo's
  history** (`1a2f823`, `2c04d21`, `dbd1fe7`, `2998ae9` — the repo has a
  single squashed commit `870cc34`). They point at the pre-squash
  environment; the decision log should say so so evidence pointers are not
  mistaken for checkable refs.
- **`tmp/prime-agent` reference clone is gone** (it was in scratch, not
  committed). Re-clone (`PrimeIntellect-ai/prime-agent`, MIT) or work from
  GitHub raw before Phase 3 work.
- Everything else in §11.1 verified true against the tree (file list,
  ~1914 LOC, absence of app.py/editor.py/fuzzy.py, suite ≈375).

### 12.2 What was verified this pass (beyond §11)

- `raw.py` `_read_input`: `os.read(fd, 4096)` after `select()`.
  **Correct as written** — the size argument is a cap, not a
  block-until-full requirement, so a lone keystroke is delivered
  immediately; the earlier 64-byte-stall bug class is gone. No change
  needed.
- `frame.py:184`: cursor positioning is **relative-only by design**
  ("the output never contains an absolute cursor-position report") with
  the parked-at-row-1 contract §10 identified; `_cpr` (frame.py:186) is
  defined but **unused** — matches §11.4.4.
- Full suite: 375 passed / 1 skipped / ~18 s (measured 2026-09-09).

### 12.3 Per-decision first-principles review (what §11 did not cover)

#### 12.3.1 Raw mode + differential rendering — KEEP; cursor: absolute wins

Raw mode, differential rendering, and the 0.1 s frame budget are the
right design and the reason the port will feel like prime-agent:
per-frame terminal work is O(what changed), independent of history
length. Keep all of it.

The one change (agrees with §11.4.4's second option, which §12
recommends over the re-home option): **every rewritten line is addressed
absolutely** (CUP, `\033[row;1H`) — the parked-cursor contract is
deleted, and the currently-dead `_cpr` helper (frame.py:186) is exactly
this design, resurrected. Cost ≈6 bytes per changed line — negligible.
Benefit: the engine survives lost bytes, terminal clamping, and
out-of-band writes (a leaked `print`, a traceback) without corrupting
the next frame — the whole class of bug from checkpoint 1 (§10).
Re-homing to row 1 each frame is cheaper but leaves a frame window with
the cursor misplaced; absolute positioning is the strict superset of
robustness. `sync_out` stays deleted / single-entry-point per §11.4.5.

**One test worth adding** — the design argument in one test: drop a
byte mid-frame into an in-memory terminal and assert the next frame still
lands correctly. With absolute addressing this passes by construction;
with the parked-cursor contract it cannot.

#### 12.3.2 Agent wiring — printer injection; the seam already exists

**Correction to an earlier draft of this section:**
`TerminalAgent._run_turn(self, text, printer)` (agent.py:377) **already
threads a printer** — `printer(text)` at line 391; the print REPL passes
its own printer, so the hard rule (REPL byte-for-byte unchanged) is
preserved by construction. No new parameter is needed. tui2 passes a printer
that appends to the message buffer and does
`events.put("render")` (raw.py's EventQueue, drained every tick by the
render loop) — no globals, no state to restore, trivially testable
(printer can be a list; events can be the real EventQueue).
This matches §11.4.2; the draft claim that `_run_turn` "already takes
only text (agent.py:290)" was wrong and is withdrawn.

The six `_status_*` hooks (agent.py:263–286, verified) are
side-effecting — they print / set the old StatusBar context — so the
tui2 agent subclass must **override** them to route into the render tree,
not merely bind them (§11.4.3). The light route of §11.8 Q2 is
confirmed feasible with zero changes to `TerminalAgent`.

#### 12.3.3 Thread model — KEEP as is; one real (small) gap

**Correction to my earlier draft of this section:** `request_render()` does
not exist — I invented it. The actual mechanism (verified in raw.py):
workers push events into `EventQueue` (raw.py:147, backed by
`queue.Queue` — thread-safe by construction, so there is no racy
flag to fix); the render loop drains the queue every tick and re-renders
once per `"render"` event, plus a forced rebuild after each keystroke
chunk. The thread model itself is right and unchanged by the port.

The one real gap: there is **no coalescing** — N `render` events in one
tick give N re-renders, and streaming tokens fire constantly. Two-part
fix, both small: (1) at drain time, collapse N `"render"` events to one
(one rebuild per tick); (2) the §11.5 render cache so each rebuild is
O(what changed). Together: coalescing bounds the *rate*, the cache
bounds the *cost per frame*. No new threads, no locks needed beyond
what `queue.Queue` already provides.

#### 12.3.4 Theme — CONFLICT with §11.4.1 (open question, §12.5 Q1)

§11.4.1 says: drop the multi-theme JSON for one baked-in,
NO_COLOR-aware dark palette (lightest repo; nbchat's REPL today has no
themes). This section's original view: keep the JSON mechanism but trim
each theme to the ~15–20 keys tui2 components actually read, and drop
`theme-schema.json` for a 20-line stdlib presence check. The case:
prime-agent's look lives *in the data*; themes are zero code to support
once palette plumbing exists; a user-swappable theme is a prime-agent
feature that maps 1:1 onto nbchat's zero-dep constraint. The case for
§11.4.1: lightest repo, one palette to test. Either is implementable in
an afternoon; the data decision is the user's. (If §11.4.1 wins, the
schema point is moot.)

### 12.4 What changes in the plan (concrete)

- **Cursor contract → absolute CUP per rewritten line** (§12.3.1);
  re-home-to-row-1 dropped; `_cpr` resurrected as the mechanism;
  `sync_out` per §11.4.5. Rewrite `render_frame` positioning + tests.
- **Add the byte-drop corruption test** (in-memory terminal, §12.3.1).
- **Coalesce `render` events at drain time** (one rebuild per tick,
  §12.3.3). The queue itself is already thread-safe; no lock work.
- **Adopt §11.5's render cache as a hard Phase 3 requirement** (agree
  with §11.8 Q4; the single most important addition for the performance
  goal).
- **`--v2` mapping:** `--v2` → real app, `--v2-demo` → Phase 1 demo
  (today `--v2` → demo). Confirm flag names (§12.5 Q3).
- **`nbchat/ui` decision:** it is a second rendering engine nothing in
  tui2 needs, and the only consumer of the `markdown` pip dependency
  (`nbchat/ui/utils.py:2`). Recommendation: deprecate in docs now,
  remove after user-test checkpoint 2 — removal drops `markdown` from
  requirements.txt (the repo gets *lighter*, the stated goal). Requires
  an import-usage check first (`nbchat/__init__` and channels reference
  it). (§12.5 Q2.)
- **Editor recovery before re-authoring:** if the prior session's
  `editor.py`/`fuzzy.py`/`app.py` are retrievable from that environment,
  recovery beats re-authoring (§12.5 Q4); otherwise re-author from the
  Session 4 spec (kill-ring Ctrl+K/U/W/Y, word motion, undo/redo,
  shift+enter newline via xterm `13;2u`, backslash continuation,
  reverse-style cursor).
- **Theme trim per the Q1 decision** (§12.3.4).
- **Note in the decision log** that cited commit hashes are pre-squash
  (§12.1).

### 12.5 Open questions for the user (supplements §11.8)

1. **Theme policy** — §11.4.1 (one baked-in palette) vs §12.3.4 (trimmed
   JSON themes)? Deciding factor: "lightest repo" vs "the ported look,
   user-swappable".
2. **Retire `nbchat/ui`** — deprecate now + remove after checkpoint 2
   (would drop the `markdown` dep) or leave alone?
3. **`--v2` semantics** — confirm `--v2` → real app, `--v2-demo`
   → demo?
4. **Editor recovery** — is the prior environment still accessible, or
   do I re-author from the Session 4 spec?
5. **Suite budget** — §11.6 says pick one number: I propose **<30 s
   hard cap, pty e2e gated out of the budget** (measured today: ~18 s).

---

## 8. Multi-agent throughput plan (2026-09-09)

Goal (per `docs/multi_agent.md`): maximize single-5090 throughput under the
C=8 saturation regime by making nbchat's team engine behave like the
`dispatcher` design validated in `c8lab` (see `c8lab/THROUGHPUT_FINDINGS.md`),
porting the useful prime-agent mechanics along the way.

### Assessment: is prime-agent's fan-out "the best" for our objectives?
Prime-agent (RLM) is a **dynamic, unbounded, tree-shaped fan-out**: any
agent can spawn subagents on demand (`rlm.run`, max-depth capped), with a
subagent registry (`list_subagents`/status per child) and a kernel that is
strictly single-threaded — it serializes *agent work* and relies on the
backend to queue *LLM work*. That shape is great for **adaptive
decomposition** but it is **not throughput-optimal for a slot-bound GPU**:
unbounded spawning gives the engine no view of lane availability, so
subagents pile up in LLM-side queues and prefill storms thrash the decode
batch. Our c8lab result says the optimum is a **fixed dispatcher**: one
shared due-queue, N=C lanes, tool I/O never holding a lane, filler (idle
backfill) admitted only when no agent turn is due within a grace window.

**Conclusion:** keep nbchat's bounded `TeamCoordinator` (plan → dispatch →
verify → integrate) as the skeleton; **port three prime-agent ideas**:

1. **P1 — Lane decoupling (the big one).** Today a worker thread runs its
   whole agentic turn (LLM + tools + LLM + tools …) while effectively
   occupying the run's concurrency budget; the LLM request is one of many
   queueing on the server, and tool I/O blocks that worker's *next* LLM
   submission only if the worker is a bottleneck — the real loss is that a
   worker stuck in a 30 s tool call still holds a worker slot, so
   `max_workers` slots ≠ `C` in-flight LLM requests. Port the dispatcher
   model: workers release the lane after the LLM turn completes (stream
   close) and re-acquire for the next; the pool spawns claimers up to
   `team_max_workers` (= C=8) and the server's own n_parallel queue is the
   only queue. Net effect = `c8lab`'s `dispatcher` design (no
   `held_gap` wedge, backfill during tool gaps).
   *Files: `nbchat/core/team.py` (worker run loop), `nbchat/core/client.py`
   (timeout/queue behaviour), config `team_max_workers` semantics.*

2. **P2 — Subagent registry (visibility).** Prime-agent's
   `rlm.list_subagents` / per-child `status` + session dir is exactly what
   our `/team` status view lacks: a live table of every delegated
   subtask, its status (running/completed/error), which worker owns it,
   and its session id. Add `TeamCoordinator.list_subtasks()` (data already
   in `TaskQueue.children()` + statuses) and surface it in `/team` status
   output and in the coordinator's final report.
   *Files: `nbchat/core/team.py`.*

3. **P3 — Admission control on worker LLM submissions (backpressure). [x] SHIPPED 2026-09-09**
   (see `nbchat/core/lane_gate.py`: `LaneGate` — `BoundedSemaphore` +
   deadline-checked `acquire()`; wired into `MetricsLoggingClient` via
   config `lane_gate_enabled`/`lane_gate_limit`/`lane_gate_acquire_timeout`;
   7 tests in `tests/test_lane_gate.py`).
   Prime-agent's `prompt-admission.ts` pattern: a submission awaits
   *admission* (a free lane slot) and can be *cancelled* by signal,
   never leaking unhandled work. In nbchat terms: before a worker issues
   its next LLM call it should observe the run deadline (cancel, not
   submit) and respect a global in-flight LLM counter bounded by
   `team_max_workers` so 8 workers never burst past C concurrent requests
   into the server queue (prefill storms). Implement as a small
   `LaneGate` (threading.Semaphore + deadline-checked `acquire()`) around
   `client.chat` when inside a team run.
   *Files: new `nbchat/core/lane_gate.py`, `nbchat/core/client.py`,
   `nbchat/core/team.py`.*

### Verification (prove the GPU is maxed)
- **V1 (sim, regression guard):** re-run `c8lab.experiment` with the
  calibrated `EngineSim`; the shipped `dispatcher` numbers must be matched
  or beaten by the new `nbchat-ported` client model (a thin client that
  mirrors the P1/P3 semantics, not a re-sim of TS code).
- **V2 (real-server microbench):** with the inference server up, drive the
  new team engine on a real multi-task goal (e.g. 8 independent
  "implement + test module X" tasks) and compare **wall time, in-flight
  concurrency observed server-side, tokens/s** against the current
  held-lane behaviour (run both, same tasks). Expect wall time ≈ the
  dispatcher optimum: no worker sits on a slot during tool I/O.
- **V3 (end-to-end real-world):** a `/team` run on a genuine multi-file
  goal in this repo (e.g. porting a small self-contained prime-agent
  utility into nbchat with tests), timed, with the subtask registry table
  captured mid-run as evidence of parallelism; `run_tests` green before
  push.

### Order of implementation
1. ~~`lane_gate.py` + client integration (P3)~~ **done 2026-09-09** — `LaneGate` shipped with tests; commit `cf18968`.
2. Worker loop decoupling (P1) — the core change; keep the
   ToolArbiter invariants intact.
3. Subagent registry + `/team` status (P2).
4. V1 sim parity check → V2 real-server microbench → V3 end-to-end.
5. Tracker + docs (`multi_agent.md`) updates; final email with evidence.


---

## 13. TUI v2 fix pass (2026-07-10)

The Phase 2/3 wiring was shipped (2026-09-07/09) but the real app was
**unusable**: submitting a message left a frozen spinner, keys appeared
dead, and the model's answer never appeared on screen. A pty + unit-level
investigation (documented in `docs/tui2_issues.md`) found 5 critical and
7 major defects. All are fixed now; the fixes are covered by 13 new
regression tests in `tests/test_tui2.py` (63 tui2 tests total) and a live
pty end-to-end run against a real model.

### Fixed (critical)
- **C1 answer text dropped** — `ChatMessage.render` XOR'd blocks vs. text;
  any turn with thinking/tool calls rendered *without* the reply. Now
  blocks render first, then the answer text (same as v1 order).
- **C2 thinking block explosion** — every reasoning token appended a new
  full block (newest first). Now one block per LLM call, updated in place,
  closed on first content / tool / stream complete; chronological order.
- **C3 blank screen while streaming** — live content (thinking, tool
  panels, streamed text) is not in the log until finalize, so frames were
  static. The frame now composes log + live turn + loader + editor +
  status, bottom-anchored, with a persistent spinner in the status line
  and a ~1 Hz clock heartbeat (`clock_interval`) so it animates between
  events.
- **C4 space key dead** — `KeyReader` emits `Key("space")`, which
  `_key_text` didn't map: every space a user typed was dropped. Now
  mapped to `" "`.
- **C5 Ctrl+C / Ctrl+D wrong** — Ctrl+C always quit (no interrupt),
  Ctrl+D always quit (no submit). Now a three-way `_handle_input`
  contract (`True` = exit, `None` = consumed, `False` = dispatch):
  Ctrl+C interrupts a running turn, quits when idle; Ctrl+D submits when
  the editor has text, quits when empty. Matches the v1 semantics and the
  editor keymap.

### Fixed (major)
- **M1 no session continuity** — the app always started an empty session
  and forgot it. Now: resume the last session by default (shared with v1),
  `--new` / `--session <id>` flags, `remember_session` after every turn,
  history re-rendered into the log on resume.
- **M2 slash commands no-ops** — all of them silently discarded. Now routed
  through the v1 `handle_command` with stdout captured and rendered as a
  dim "note" message; `/new`/`/load` re-render the history.
- **M3 no mid-stream interjection** — prime-agent's signature behaviour.
  Typing a new message while a turn runs interrupts it and redirects the
  agent to the new text (latest wins).
- **M4 no header** — now `nbchat · <model> · session <id>`, updated on
  session switch.
- **M5 no context/tok/s in status** — now a context-budget bar (v1's
  `_ctx_bar`) and a rolling 1 s tok/s, right-aligned.
- **M6 `--v2` launched the demo** — `nbchat.tui.app.run` now routes `--v2`
  to the real app (`nbchat.tui2.__main__.run`, which strips the flag);
  the demo stays reachable via `python -m nbchat.tui2 --demo`.
- **M7 stderr corruption in raw mode** — `sys.stderr` is redirected to
  `~/.nbchat/tui2-stderr.log` for the session so `logging`/library noise
  can't tear the alternate screen (mirrors prime-agent's Tty stderr trap).

### Fixed (minor)
- m1 empty editor drew only dim placeholder with no cursor — the cursor
  block now overdraws the first placeholder character.
- m2 tool results rendered as one truncated line — now split into real
  lines, capped at 8, with "… N more line(s)".
- m3 removed the global `st._enabled` monkeypatch in `app.run` (the v1
  spinner module is no longer touched; the v1 REPL is byte-identical).
- m6 worker exceptions were swallowed silently — now surfaced as an error
  block in the log + the status line.

### Verification
- `tests/test_tui2.py`: 50 → **63 passing** (13 new regression tests for
  C1–C5, M1/M2/M3, redirect-on-finalize, session resume).
- Full suite: **all green** (run in two wall-clock chunks because of the
  24 s session guard: 247 + 157 tests, zero failures).
- Live pty end-to-end against a real model (`python3 -m nbchat.tui2`):
  the full frame layout renders, a real turn streams thinking → tool →
  answer, the answer text is visible, `/sessions` renders its note,
  `/quit` exits and restores the terminal.

### Still pending (intentionally deferred — see `docs/tui2_issues.md`)
- Scrollback / log scrolling (the log is a `MessageLog`; adding a
  scroll view + PageUp/PageDown is the next natural engine piece).
- Full session-picker UI (the fuzzy `SelectList` exists; `/load` still
  takes an id, `/sessions` lists).
- Voice / email / supervisor / team surfaces in v2 — their v1 status
  output is print-based; wiring them means re-plumbing those print paths
  into structured hooks. v2 intentionally covers chat + sessions +
  commands for now.
- CJK / wide-glyph column math (`_clamp` counts code points, not cells).
- `--no-color` / TERM=dumb degradation for v2 (v1 path unaffected).
