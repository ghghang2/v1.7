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
1 → 2 → 3 → (4, 5 in parallel) → 6 → 7.

## tui3 wave 1 (this pass)

**Keymap substrate + browse mode + contextual mode bar.** Purely additive —
no existing binding changes:

- A single data-driven `KEYMAP` (the source of truth) generates both the
  `/hotkeys` reference and the new **mode bar**, so they can never desync.
- A **mode bar** (one line, above the status line) shows the active mode and
  its exact keys.
- **Browse mode**, toggled with **Ctrl+O** (free today; Ctrl+B is the
  editor's backward): `j`/`k` scroll the log, `Home`/`End` jump, `Esc` exits.
  In-log `/` search and `v` visual copy land in wave 2.

Waves 2+ (browse search/copy, mouse, config/settings, theming, socket API,
detach) proceed in the build order above.
