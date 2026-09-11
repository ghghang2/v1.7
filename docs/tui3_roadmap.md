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
| 7 | **Detachable background agent** (reattach after disconnect) | 10 | L | Move the agent/turn worker into a long-lived supervised process; session + in-flight stream + pending approvals survive TUI exit / SSH drop. The headline "detach without stopping work". |

**Build order** (follows herdr's, adjusted for what tui2 already has):
1 → 2 → 3 → (4, 5 in parallel) → 6 → 7.  **Shipped so far:** wave 1
(keymap + browse + mode bar), wave 2 (in-log search + visual copy),
wave 3 (mouse wheel scroll).  Next: click-to-select, then config/settings
and theming.

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

Waves 4+ (click-to-select, config/settings, theming, socket API, detach)
proceed in the build order above.
