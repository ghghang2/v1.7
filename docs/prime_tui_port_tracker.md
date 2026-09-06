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
| `nbchat/ui/*.py` | ~2.8k | Rich-based chat renderer (existing) |

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
3. **Dependencies.** Keep the footprint small and standard. Preferred:
   `termios`/`tty` (stdlib, raw mode), and **`rich`** for Markdown + code
   highlighting where it saves re-porting `marked`. `rich` is already in the
   dependency set (used by `nbchat/ui`), so no new install is expected.
   (Confirm `rich` availability before first use; if absent, fall back to a
   minimal ANSI markdown renderer.)
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
- [ ] Component base + layout: `Text`, `Box`, `Spacer`, `Container`.
- [x] Event loop: thread-safe input + render-request queue; `requestRender()`.
- [~] Bracketed paste + basic key parsing (arrows, Ctrl+C, Ctrl+D, Enter).
- [ ] `StatusLine`/footer component (model, tokens/s, session).
- [ ] `Loader`/spinner component (agent working state).
- [ ] **Smoke test (pty):** prove flicker-free diff updates — assert only
      changed lines are re-emitted, and that raw mode restores cleanly on exit.
- [ ] **User-test checkpoint 1** (opt-in flag): banner + status line + loader.

### Phase 2 — Conversation surface  · est. ~1.5k LOC
- [ ] `Markdown` component (via `rich`), code highlighting.
- [ ] User/assistant message components (bubbles, backgrounds).
- [ ] Tool-call panels with diff colouring (add/remove lines).
- [ ] Thinking blocks (collapsible/dimmed).
- [ ] Wire all existing `/` commands into the new input line (same semantics).
- [ ] `SelectList` (fuzzy) for `/sessions`, `/model`.
- [ ] Port theme JSONs → theme module.
- [ ] **User-test checkpoint 2**: full chat in the new surface.

### Phase 3 — Editor polish  · est. ~1k LOC
- [ ] Multi-line `Editor` component: undo/redo, kill-ring, backslash continue.
- [ ] Slash-command + path autocomplete.
- [ ] Fuzzy search in selectors.
- [ ] (Optional) mouse support.
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
- (none yet)

### Pending
- Phase 1 (remaining unchecked items in the Phase 1 list above)
- Phase 2 (all items above)
- Phase 3 (all items above)

### Blocked
- (none yet)

### User-test checkpoints
| # | What to test | How | Status |
|---|--------------|-----|--------|
| 1 | Banner, status line, loader, clean exit | *(TBD at checkpoint)* | Pending |
| 2 | Full chat in new surface | *(TBD at checkpoint)* | Pending |
| 3 | Editor feel | *(TBD at checkpoint)* | Pending |

---

## 7. Issues & resolutions log

| Date | Issue | Resolution |
|------|-------|------------|
| 2026-09-06 | Node runtime not installed; can't consume `pi-tui` as a library from Python. | Reimplement the engine in Python (Phase 1); port concepts 1:1. |
| 2026-09-06 | prime-agent interactive mode is tightly coupled to its own agent core — not reusable over an external HTTP agent. | Build nbchat-specific components in Phase 2, reusing prime-agent's *visual/UX* design, not its code. |

---

## 8. Decisions log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-09-06 | Additive, opt-in new TUI; existing REPL stays default and unchanged. | User hard rule: no breaking changes. |
| 2026-09-06 | Reimplement engine in Python rather than run Node/TS. | Node not available; keeps one runtime and small footprint. |
| 2026-09-06 | Use `rich` for Markdown/code highlighting. | Already in deps via `nbchat/ui`; avoids re-porting `marked`. |
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
