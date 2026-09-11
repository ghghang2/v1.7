"""The TUI v2 chat application (Phase 2/3).

:class:`ChatApp` is a :class:`nbchat.tui.agent.TerminalAgent` subclass whose
terminal-output hooks are re-routed from ``sys.stdout`` into the structured
render tree (:class:`~nbchat.tui2.chat.ChatLog`) per the §11.4.2 decision:

- the print-based status bar and ``_status_*`` hooks are overridden so they
  feed the tui2 status line instead of printing (no double-fire in raw mode);
- ``_run_turn``\'s ``printer`` seam feeds the user turn into the log;
- live streaming (thinking, tool panels, answer text) is rendered into the
  frame *while* the turn runs — the screen never freezes mid-turn;
- the existing REPL (``nbchat.tui``) is byte-for-byte unchanged — nothing is
  subclassed or patched, only a new entry point is added.

The agentic turn runs on a daemon worker thread (serialized by the
agent\'s own ``_send_lock``) so the UI event loop stays responsive while
streaming.  ``Enter`` submits (single-line semantics); ``Esc`` interrupts an
in-flight turn; ``Ctrl+C`` interrupts when a turn is running and quits when
idle; ``Ctrl+D`` submits when the editor has text and quits when it is
empty.  Slash commands (``/help``, ``/new``, ``/sessions``, ``/quit``, …)
are handled by the shared ``nbchat.tui.app.handle_command`` with output
captured into the log.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import List

from . import chat as chatc
from .components import Box, Container, Loader, StatusLine, Text, _SPINNER_FRAMES, blank
from .editor import LineEditor
from .frame import Frame, Line, Segment, Style
from .fuzzy import fuzzy_rank
from .theme import DARK
from . import theme
from .keys import Key
from .keys import KeyReader
from .raw import EventQueue, RawTerminal, TUIApp
from .notify import NotifyStack
from . import config
from nbchat.tui.agent import TerminalAgent

# <tool_call> blocks leak through the stream when the model emits them as
# text instead of structured tool calls; keep the log clean.
_TOOL_TEXT_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL)

# Cap on visible tool-result lines inside a panel (v1 shows ~the same).
_TOOL_RESULT_LINES = 8

# ── Project instructions (AGENTS.md / CLAUDE.md auto-load) ────────────
# Auto-load repo convention files into the tui2 system prompt so the agent
# follows project-specific rules without the user pasting them.  Best-effort
# and opt-out via NBCHAT_NO_PROJECT_INSTRUCTIONS=1.  Looks in the working
# directory first, then the git repo root.
_PROJECT_INSTR_NAMES = ("AGENTS.md", "agents.md", "CLAUDE.md", "claude.md")
_PROJECT_INSTR_CAP = 16384


def _find_project_instructions(cwd: str | None = None) -> str:
    """Return the path to the first project-instruction file (AGENTS.md /
    CLAUDE.md) in the working directory, then the git repo root, or '' if
    none is found.  Best-effort: never raises."""
    if os.environ.get("NBCHAT_NO_PROJECT_INSTRUCTIONS"):
        return ""
    try:
        base = cwd or os.getcwd()
    except Exception:
        base = os.getcwd()
    roots = [base]
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           cwd=base, capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            toplevel = r.stdout.strip()
            if toplevel and toplevel not in roots:
                roots.append(toplevel)
    except Exception:
        pass
    for root in roots:
        for name in _PROJECT_INSTR_NAMES:
            p = os.path.join(root, name)
            if os.path.isfile(p):
                return p
    return ""


def _load_project_instructions(path: str) -> str:
    """Read a project-instruction file (capped, best-effort) and wrap it in a
    system-prompt marker.  Returns '' on any problem."""
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        return ""
    if not text.strip():
        return ""
    if len(text) > _PROJECT_INSTR_CAP:
        text = text[:_PROJECT_INSTR_CAP] + "\n\u2026 (truncated)"
    try:
        rel = os.path.relpath(path)
    except Exception:
        rel = path
    return ("\n\n[PROJECT INSTRUCTIONS \u2014 " + rel + "] (auto-loaded, obey these)\n"
            + text + "\n[/PROJECT INSTRUCTIONS]")

# ── Keymap substrate (tui3 wave 1) ────────────────────────────────────
# Single source of truth for the keybindings.  It generates BOTH the
# ``/hotkeys`` reference and the on-screen **mode bar**, so the two can
# never desync.  Each mode is a tuple of ``(key, description)`` rows.
KEYMAP = {
    "normal": (
        ("enter", "send message"),
        ("ctrl+d", "send (quit when input empty)"),
        ("esc", "interrupt turn / close modal"),
        ("ctrl+c", "interrupt (busy) or quit (idle)"),
        ("ctrl+o", "toggle browse mode"),
        ("ctrl+t", "show / hide thinking blocks"),
        ("ctrl+l", "session picker (or bare /load)"),
        ("ctrl+p", "command palette"),
        ("ctrl+r", "reverse search input history"),
        ("up/down", "recall the previous / next input"),
        ("pgup/pgdn", "scroll the log up / down"),
        ("wheel", "scroll the log (mouse)"),
        ("click/drag", "copy one / a range of log lines"),
        ("home/end", "jump to the top / bottom of the log"),
        ("!<cmd>", "run a shell command (!! stores output)"),
    ),
    "browse": (
        ("j / k", "scroll down / up"),
        ("pgup / pgdn", "page down / up"),
        ("home / end", "jump to the top / bottom"),
        ("/", "search the log (n / N next / prev)"),
        ("v", "copy the visible log to clipboard"),
        ("esc / ctrl+o", "leave browse mode"),
    ),
}

# How many key hints the mode bar shows for the active mode.
_MODEBAR_HINTS = 5


class _TeamCapture:
    """Capture a background team run's ``sys.stdout`` and relay it (batched)
    into the TUI log via ``"call"`` events.

    A team run's workers and coordinator write their output to the global
    ``sys.stdout``.  In raw mode that is the terminal the TUI paints its
    frames into, so unguarded worker output would corrupt the screen.  This
    object is dropped in as ``sys.stdout`` for the duration of the run: it
    swallows the output, holds complete lines, and — throttled by time and
    size — pushes a batch to the UI thread (which appends it as a dim note).
    The UI thread is never blocked and the screen is never corrupted.
    """

    def __init__(self, app, min_interval: float = 0.4, min_chars: int = 1200):
        self._app = app
        self._buf = ""
        self._lock = threading.Lock()
        self._min_interval = min_interval
        self._min_chars = min_chars
        self._last_emit = 0.0

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, data: str) -> int:
        if not data:
            return 0
        now = time.monotonic()
        batch = ""
        with self._lock:
            self._buf += data
            flush_all = len(self._buf) >= self._min_chars
            has_line = "\n" in self._buf
            if flush_all or (has_line
                             and (now - self._last_emit) >= self._min_interval):
                batch, self._buf = self._buf, ""
                self._last_emit = now
        if batch:
            # Deliver on the UI thread (the render loop runs "call" closures),
            # so the log is never mutated from the worker thread.
            self._app.events.put("call",
                                 lambda b=batch: self._app._team_emit(b))
        return len(data)

    def flush(self) -> None:
        """Ship any partial tail (called when the run ends)."""
        with self._lock:
            batch, self._buf = self._buf, ""
        if batch:
            self._app.events.put("call",
                                 lambda b=batch: self._app._team_emit(b))


class ChatApp(TerminalAgent):
    """A full conversation app: agent + chat log + input line.

    Subclasses :class:`~nbchat.tui.agent.TerminalAgent` directly so the
    print-based output hooks (``_status_set``, ``_print_user``,
    ``_on_stream_*``, ``_on_tool_display``, ``_on_agent_message``) resolve
    to the overrides below instead of writing to ``sys.stdout``.

    ``session_id`` resumes a specific session (resolved like ``/load``);
    ``resume_last=False`` forces a brand-new session (the ``--new`` flag).
    """

    def __init__(self, term: RawTerminal, events: EventQueue,
                 resume_last: bool = True,
                 session_id: str | None = None,
                 bg: bool = False,
                 supervisor: bool = False,
                 voice: bool = False) -> None:
        super().__init__(color=False)
        self.term = term
        self.events = events
        # Todo/progress pill: nudge the LLM to keep a short task list
        # (surfaced as a live pill in the status bar + the /todos command).
        _tn = self._TODO_NOTE
        if _tn and _tn not in self.system_prompt:
            self.system_prompt += _tn
        # Project instructions (AGENTS.md / CLAUDE.md): auto-load repo
        # conventions into the system prompt (best-effort, tui2-only,
        # opt-out via NBCHAT_NO_PROJECT_INSTRUCTIONS=1).
        _pi_path = _find_project_instructions()
        self._project_instr_path = _pi_path
        if _pi_path:
            _pi = _load_project_instructions(_pi_path)
            if _pi:
                self.system_prompt += _pi
        # Always-on supervisor watchdog (v1 --supervisor parity).  Bound to
        # this agent; started/stopped by run().  ``None`` when disabled.
        self._supervisor_enabled = supervisor
        self._supervisor = None
        # Alfred voice bridge (v1 --voice parity).  The laptop client reaches
        # the bridge over an SSH tunnel; inbound transcripts are auto-submitted
        # as user turns.  ``_voice_bridge`` is ``None`` when disabled.
        self._voice_enabled = voice
        self._voice_bridge = None

        # Session continuity (v1 parity): resume the last session by
        # default, a specific one when given, or a fresh one on request.
        if session_id:
            resolved = self.resolve_session(session_id)
            if resolved is None:
                raise ValueError(
                    f"no such session: {session_id!r}  (see /sessions)")
            self._switch_session(resolved)
        elif resume_last:
            last = TerminalAgent.last_session()
            if last:
                self._switch_session(last)
        self.remember_session(self.session_id)

        self.editor = LineEditor(
            placeholder="Type a message…  (enter sends · esc interrupts · /help)",
            multiline=False,
        )
        self.log = chatc.ChatLog()
        self._populate_history()

        # Status fed by the overridden agent hooks.
        self._status_state = "ready"
        self._status_detail = ""
        self._ctx_used = 0.0
        self._ctx_budget = 0.0
        self._auto_compact_due = False
        self._auto_compact_frac, self._auto_compact_enabled = self._auto_compact_cfg()
        self._last_user_text = None
        self._queue: list = []
        self._tok_times: list = []
        self._turns = 0
        self._turn_mutated = False  # auto-checkpoint once per edit-window
        # Recurring instruction (/heartbeat): fired as a turn every N seconds
        # while the session is idle.  "" = disabled.
        self._heartbeat: str = ""
        self._heartbeat_interval: float = 0.0
        self._heartbeat_last: float = 0.0
        self._plan_mode = False     # read-only research mode (blocks edits)
        self._file_mutating_tools = {"create_file", "make_change_to_file",
                                     "run_command"}
        self._loader = Loader("ready")

        # Live streaming turn.  Blocks accumulate in chronological order;
        # the *current* thinking block is updated in place as reasoning
        # streams (one block per LLM call, never one per token).
        self._stream_blocks: list[chatc.ChatBlock] = []
        self._stream_text = ""
        self._reasoning_printed = ""
        self._content_printed = ""
        self._thinking_open = False
        self._cur_thinking: chatc.ChatBlock | None = None

        # Pending interjection: a message typed while a turn is running;
        # it interrupts the turn and runs as soon as it winds down.
        self._redirect: str | None = None

        # Thinking blocks: Ctrl+T toggles whether reasoning is shown.
        self._thinking_visible = True

        # Session picker modal (Ctrl+L / no-arg /load): None when closed.
        # The same widget slot backs three modal kinds (see _modal_kind):
        #   "session" — load a saved session
        #   "palette" — Ctrl+P: jump to any slash command
        #   "search"  — Ctrl+R: search the input history
        self._picker = None          # SelectList
        self._picker_filter = ""     # fuzzy filter text
        self._picker_sessions: list = []   # raw selectable rows
        self._modal_kind = "session" # which modal is (or was) open
        # Input history for Ctrl+R reverse search (turns + commands).
        self._history: list = []
        # Arrow-key history recall (Up/Down).  ``_hist_pos`` is ``None``
        # while free-typing; otherwise the index into ``_history`` of the
        # entry currently recalled.  ``_hist_draft`` holds the text being
        # typed when recall starts, so Down past the newest restores it.
        self._hist_pos: int | None = None
        self._hist_draft = ""

        # Tool-approval gate (herdr-style safety): risky tool calls prompt
        # the user before running.  The gate wraps the module-level
        # ``tool_executor.run_tool`` (the same seam ``team.py``'s
        # ToolArbiter uses) and is installed/removed in ``run()``.
        self._approval_enabled = True
        # External-effect / mutating tools that must be confirmed.
        self._risky_tools = {"run_command", "push_to_github", "send_email"}
        self._approval = None              # pending approval modal state
        self._approval_orig = None         # original run_tool (for restore)

        # /goal — a running objective the app keeps auto-continuing toward
        # (prime-agent style): after each turn it chains the next turn until
        # the turn budget is exhausted, the model declares completion, or the
        # user runs ``/goal stop``.
        # In-TUI notification stack (toasts + BEL + optional sound).
        self._notify = NotifyStack()

        # Browse mode (tui3): a key-capturing mode for reading/scrolling the
        # log without typing into the editor.  Toggled with Ctrl+O.
        self._browse = False
        # In-log search (tui3 wave 2): active while a query is being typed
        # (``input`` True) or matches are being cycled through (``input``
        # False).  ``_search_matches`` = [(line_idx, snippet), ...].
        self._logsearch = None
        self._search_matches = []
        self._search_idx = 0
        # @-file completion (tui3): active while an ``@token`` sits
        # before the cursor.  ``_filecomp`` = {query, matches, idx} or None;
        # ``_file_list_cache`` = (cwd, [rel paths]) so the tree is walked
        # once per cwd, not per keystroke.
        self._filecomp = None
        self._file_list_cache = None
        # Click / drag-to-copy (tui3 wave 3b): a mouse press in the log
        # region records the anchor frame row; a release copies the range
        # [anchor .. release] (a single click copies one line).  The copy
        # is taken from the last rendered frame so no log-index math is
        # needed.
        self._sel_anchor = None  # 1-based frame row, or None
        self._last_frame_rows: list = []
        self._last_log_end = 0  # 1-based last row of the log/live region

        self._goal = None
        self._goal_budget = 20  # default auto-continue turn budget
        # Autonomous approval gate (/autonomous): when True, the goal
        # auto-continue PAUSES after each turn and prompts the user instead
        # of silently chaining.  Off by default (/goal keeps auto-chaining).
        self._autonomous_gate = False
        # Phrases the model can emit to mark the goal achieved.
        self._goal_done_markers = ("goal complete", "goal: complete",
                                   "goal achieved", "[goal done]",
                                   "goal accomplished")

        # Team (multi-agent) run bookkeeping.  A background team run streams
        # its output through a _TeamCapture that is relayed into the log.
        self._team_state: dict = {
            "thread": None,        # live worker thread (None when idle)
            "coordinator": None,   # TeamCoordinator for the current/last run
            "capture": None,       # _TeamCapture holding the run's stdout
            "goal": "",            # goal text of the current/last run
            "status": "idle",      # idle | running | done | failed | stopped
            "report": "",          # final coordinator report (last run)
            "started": 0.0,        # time.monotonic() when the run started
            "stopped": False,      # True once the user requests a stop
        }

        # Turn worker bookkeeping.
        self._turn_thread: threading.Thread | None = None

        self._tui = TUIApp(term, events, bg=bg)
        self._tui.set_frame_provider(self._build_frame)
        self._tui.set_key_reader(KeyReader())
        self._tui.on_input = self._on_input
        # ~1 Hz heartbeat so the busy spinner advances between events.
        self._tui.clock_interval = 1.0

        # ── Persisted user settings (tui3 wave 4) ──────────────────────
        # Apply the user's saved preferences to the state above.  Loading
        # is defensive (config.load never raises); saving is best-effort.
        self._cfg = config.load()
        self._thinking_visible = bool(self._cfg.get("thinking_visible", True))
        self._notify.toasts = bool(self._cfg.get("notify_toasts", True))
        self._notify.bel = bool(self._cfg.get("notify_bel", True))
        self._notify.sound = bool(self._cfg.get("notify_sound", False))
        self._approval_enabled = bool(self._cfg.get("approval_enabled", True))
        self._risky_tools = set(
            self._cfg.get("risky_tools",
                          ["run_command", "push_to_github", "send_email"]))
        self._scroll_tick = max(1, int(self._cfg.get("scroll_tick", 3) or 3))
        # Active colour theme (persisted); the proxy in theme.py retargets
        # so every component follows it from this point on.
        config.set_theme_active(self._cfg.get("theme", "dark"))

    # ── session / history ───────────────────────────────────────────────

    def _populate_history(self) -> None:
        """Render prior user/assistant rows into the log on (re)start."""
        try:
            rows = list(self.history)
        except Exception:
            return
        for row in rows:
            role, content, _tid, _tname, _targs, _ef = row
            content = (content or "").strip()
            if not content or role not in ("user", "assistant"):
                continue
            self.log.add(chatc.ChatMessage(role=role, text=content))

    def _session_changed(self) -> None:
        """Reset all per-session UI state after /new or /load."""
        self.log = chatc.ChatLog()
        self._stream_blocks = []
        self._stream_text = ""
        self._reasoning_printed = ""
        self._content_printed = ""
        self._close_thinking()
        self._redirect = None
        self._turns = 0
        self._populate_history()

    # ── agent hook overrides (print → render tree) ──────────────────────

    def _status(self):  # override: no print status bar in tui2
        return None

    def _status_set(self, state: str, detail: str = "") -> None:
        self._status_state = state
        self._status_detail = detail
        self._ui_refresh()

    def _status_window(self, estimated_tokens: int, budget: int) -> None:
        self._ctx_used = float(estimated_tokens)
        self._ctx_budget = float(budget)
        # Auto-compact trigger: flag that the window is over the
        # threshold; the actual compact runs at a safe post-turn point
        # (_finalize_turn) so it never interrupts an in-flight turn.
        if self._ctx_budget > 0:
            self._auto_compact_due = ((self._ctx_used / self._ctx_budget)
                                      >= self._auto_compact_frac)
        else:
            self._auto_compact_due = False
        self._ui_refresh()

    @staticmethod
    def _auto_compact_cfg():
        """(threshold, enabled) for auto-compact from NBCHAT_AUTO_COMPACT.

        Default (unset): enabled at 0.80 of the context budget.  A float in
        (0, 1] sets the threshold (and enables); ``0``/``off``/``false``
        disables it; ``1``/``on``/``true`` enables at the 0.80 default.
        """
        import os as _os
        raw = (_os.environ.get("NBCHAT_AUTO_COMPACT", "") or "").strip().lower()
        if raw in ("", "1", "on", "true", "yes"):
            return 0.80, True
        if raw in ("0", "off", "false", "no"):
            return 0.80, False
        try:
            frac = float(raw)
        except ValueError:
            return 0.80, True
        if frac <= 0.0:
            return 0.80, False
        return min(frac, 1.0), True

    def _maybe_auto_compact(self) -> None:
        """Compact the context off-thread when it crossed the threshold.

        Called from _finalize_turn (post-turn, nothing chaining a new turn).
        Skips when disabled, not flagged, or a turn is already running.
        Never raises; the note is delivered on the UI thread via a "call".
        """
        if not (self._auto_compact_enabled and self._auto_compact_due):
            return
        if self.busy:
            return  # a new turn is already running; try again next turn
        self._auto_compact_due = False  # handle this flag now
        events = self.events

        def _worker():
            # Let the just-finished turn release the send lock first.
            import time as _t
            for _ in range(30):
                if not self.busy:
                    break
                _t.sleep(0.1)
            if self.busy:
                return  # a new turn started; defer to its finalize
            try:
                rep = self.force_compact("auto: context near budget")
            except Exception as exc:
                note = f"auto-compact: skipped ({type(exc).__name__})"
                events.put("call", lambda n=note: self._note(n))
                return
            if not rep.get("compacted"):
                note = f"auto-compact: {rep.get('reason', 'nothing to compact')}"
                events.put("call", lambda n=note: self._note(n))
                return
            try:
                from nbchat.tui.status import _humanise as _h
                note = (f"auto-compact: {rep['before_rows']}\u2192{rep['window_rows']} "
                        f"rows \u00b7 ~{_h(rep['before_tokens'])}"
                        f"\u2192~{_h(rep['after_tokens'])} tok "
                        f"(budget {_h(rep['budget'])})")
            except Exception:
                note = "auto-compact: context compacted to the budget"
            events.put("call", lambda n=note: self._note(n))

        threading.Thread(target=_worker, daemon=True).start()

    def _queue_key(self) -> None:
        """Ctrl+Q: queue the current draft to run after the active turn.

        While a turn is in flight the draft is appended to the queue (editor
        cleared) and a note shows the pending count; _finalize_turn runs them
        in order.  If no turn is running it just submits normally.
        """
        text = self.editor.text().strip()
        if not text:
            if self._queue:
                self._note(f"queue: {len(self._queue)} pending (type a "
                           f"message then Ctrl+Q to add)")
            else:
                self._note("queue: nothing to queue (editor is empty)")
            self._ui_refresh()
            return
        if self.busy:
            self._queue.append(text)
            self.editor.clear()
            self._note(f"queued (now {len(self._queue)} pending) — will run "
                       f"after the current turn")
        else:
            self.editor.clear()
            self._submit(text)
        self._ui_refresh()

    def _process_next_queued(self) -> None:
        """Run the next queued follow-up after a turn fully winds down.

        Called from _finalize_turn when nothing chained a new turn.  Uses the
        same _turn_thread=None trick as the redirect path: we're inside the
        winding-down worker thread (its is_alive() is still True), so clear
        the handle to make _start_turn take the fresh-turn branch.
        """
        if not self._queue:
            return
        self._turn_thread = None
        nxt = self._queue.pop(0)
        remaining = len(self._queue)
        self._note(f"queue: running your queued message ({remaining} "
                   f"still pending)")
        if self._tui._running:
            self._start_turn(nxt)

    def _cmd_queue(self, arg: str) -> str:
        """Show or clear the steering queue (Ctrl+Q adds to it)."""
        if arg.strip().lower() in ("clear", "c", "x", "reset"):
            n = len(self._queue)
            self._queue.clear()
            return f"queue: cleared {n} message(s)"
        if not self._queue:
            return "queue: empty (Ctrl+Q queues a draft while a turn runs)"
        rows = [f"  {i + 1}. {t[:60]}" for i, t in enumerate(self._queue)]
        return f"queue: {len(self._queue)} pending" + chr(10) + chr(10).join(rows)

    def _tpl_dir(self) -> str:
        """Where prompt-template *.md files live."""
        env = os.environ.get("NBCHAT_PROMPTS_DIR")
        if env:
            return env
        return os.path.join(os.path.expanduser("~"), ".nbchat", "prompts")

    def _prompt_templates(self) -> dict:
        """Load prompt templates (one per *.md file; name = filename stem)."""
        d = self._tpl_dir()
        out: dict = {}
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".md"):
                    continue
                try:
                    with open(os.path.join(d, fn), encoding="utf-8") as f:
                        out[fn[:-3]] = f.read()
                except OSError:
                    continue
        return out

    def _render_template(self, body: str, args: str) -> str:
        """Substitute $1/$2/... args, $0 or $ARG = all args; flatten to one line."""
        toks = args.split()

        def _sub(m):
            n = m.group(1)
            if n == "0":
                return " ".join(toks)
            idx = int(n)
            return toks[idx - 1] if 1 <= idx <= len(toks) else m.group(0)

        out = re.sub(r"\$(\d)", _sub, body).replace("$ARG", " ".join(toks))
        return " ".join(out.split())

    def _cmd_tpl(self, arg: str) -> str:
        """Prompt templates: /tpl lists them; /tpl <name> [args] sends one."""
        parts = arg.strip().split(None, 1) if arg.strip() else []
        tpls = self._prompt_templates()
        if not parts:
            if not tpls:
                return (f"no templates found (drop *.md files in "
                        f"{self._tpl_dir()}; NBCHAT_PROMPTS_DIR to override)")
            rows = [f"  /tpl {n}" for n in sorted(tpls)]
            return ("templates:" + chr(10) + chr(10).join(rows)
                    + chr(10) + "usage: /tpl <name> [args...]  ($1 $2 … $0/all)")
        name = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        if name not in tpls:
            have = ", ".join(sorted(tpls)) or "none"
            return f"no template '{name}' (available: {have})"
        body = self._render_template(tpls[name], rest)
        if self.busy:
            self._queue.append(body)
            return f"queued template '{name}' (now {len(self._queue)} pending)"
        self._start_turn(body)
        shown = body[:60] + ("…" if len(body) > 60 else "")
        return f"sent template '{name}': {shown}"

    def _launch_external_editor(self) -> None:
        """Open the current draft in $EDITOR, then load the result back.

        The tui2 editor is single-line, so composing a long prompt in it is
        awkward.  This pauses the raw terminal (restore -> normal mode), runs
        $EDITOR on a temp file seeded with the draft, reads the result back
        (flattened to one line), and re-enters raw mode.  Ctrl+E or /editor.
        """
        editor = (os.environ.get("EDITOR") or os.environ.get("VISUAL") or "").strip()
        if not editor:
            self._note("no $EDITOR/$VISUAL set — cannot open an external editor")
            self._ui_refresh()
            return
        was_raw = getattr(self.term, "_saved", None) is not None
        draft = self.editor.text()
        path = None
        try:
            fd, path = tempfile.mkstemp(prefix="nbchat_edit_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(draft or "")
            if was_raw:
                self.term._restored = False
                self.term.restore()
            try:
                subprocess.run(editor.split() + [path])
            finally:
                if was_raw:
                    self.term._restored = False  # so the final with-exit restore works
                    self.term.enter()
            with open(path, encoding="utf-8") as f:
                newtext = f.read()
            flat = " ".join(newtext.split())
            self.editor.set_text(flat)
            self._note("editor: loaded draft ({0} chars)".format(len(flat)) if flat
                       else "editor: (empty draft — Enter to send, Esc to discard)")
        except Exception as exc:
            self._note("editor error: {0}: {1}".format(type(exc).__name__, exc))
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        self._ui_refresh()

    def _cmd_editor(self, arg: str) -> str:
        self._launch_external_editor()
        return ""

    def _print_user(self, text: str) -> None:
        self.log.add(chatc.ChatMessage(role="user", text=text))
        self._ui_refresh()

    def _on_stream_reasoning(self, reasoning: str) -> None:
        if not reasoning:
            return
        # ``reasoning`` is cumulative *per LLM call*.  When it no longer
        # extends the previous call\'s text, a new call opened its own
        # thinking phase: keep the old block and open a fresh one.
        if (self._reasoning_printed
                and not reasoning.startswith(self._reasoning_printed)):
            self._reasoning_printed = ""
        if not self._thinking_open:
            blk = chatc.ChatBlock(kind="thinking", title="thinking",
                                  text=reasoning)
            self._stream_blocks.append(blk)
            self._thinking_open = True
            self._cur_thinking = blk
        else:
            self._cur_thinking.text = reasoning
        self._reasoning_printed = reasoning
        self._ui_refresh()

    def _on_stream_token(self, content: str) -> None:
        delta = content[len(self._content_printed):]
        if delta and self._thinking_open:
            self._close_thinking()
        if delta:
            # Run the delta through the voice-tag parser exactly like the
            # v1 REPL does: <voice> payloads must not leak into the UI.
            display, blocks = self._voice_parser.process(delta)
            if display:
                now = time.monotonic()
                self._tok_times.append(now)
                while self._tok_times and now - self._tok_times[0] > 1.0:
                    self._tok_times.pop(0)
                self._stream_text += display
            for block in blocks:
                lines = [ln for ln in block.splitlines() if ln.strip()][:6]
                if not lines:
                    lines = [block.strip()[:200]]
                self._stream_blocks.append(chatc.ChatBlock(
                    kind="tool", name="voice", title="[voice]",
                    status="done", body=lines,
                ))
            self._ui_refresh()
        self._content_printed = content

    def _on_stream_complete(self, content: str, tool_calls=None) -> None:
        if content:
            self._last_response = content
        held = self._voice_parser.flush_unclosed()
        if held:
            self._stream_text += held
        self._close_thinking()
        self._reasoning_printed = ""
        self._content_printed = ""
        # Fresh voice parser for the next LLM call (clean tag buffer).
        from nbchat.tui.agent import VoiceTagParser
        self._voice_parser = VoiceTagParser()
        if held:
            self._ui_refresh()

    def _on_tool_display(self, raw_result: str, tool_name: str,
                         tool_args: str) -> None:
        self._close_thinking()
        self._reasoning_printed = ""
        self._content_printed = ""
        hint = _arg_hint(tool_args)
        lines = _result_lines(raw_result, limit=_TOOL_RESULT_LINES)
        self._stream_blocks.append(chatc.ChatBlock(
            kind="tool", name=tool_name, title=f"{tool_name}({hint})",
            status="done", body=lines,
        ))
        self._ui_refresh()

    def _on_agent_message(self, text: str) -> None:
        """System notices (supervisor, refine, warnings) — v1 prints these
        in red; we render them as an error-styled panel.  Mid-turn the
        panel joins the live blocks; once the turn has finalised it is
        appended straight to the log so it persists (v1 parity: the
        notice stays on screen)."""
        self._status_set("error", text[:80])
        lines = [ln for ln in text.splitlines() if ln.strip()][:10]
        if not lines:
            lines = [text.strip()[:200]]
        block = chatc.ChatBlock(
            kind="tool", name="error", title="agent message",
            status="error", body=lines,
        )
        if self._turn_active and self._turn_thread is not None \
                and self._turn_thread.is_alive():
            self._stream_blocks.append(block)
        else:
            self._close_thinking()
            self.log.add(chatc.ChatMessage(
                role="assistant", text="", blocks=[block]))
        if not getattr(self, "_last_response", ""):
            self._last_response = text
        self._ui_refresh()

    def _close_thinking(self) -> None:
        self._thinking_open = False
        self._cur_thinking = None

    # ── turn worker ─────────────────────────────────────────────────────

    def _start_turn(self, text: str) -> None:
        self._turn_mutated = False  # fresh edit-window: allow an auto-checkpoint
        self.log.offset = 0  # the user just sent a message: follow the reply
        if self._turn_thread is not None and self._turn_thread.is_alive():
            # Mid-stream interjection (v1 semantics): stop the running turn
            # and send this message as soon as it winds down.  Latest
            # message wins if the user types again while one is pending.
            self._redirect = text
            self.interrupt()
            self._status_set("ready", "redirecting — stopping current response")
            self._ui_refresh()
            return
        self._turn_thread = threading.Thread(
            target=self._turn_worker, args=(text,), daemon=True)
        self._turn_thread.start()

    def _turn_worker(self, text: str) -> None:
        try:
            self.send(text)
        except KeyboardInterrupt:
            pass
        except Exception as exc:  # surface in the UI, don\'t kill the loop
            self._stream_blocks.append(chatc.ChatBlock(
                kind="tool", name="error", title=type(exc).__name__,
                status="error", body=[str(exc)[:300]],
            ))
            self._status_set("error", f"{type(exc).__name__}: {str(exc)[:60]}")
        finally:
            self._finalize_turn()

    def _goal_next_prompt(self):
        """Return the next auto-continue prompt, or ``None`` to stop.

        Called at the end of each turn (worker thread).  Advances the goal
        bookkeeping and returns a prompt when another auto-continue turn
        should run, else ``None`` (budget exhausted / stopped / completed).
        """
        g = self._goal
        if g is None or g.get("stopped"):
            return None
        if g["remaining"] <= 0:
            g["stopped"] = True
            self._note(f"goal: turn budget exhausted "
                       f"({g['budget']} auto-turns) — stopping")
            return None
        g["remaining"] -= 1
        g["done"] += 1
        progress = "(goal turn %d/%d)" % (g["done"], g["budget"])
        prompt = ("Keep working toward this goal: " + g["objective"]
                  + "\n" + progress + "\n"
                  + "If the goal is now fully achieved, reply with the exact "
                  + "line 'GOAL COMPLETE' plus a one-line summary; otherwise "
                  + "continue and do not stop early.")
        return prompt

    def _goal_declared_done(self, text: str) -> bool:
        """True when the model's reply marks the goal achieved."""
        if not text:
            return False
        low = text.lower()
        return any(m in low for m in self._goal_done_markers)

    def _goal_finish(self, msg_text: str) -> bool:
        """Handle a goal turn's completion.

        Returns ``True`` when a new continuation turn was started (silent
        auto-continue), ``False`` otherwise (declared done, approval-gate
        pause, or budget exhausted).  Lives here (not inline in
        ``_finalize_turn``) so the gate / done / chain decision is
        unit-testable in isolation.
        """
        g = self._goal
        if g is None or g.get("stopped"):
            return False
        if self._goal_declared_done(msg_text):
            g["stopped"] = True
            self._note("goal: model reported it complete - stopping "
                       "auto-continue")
            return False
        if self._autonomous_gate:
            # Approval gate: pause the chain and prompt the user.
            self._note(
                "autonomous gate: turn done (%d continuation turns used, "
                "%d auto-turns left) \u00b7 /autonomous go = continue \u00b7 "
                "/autonomous auto = keep going silently \u00b7 "
                "/autonomous stop = halt" % (g["done"], g["remaining"]))
            return False
        nxt = self._goal_next_prompt()
        if nxt is not None:
            self._turn_thread = None
            if self._tui._running:
                self._start_turn(nxt)
                return True
        return False

    def _finalize_turn(self) -> None:
        self._close_thinking()
        self.log.offset = 0  # a new reply lands: snap back to the bottom
        full_blocks = list(self._stream_blocks) or None
        msg = chatc.ChatMessage(
            role="assistant",
            text=_strip_markup(self._stream_text),
            blocks=(self._stream_blocks if self._thinking_visible
                    else [b for b in self._stream_blocks
                          if b.kind != "thinking"] or None),
        )
        msg._full_blocks = full_blocks
        if msg.text or msg.blocks:
            self.log.add(msg)
        self._stream_blocks = []
        self._stream_text = ""
        self._reasoning_printed = ""
        self._content_printed = ""
        self._tok_times = []
        self._turns += 1
        self._status_set("ready", "")
        try:
            self.remember_session(self.session_id)
        except Exception:
            pass
        self._ui_refresh()
        # Pending interjection: this (possibly interrupted) turn has fully
        # wound down and released the send lock — run the redirect now.
        # Clear the handle first: this code runs *inside* the worker
        # thread, whose ``is_alive()`` is still True until it returns, so
        # _start_turn would otherwise take the busy branch again.
        redirect = self._redirect
        self._redirect = None
        chained = False
        if redirect:
            self._turn_thread = None
            if self._tui._running:
                self._start_turn(redirect)
                chained = True
        elif self._goal is not None and not self._goal.get("stopped"):
            # Goal auto-continue (only when the user has not redirected this
            # turn).  Declared-done / approval-gate / silent-chain decision
            # lives in _goal_finish so it is unit-testable in isolation.
            if self._goal_finish(msg.text):
                chained = True
        # Turn-complete toast — only when the turn actually ended (no
        # redirect / goal continue).  Quiet by default: the focused chat's
        # own turn fires no BEL/sound (herdr suppresses the active pane);
        # the card is a subtle "done" marker.
        if not chained:
            self._notify.push("turn complete", "", "ok",
                              write=self._term_write)
            self._ui_refresh()
            # Auto-compact: the turn fully wound down and nothing is
            # chaining a new turn — if the context window is over the
            # threshold, compact now (off the worker thread).
            self._maybe_auto_compact()
            # Steering queue: run the next queued follow-up, if any.
            self._process_next_queued()

    def _interrupt(self) -> None:
        if self.busy:
            self.interrupt()
            self._status_set("ready", "interrupting…")
        else:
            self.editor.clear()
        self._ui_refresh()

    # ── key handling ────────────────────────────────────────────────────

    def _handle_input(self, data: str):
        """Raw-byte handling for the two control keys the loop would
        otherwise consume before key parsing (see :meth:`TUIApp.start`).

        Ctrl+C: interrupts the in-flight turn when one is running, quits
        when idle.  Ctrl+D: submits the editor text when present (matching
        the editor keymap), quits when the editor is empty.  Both consume
        the byte (``None``) instead of letting it reach the key reader.
        """
        if data == "\x03":  # Ctrl+C
            if self._approval is not None:
                # Decline the pending tool (unblocks the parked worker)
                # rather than trying to interrupt a thread that is blocked
                # on the approval prompt.
                self._approval_answer(False)
                return None
            if self._picker is not None:
                self._close_picker()
                return None
            if self.busy:
                self._interrupt()
                return None
            self._tui.stop()
            return True
        if data == "\x04":  # Ctrl+D
            if self.editor.text():
                text = self.editor.text().strip()
                self.editor.clear()
                if text:
                    self._submit(text)
                return None
            self._tui.stop()
            return True
        return False

    def _on_input(self, key: Key) -> None:
        # A tool-approval prompt captures keys while it is pending.
        if self._approval is not None:
            if key.name in ("enter", "y"):
                self._approval_answer(True)
            elif key.name == "a":
                self._approval_always()
            elif key.name in ("n", "escape", "esc"):
                self._approval_answer(False)
            return
        # The session-picker modal captures all keys while it is open.
        if self._picker is not None:
            self._picker_key(key)
            return
        # Mouse (tui3 wave 3): wheel scrolls the log in any mode; button
        # press/release are consumed for now (click-select is a follow-up).
        if key.name == "wheel-up":
            self._sel_anchor = None  # view is moving; drop any pending sel
            self._scroll_log(self._scroll_tick)
            return
        if key.name == "wheel-down":
            self._sel_anchor = None
            self._scroll_log(-self._scroll_tick)
            return
        if key.name in ("mouse-press", "mouse-release"):
            self._mouse_select(key)
            return
        # Browse mode (tui3): Ctrl+O toggles it; while active it captures
        # keys for reading/scrolling the log instead of the editor.
        if key.name == "ctrl+o":
            self._browse = not self._browse
            if not self._browse:
                self.log.offset = 0  # snap back to the bottom on exit
            self._ui_refresh()
            return
        if self._browse:
            # In-log search input takes precedence while a query is typed.
            if self._logsearch is not None and self._logsearch["input"]:
                self._logsearch_char(key)
                return
            if key.name in ("escape", "esc"):
                if self._logsearch is not None:
                    self._logsearch_close()
                else:
                    self._browse = False
                    self.log.offset = 0
                self._ui_refresh()
                return
            if key.name == "/":
                self._logsearch_open()
                return
            if key.name == "n":
                self._logsearch_next(True)
                return
            if key.name == "N":
                self._logsearch_next(False)
                return
            if key.name == "v":
                self._visual_copy()
                return
            if key.name == "j":
                self._scroll_log(-1)  # down / newer
                return
            if key.name == "k":
                self._scroll_log(1)   # up / older
                return
            if key.name == "pageup":
                self._scroll_log(max(5, (self.term.height - 8) // 2))
                return
            if key.name == "pagedown":
                self._scroll_log(-max(5, (self.term.height - 8) // 2))
                return
            if key.name == "home":
                w = self.term.width
                total = len(self.log._all_rows(w))
                self.log.offset = max(0, total - max(self.term.height - 8, 2))
                self._ui_refresh()
                return
            if key.name == "end":
                self.log.offset = 0
                self._ui_refresh()
                return
            # Any other key is consumed (not typed into the editor).
            self._ui_refresh()
            return
        if key.name == "ctrl+t":
            self._toggle_thinking()
            return
        if key.name == "ctrl+l":
            self._open_picker()
            return
        if key.name == "ctrl+p":
            self._open_palette()
            return
        if key.name == "ctrl+r":
            self._open_search()
            return
        if key.name == "ctrl+q":
            self._queue_key()
            return
        if key.name == "ctrl+e":
            self._launch_external_editor()
            return
        # @-file completion modal (tui3): while open it captures the
        # nav/accept keys (up/down/enter/tab/esc); printable chars and
        # backspace fall through to the editor (which grows/shrinks the
        # @token) and _maybe_open_filecomp re-ranks live below.
        if self._filecomp is not None and self._filecomp_key(key):
            self._ui_refresh()
            return
        # Arrow-key history recall: Up walks to older inputs, Down walks
        # back toward the newest and then restores the in-progress draft.
        if key.name == "up":
            self._history_recall(-1)
            return
        if key.name == "down":
            self._history_recall(1)
            return
        # Scrollback: page through the conversation log.
        if key.name == "pageup":
            self._scroll_log(max(5, (self.term.height - 8) // 2))
            return
        if key.name == "pagedown":
            self._scroll_log(-max(5, (self.term.height - 8) // 2))
            return
        if key.name == "home":
            w = self.term.width
            total = len(self.log._all_rows(w))
            self.log.offset = max(0, total - max(self.term.height - 8, 2))
            self._ui_refresh()
            return
        if key.name == "end":
            self.log.offset = 0
            self._ui_refresh()
            return
        if key.name == "ctrl+d" and not self.editor.text():
            self._tui.stop()
            return
        if key.name in ("escape", "esc"):
            self._interrupt()
            return
        if key.name == "paste" and key.payload:
            # The editor is single-line: flatten pasted newlines so the
            # buffer (and the frame) never contains a raw line break.
            self.editor.handle("paste", key.payload.replace("\n", " "))
            self._ui_refresh()
            return
        # Any real edit/typing ends history recall (the next Up starts
        # fresh from the newest entry).
        self._hist_pos = None
        self.editor.handle(key.name, _key_text(key))
        if self.editor.submitted:
            self.editor.submitted = False
            self._filecomp = None  # submitting closes any completion
            text = self.editor.text().strip()
            self.editor.clear()
            if text:
                self._submit(text)
        else:
            self._maybe_open_filecomp()
        self._ui_refresh()

    def _submit(self, text: str) -> None:
        # Every submitted line goes on the reverse-search stack (Ctrl+R).
        self._history.append(text)
        if len(self._history) > 200:
            self._history = self._history[-200:]
        if text.startswith("/"):
            self._run_command(text)
            return
        if text.startswith("!"):
            self._run_shell(text)
            return
        self._last_user_text = text
        self._start_turn(text)

    def _run_shell(self, line: str) -> None:
        """Run a local shell command (``!cmd`` / ``!!cmd``) and show output.

        ``!!`` additionally stores the combined output on
        ``self._last_shell`` for later reference.  The command runs on a
        worker thread so the UI never blocks on it.
        """
        store = line.startswith("!!")
        cmd = (line[2:] if store else line[1:]).strip()
        self.log.add(chatc.ChatMessage(role="user", text=line))
        self.log.offset = 0
        if not cmd:
            self._note("usage:  !cmd   run a shell command   ·   !!cmd   run "
                       "and store its output")
            self._ui_refresh()
            return
        self._status_set("running", f"shell: {cmd[:44]}")
        self._ui_refresh()
        threading.Thread(target=self._shell_worker, args=(cmd, store),
                         daemon=True).start()

    def _shell_worker(self, cmd: str, store: bool) -> None:
        """Synchronous core of ``_run_shell`` (run on a worker thread)."""
        import subprocess
        try:
            p = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=120)
            out, err, code = p.stdout, p.stderr, p.returncode
        except subprocess.TimeoutExpired:
            out, err, code = "", "timed out after 120s", 124
        except Exception as exc:  # never let a shell error kill the UI
            out, err, code = "", str(exc), -1
        combined = out
        if err:
            combined += ("" if combined.endswith("\n") else "\n")
            combined += "[stderr]\n" + err
        if not combined.strip():
            combined = f"(no output, exit {code})"
        if store:
            self._last_shell = combined
        body = combined.rstrip("\n").splitlines()
        if len(body) > 30:
            body = body[:30] + [f"… {len(body) - 30} more line(s)"]
        self.log.add(chatc.ChatMessage(
            role="assistant", text="",
            blocks=[chatc.ChatBlock(
                kind="tool", name="shell",
                title=f"exit {code}",
                status="done" if code == 0 else "error",
                body=body)]))
        self._status_set(
            "ready" if code == 0 else "error",
            f"shell exit {code}" + (" · output stored (!!)" if store else ""))
        if code != 0:
            self._notify.push("shell failed", f"exit {code}: {cmd[:50]}",
                              "error", write=self._term_write,
                              force_bel=True)
        self._ui_refresh()

    # ── Tool-approval gate (herdr-style safety) ─────────────────────────

    def _install_approval_gate(self) -> None:
        """Wrap ``tool_executor.run_tool`` so risky tools prompt first.

        Reuses the exact seam ``team.py``'s ToolArbiter uses (a module-level
        wrapper that is idempotent and restores the true original).  Runs
        on the turn worker thread when a tool is about to execute.
        """
        if self._approval_orig is not None:
            return  # already installed
        import nbchat.core.tool_executor as te
        current = te.run_tool
        if (getattr(current, "__qualname__", "")
                == "ChatApp._gated" and getattr(current, "_gate_orig", None)):
            # A prior (crashed) app left its wrapper; reuse its original.
            self._approval_orig = current._gate_orig
        else:
            self._approval_orig = current
        orig = self._approval_orig
        app = self

        def _gated(tool_name: str, args_json: str,
                   timeout: int | None = None) -> str:
            if app._plan_mode and tool_name in app._file_mutating_tools:
                return (f"[PLAN MODE] '{tool_name}' was blocked because plan "
                        "mode is read-only. Do NOT retry the edit; finish "
                        "your research and present a plan instead.")
            if tool_name in app._file_mutating_tools and not app._turn_mutated:
                app._auto_checkpoint(tool_name)  # best-effort, never raises
                app._turn_mutated = True
            if app._approval_enabled and tool_name in app._risky_tools:
                if not app._prompt_approval(tool_name, args_json):
                    return (f"User DECLINED to run '{tool_name}'. Do not "
                            "retry it. If the task cannot proceed without "
                            "it, stop and report that the action was "
                            "declined.")
            return orig(tool_name, args_json, timeout=timeout)

        _gated.__name__ = "run_tool"
        _gated.__qualname__ = "ChatApp._gated"
        _gated._gate_orig = orig
        te.run_tool = _gated

    def _remove_approval_gate(self) -> None:
        """Restore the original ``run_tool`` and unblock any pending ask."""
        # Unblock a worker parked on an approval (e.g. the user quit).
        pend = self._approval
        if pend is not None:
            pend["result"] = False
            try:
                pend["event"].set()
            except Exception:
                pass
            self._approval = None
        if self._approval_orig is None:
            return
        import nbchat.core.tool_executor as te
        if getattr(te.run_tool, "__qualname__", "") == "ChatApp._gated":
            te.run_tool = self._approval_orig
        self._approval_orig = None

    def _prompt_approval(self, tool: str, args_json: str) -> bool:
        """Show the approval modal and block until the user answers.

        Called from the turn worker thread.  Returns ``True`` to allow the
        tool, ``False`` to decline.  Auto-declines after a 5-minute safety
        timeout so a parked turn can never wedge forever.
        """
        import json as _json
        try:
            args_brief = _json.dumps(_json.loads(args_json),
                                     ensure_ascii=False)
            if len(args_brief) > 160:
                args_brief = args_brief[:157] + "…"
        except Exception:
            args_brief = str(args_json)[:160]
        ev = threading.Event()
        self._approval = {"tool": tool, "args": args_brief,
                          "event": ev, "result": False}
        self._status_set("running", f"approval: {tool}")
        # Needs-attention: ring the BEL and raise a warn toast even if the
        # user muted the main-turn chime.
        self._notify.push("approval needed", tool, "warn",
                          write=self._term_write, force_bel=True)
        self._ui_refresh()
        ev.wait(timeout=300)  # 5-minute safety timeout -> auto-deny
        result = self._approval["result"] if self._approval else False
        self._approval = None
        self._status_set("ready", f"{tool}: {'approved' if result else 'declined'}")
        self._ui_refresh()
        return result

    def _approval_answer(self, allow: bool) -> None:
        """Called from the UI thread when the user answers the prompt."""
        pend = self._approval
        if pend is None:
            return
        pend["result"] = allow
        try:
            pend["event"].set()
        except Exception:
            pass

    # tui2-native slash commands: handled inside the TUI (they need the
    # live frame / clipboard / side-turn state) rather than delegated to the
    # v1 print REPL.  Everything else falls through to v1 ``handle_command``.
    _TUI2_NATIVE = ("/context", "/hotkeys", "/copy", "/compact",
                    "/refine", "/lessons", "/memory", "/btw", "/approve",
                    "/goal", "/notify", "/theme", "/monitor", "/inbox",
                    "/team", "/browse", "/search", "/sup", "/voice",
                    "/fork", "/checkpoint", "/undo", "/find", "/diff",
                    "/export", "/plan", "/retry", "/queue", "/tpl", "/editor", "/gstatus", "/stash",
                    "/rewind", "/pin", "/unpin", "/settings", "/todos", "/project", "/heartbeat", "/autonomous")

    def _run_command(self, line: str) -> None:
        """Route a slash command.

        tui2-native commands are handled here; the rest go through the shared
        v1 ``handle_command`` with stdout captured into the log as a dim note.
        """
        from nbchat.tui.app import handle_command

        parts = line.strip().split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/load" and not arg:
            self._open_picker()
            return
        if cmd == "/name":
            line = "/title " + arg if arg else "/title"
        elif cmd in self._TUI2_NATIVE:
            self._run_tui2_command(cmd, arg)
            return

        prev_sid = self.session_id
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            should_quit = bool(handle_command(self, line))
        except Exception as exc:
            should_quit = False
            buf.write(f"command error: {type(exc).__name__}: {exc}")
        finally:
            sys.stdout = old
        out = buf.getvalue().strip()

        if cmd == "/help":
            addendum = self._tui2_help_addendum()
            out = (out + "\n\n" + addendum) if out else addendum

        if self.session_id != prev_sid:
            self._session_changed()
        if out:
            self.log.add(chatc.ChatMessage(role="system", text=out))
        self._ui_refresh()
        if should_quit:
            self._tui.stop()

    def _tui2_help_addendum(self) -> str:
        """tui2-native features appended to ``/help`` (v1 knows none)."""
        nl = chr(10)
        rows = [
            "TUI v2 extras (not in v1):",
            "  /context    model + context window + compression stats",
            "  /monitor    live session metrics (cache / tools / warnings)",
            "  /inbox [n]  list / read unseen email (read-only, off-thread)",
            "  /team [g]   run a goal as parallel agent team (stop / status)",
            "  /browse <u> fetch a web page's text (headless Chromium)",
            "  /search <q> web search (DuckDuckGo) via /browse",
            "  /sup [q]    supervisor state query / watchdog status",
            "  /voice      Alfred voice-bridge status (--voice)",
            "  /fork [n]   branch this conversation into a new session",
            "  /checkpoint [label]   record a restorable snapshot of the tree",
            "  /undo [label]         preview (no label) / revert tracked files",
            "  /find <query>         search messages across all sessions",
            "  /diff [--stat] [label]  review tracked-file changes (colorized)",
            "  /export [html] [path]  save this session (markdown, or html page)",
            "  /plan [on|off]  read-only research mode (blocks file edits)",
            "  /retry [text] re-run your last message (or run a new one)",
            "  /queue [clear]  view/clear the Ctrl+Q follow-up queue",
            "  /tpl [name [args]] prompt templates from prompts/*.md",
            "  /editor [ctrl+e]  compose the draft in $EDITOR",
            "  /gstatus  git working-tree overview (branch/staged/unstaged/untracked)",
            "  /stash [push|pop [n]|clear]  stash/pop drafts (git-stash for input)",
            "  /rewind [n|restore]  drop the last N user turns (restore to undo)",
            "  /pin /unpin  pin/unpin this session (top of the picker)",
            "  /settings [k v]  view / live-tune TUI settings (theme, scroll, toasts) ",
            "  /todos          show the live agent task list (progress pill)",
            "  /project        show the AGENTS.md / CLAUDE.md auto-loaded at start",
            "  /heartbeat every <dur> <instruction>   fire a recurring turn when idle",
            "  /autonomous <objective> [--auto]   auto-continue with an approval gate",
            "  @<path>      file completion (type @ + a filename, pick a match)",
            "  /compact    force a context summarisation now",
            "  /copy       copy last reply to the clipboard",
            "  /btw <q>    side question, kept out of this session",
            "  /approve    tool-approval gate (on/off/add/rm/list)",
            "  /goal <x>   auto-continue until done or budget (stop)",
            "  /notify     toasts / BEL / sound (test)",
            "  /theme      dark | light | prime (live + remembered)",
            "  /lessons    /memory  /refine   continual-harness",
            "  !<cmd>      run a shell command ( !! stores output )",
            "  Ctrl+T      show / hide thinking blocks",
            "  Ctrl+L      session picker (bare /load = picker)",
            "  Ctrl+P      command palette    Ctrl+R reverse search",
            "  Ctrl+O      browse mode (j/k scroll the log)",
            "  @<name>     file completion (up/down pick, Enter/Tab, Esc)",
            "  PgUp/PgDn   scrollback         Home/End top/bottom",
        ]
        return nl.join(rows)

    def _run_tui2_command(self, cmd: str, arg: str) -> None:
        """Dispatch a tui2-native slash command to its handler."""
        handlers = {
            "/context": self._cmd_context,
            "/hotkeys": self._cmd_hotkeys,
            "/copy": self._cmd_copy,
            "/compact": self._cmd_compact,
            "/refine": self._cmd_refine,
            "/lessons": self._cmd_lessons,
            "/memory": self._cmd_memory,
            "/btw": self._cmd_btw,
            "/approve": self._cmd_approve,
            "/goal": self._cmd_goal,
            "/notify": self._cmd_notify,
            "/theme": self._cmd_theme,
            "/monitor": self._cmd_monitor,
            "/inbox": self._cmd_inbox,
            "/team": self._cmd_team,
            "/browse": self._cmd_browse,
            "/search": self._cmd_search,
            "/sup": self._cmd_sup,
            "/voice": self._cmd_voice,
            "/fork": self._cmd_fork,
            "/checkpoint": self._cmd_checkpoint,
            "/undo": self._cmd_undo,
            "/find": self._cmd_find,
            "/diff": self._cmd_diff,
            "/export": self._cmd_export,
            "/plan": self._cmd_plan,
            "/retry": self._cmd_retry,
            "/queue": self._cmd_queue,
            "/tpl": self._cmd_tpl,
            "/editor": self._cmd_editor,
            "/gstatus": self._cmd_gstatus,
            "/stash": self._cmd_stash,
            "/rewind": self._cmd_rewind,
            "/pin": self._cmd_pin,
            "/unpin": self._cmd_unpin,
            "/settings": self._cmd_settings,
            "/todos": self._cmd_todos,
            "/project": self._cmd_project,
            "/heartbeat": self._cmd_heartbeat,
            "/autonomous": self._cmd_autonomous,
        }
        fn = handlers.get(cmd)
        try:
            out = fn(arg) if fn is not None else "unknown command"
        except Exception as exc:
            out = f"command error: {type(exc).__name__}: {exc}"
        if out:
            self._note(out)
        self._ui_refresh()

    def _note(self, text: str) -> None:
        """Append a dim system note to the log."""
        self.log.add(chatc.ChatMessage(role="system", text=text))
        self._ui_refresh()

    # ── tui2-native commands ────────────────────────────────────────────

    def _cmd_context(self, arg: str) -> str:
        from nbchat.tui.status import _ctx_bar, _humanise

        lines = [f"model {self.model_name or '?'} · session {self.session_id}"]
        if self._ctx_budget > 0:
            bar, pct = _ctx_bar(self._ctx_used, self._ctx_budget)
            lines.append(
                f"context {bar} {pct}  "
                f"({_humanise(self._ctx_used)} / {_humanise(self._ctx_budget)})")
        else:
            lines.append("context (no window reported yet)")
        try:
            from nbchat.core import compressor as comp
            stats = comp.get_compression_stats()
            if stats:
                calls = sum(s["calls"] for s in stats.values())
                compd = sum(s["compressed_calls"] for s in stats.values())
                lines.append(
                    f"tool-output compression {compd}/{calls} calls")
        except Exception:
            pass
        lines.append(f"turns {self._turns}")
        if self._auto_compact_enabled:
            lines.append(f"auto-compact on @ {self._auto_compact_frac:.0%} "
                         f"(NBCHAT_AUTO_COMPACT)")
        else:
            lines.append("auto-compact off (NBCHAT_AUTO_COMPACT=0)")
        return "\n".join(lines)

    def _cmd_monitor(self, arg: str) -> str:
        """Live per-session observability (tui3): cache similarity, tool
        call counts / errors, and any warnings the monitoring engine has
        detected.  Read-only — the data is accumulated by the conversation
        loop via ``nbchat.core.monitoring`` as the session runs."""
        from nbchat.core import monitoring as mon

        try:
            report = mon.get_session_monitor(self.session_id).get_session_report()
        except Exception as exc:
            return f"monitor error: {type(exc).__name__}: {exc}"
        text = mon.format_report(report).strip()
        if not text:
            return "monitor: no metrics recorded yet this session"
        return text

    def _inbox_peek(self, n: int | None) -> str:
        """Synchronous peek at unseen email (may touch the network / raise).

        Returns formatted text: a numbered header list when *n* is ``None``,
        or the full body of unseen message #*n* when it is an int.  Designed
        to run on a worker thread (see :meth:`_cmd_inbox`)."""
        from nbchat.core import email_inbox

        msgs = email_inbox.peek_unseen(limit=20)
        if not msgs:
            return "inbox: no unseen messages"
        if n is None:
            lines = [f"inbox: {len(msgs)} unseen"]
            for i, m in enumerate(msgs, 1):
                date = m.date.strftime("%m-%d %H:%M") if m.date else "?"
                lines.append(f"  {i}. [{date}] {m.from_addr}: {m.subject}")
            lines.append("  (/inbox <n> to read one — read-only; nothing is marked read)")
            return "\n".join(lines)
        if not (1 <= n <= len(msgs)):
            return f"inbox: no unseen message #{n} (have {len(msgs)})"
        m = msgs[n - 1]
        body = email_inbox.fetch_body(m.uid)
        return f"inbox: [{m.from_addr}] {m.subject}\n{body}"

    def _cmd_inbox(self, arg: str) -> str:
        """Browse the unseen inbox (tui3).  The IMAP peek runs on a daemon
        thread and the result is delivered via a ``"call"`` event, so the UI
        thread is never blocked by the network round-trip.  Read-only:
        nothing is marked read (the ``--email`` bridge owns that)."""
        n = None
        if arg:
            try:
                n = int(arg)
            except ValueError:
                return "inbox: usage /inbox [n]   (n = position in the unseen list)"

        def work() -> None:
            try:
                out = self._inbox_peek(n)
            except Exception as exc:
                out = f"inbox: {type(exc).__name__}: {exc}"
            self.events.put("call", lambda: self._note(out))

        threading.Thread(target=work, daemon=True).start()
        return "inbox: checking…"

    # ── /browse + /search (tui3: web surface over the browser tool) ─────

    def _browse_url(self, url: str, max_chars: int = 4000) -> str:
        """Fetch a page's text via the ``nbchat.tools.browser`` engine.

        Synchronous and testable: monkeypatch ``nbchat.tools.browser.browser``
        in tests.  Returns a formatted ``title`` + truncated ``content``
        string, or a friendly error note on failure.
        """
        import json as _json
        from nbchat.tools.browser import browser as _browser
        try:
            raw = _browser(url, max_content_length=max_chars * 2)
        except Exception as exc:
            return f"browse: {type(exc).__name__}: {exc}"
        try:
            data = _json.loads(raw)
        except Exception:
            return f"browse: {str(raw)[:max_chars]}"
        if data.get("error"):
            return f"browse: {data.get('error')}"
        title = data.get("title", "") or ""
        content = (data.get("content", "") or "").strip()
        if len(content) > max_chars:
            content = content[:max_chars] + " …[truncated]"
        head = f"browse: {data.get('url', url)}"
        if title:
            head += f" — {title}"
        body = content or "(no readable text)"
        return head + "\n" + body

    def _cmd_browse(self, arg: str) -> str:
        url = (arg or "").strip()
        if not url:
            return "browse: usage /browse <url>  (or /search <query>)"
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
            url = "https://" + url
        def work() -> None:
            try:
                out = self._browse_url(url)
            except Exception as exc:
                out = f"browse: {type(exc).__name__}: {exc}"
            self.events.put("call", lambda o=out: self._note(o))
        threading.Thread(target=work, daemon=True).start()
        return "browse: loading…"

    def _cmd_search(self, arg: str) -> str:
        query = (arg or "").strip()
        if not query:
            return "search: usage /search <query>"
        from urllib.parse import quote_plus
        # DuckDuckGo's /html/ endpoint is bot-friendly and returns clean
        # text; reuse the /browse pipeline for the fetch + display.
        url = "https://duckduckgo.com/html/?q=" + quote_plus(query)
        self._cmd_browse(url)
        return f"search: {query}"

    # ── /sup (supervisor state query; v1 --supervisor parity) ───────────

    def _cmd_sup(self, arg: str) -> str:
        """``/sup`` status · ``/sup <question>`` ask the supervisor about the
        live system state (server, git, tasks, assistant progress).

        The answer is a synchronous LLM call on the supervisor's own slot, so
        it runs off the UI thread (daemon) and is delivered via the ``"call"``
        event — the render loop is never blocked.
        """
        sup = self._supervisor
        if sup is None:
            return ("sup: supervisor not running "
                    "(start with --supervisor, or set NBCHAT_SUPERVISOR=1)")
        question = (arg or "").strip()
        if not question:
            return (f"sup: {'running' if sup.running else 'stopped'}  ·  "
                    f"interjections: {sup.interjection_count}  ·  "
                    f"review every {sup._interval}s, cooldown {sup._cooldown}s")
        def work() -> None:
            try:
                answer = sup.ask(question)
            except Exception as exc:
                answer = f"sup: {type(exc).__name__}: {exc}"
            note = "sup: " + answer
            self.events.put("call", lambda n=note: self._note(n))
        threading.Thread(target=work, daemon=True).start()
        return "sup: asking…"

    def _cmd_voice(self, arg: str) -> str:
        """``/voice`` — status of the Alfred voice bridge (if started)."""
        if self._voice_bridge is None:
            return ("voice: bridge not running "
                    "(start with --voice, or set NBCHAT_VOICE=1)")
        try:
            import nbchat.core.config as _cfg
            port = _cfg.VOICE_PORT
        except Exception:
            port = "?"
        return (f"voice: ACTIVE on localhost:{port}  ·  "
                f"ssh -L {port}:127.0.0.1:{port} user@server")

    # ── /fork (tui3: branch the conversation into a new session) ────────

    def _cmd_fork(self, arg: str) -> str:
        """``/fork [n]`` — branch this conversation into a new session.

        With no argument the *entire* current history is copied into a fresh
        session (branch from now).  With ``n`` (the 1-based index of a user
        message) the new session holds everything up to and including that
        message, so you can steer the branch differently from the point you
        asked it.  The original session is left completely untouched; the app
        switches to the fork and remembers it as the current session.
        """
        import nbchat.core.db as _db
        rows = _db.load_history(self.session_id)
        if not rows:
            return "fork: nothing to fork yet (no history in this session)"
        user_idx = [i for i, r in enumerate(rows) if r[0] == "user"]
        a = (arg or "").strip()
        if not a:
            cut, where = len(rows), "full history"
        else:
            if not a.isdigit():
                return ("fork: give a number (your Nth message) or leave it "
                        "blank for a full fork")
            n = int(a)
            if n < 1 or n > len(user_idx):
                return (f"fork: this session has {len(user_idx)} message(s); "
                        f"use /fork 1..{len(user_idx)} or bare /fork")
            cut, where = user_idx[n - 1] + 1, f"up to your message {n}"
        if cut <= 0:
            return "fork: nothing to fork at that point"
        prev = self.session_id
        try:
            new_sid = self._new_session_id()
            _db.replace_session_history(new_sid, [tuple(r) for r in rows[:cut]])
            try:  # carry the in-flight task list so the branch keeps its to-dos
                tl = _db.load_task_log(prev)
                if tl:
                    _db.save_task_log(new_sid, tl)
            except Exception:
                pass
            short_old = (prev or "").rsplit(":", 1)[-1][:12]
            _db.save_session_title(new_sid, f"fork {short_old} {where}")
        except Exception as exc:
            return f"fork: failed ({type(exc).__name__}: {exc})"
        self._switch_session(new_sid)
        self._session_changed()
        self.remember_session(self.session_id)
        self._note(f"forked into {self.session_id} ({where}); "
                   f"original {prev} is untouched")
        return f"fork: branched {where} -> {self.session_id}"

    # ── /rewind (tui3: conversation rewind — go back N user turns) ──────

    _REWIND_GHOST_MAX = 200  # max rows kept recoverable after a rewind

    def _cmd_rewind(self, arg: str) -> str:
        """``/rewind [n]`` — drop the last *n* user turn(s) from this session.

        No arg lists your recent user turns (newest first) with the number to
        pass.  ``/rewind <n>`` removes the last *n* user turn(s) and everything
        after them, keeping the removed slice recoverable (one level) via
        ``/rewind restore`` until you start a new user turn.  This rewinds the
        *conversation* in the current session — /fork to back up first, /undo
        for a git file revert.
        """
        a = (arg or "").strip()
        if a.lower() in ("restore", "undo", "back"):
            return self._rewind_restore()
        if a:
            if not a.isdigit():
                return ("rewind: give a number (how many recent turns to drop) "
                        "or leave it blank to list them")
            return self._rewind_apply(int(a))
        return self._rewind_list()

    def _rewind_list(self) -> str:
        import nbchat.core.db as _db
        rows = _db.load_history(self.session_id)
        user_idx = [i for i, r in enumerate(rows) if r[0] == "user"]
        if not user_idx:
            return "rewind: no user turns yet (nothing to rewind)"
        lines = [f"rewind: {len(user_idx)} user turn(s); drop the last N with /rewind <N>"]
        for k in range(1, min(8, len(user_idx)) + 1):
            content = (rows[user_idx[-k]][1] or "").strip().replace("\n", " ")
            if len(content) > 48:
                content = content[:48] + "\u2026"
            lines.append(f"  {k}  {content!r}")
        lines.append("\u2192 /rewind 1 drops your last message + its reply")
        return "\n".join(lines)

    def _rewind_apply(self, n: int) -> str:
        import nbchat.core.db as _db
        if self.busy:
            return "rewind: wait for the current turn to finish"
        rows = _db.load_history(self.session_id)
        user_idx = [i for i, r in enumerate(rows) if r[0] == "user"]
        if not user_idx:
            return "rewind: no user turns yet (nothing to rewind)"
        n = max(1, min(n, len(user_idx)))
        cut = user_idx[-n]
        kept, removed = rows[:cut], rows[cut:]
        post_user = len(user_idx) - n
        preview = (rows[user_idx[-1]][1] or "").strip().replace("\n", " ")
        if len(preview) > 40:
            preview = preview[:40] + "\u2026"
        ghost_ok = len(removed) <= self._REWIND_GHOST_MAX
        if ghost_ok:
            try:
                _db._meta_set(self.session_id, "rewind_ghost", json.dumps(
                    {"post_user": post_user,
                     "rows": [[c for c in r] for r in removed]}))
            except Exception:
                ghost_ok = False
        try:
            _db.replace_session_history(self.session_id, [tuple(r) for r in kept])
        except Exception as exc:
            return f"rewind: failed ({type(exc).__name__}: {exc})"
        self._refresh_history_cache()
        self._session_changed()
        rec = ("/rewind restore to undo (until your next turn)"
               if ghost_ok else
               "slice too large to auto-keep — /fork to back up next time")
        return (f"rewound: removed {n} turn(s) (last was {preview!r}); {rec}")

    def _rewind_restore(self) -> str:
        import nbchat.core.db as _db
        if self.busy:
            return "rewind: wait for the current turn to finish"
        try:
            raw = _db._meta_get(self.session_id, "rewind_ghost")
        except Exception:
            raw = ""
        if not raw:
            return "rewind: nothing to restore (no pending rewind)"
        try:
            g = json.loads(raw)
            ghost_rows = [tuple(r) for r in g.get("rows", [])]
            post_user = int(g.get("post_user", -1))
        except Exception:
            return "rewind: restore failed (corrupt saved slice)"
        if not ghost_rows:
            _db._meta_set(self.session_id, "rewind_ghost", "")
            return "rewind: nothing to restore (saved slice was empty)"
        cur = _db.load_history(self.session_id)
        cur_user = sum(1 for r in cur if r[0] == "user")
        if cur_user != post_user:
            _db._meta_set(self.session_id, "rewind_ghost", "")
            return ("rewind: can't restore — you started a new turn after the "
                    "rewind (saved slice cleared)")
        try:
            _db.replace_session_history(
                self.session_id, [tuple(r) for r in (list(cur) + ghost_rows)])
        except Exception as exc:
            return f"rewind: restore failed ({type(exc).__name__}: {exc})"
        _db._meta_set(self.session_id, "rewind_ghost", "")
        self._refresh_history_cache()
        self._session_changed()
        return f"restored {len(ghost_rows)} row(s): undid the last rewind"

    def _refresh_history_cache(self) -> None:
        """Reload self.history / task log / summaries from the DB (same session)."""
        import nbchat.core.db as _db
        import nbchat.core.config as _cfg
        try:
            self.history = list(_db.load_history(
                self.session_id,
                limit=int(getattr(_cfg, "HISTORY_ROW_LIMIT", 2000))))
            self.task_log = _db.load_task_log(self.session_id)
            self._turn_summary_cache = _db.load_turn_summaries(self.session_id)
        except Exception:
            pass

    # ── /checkpoint + /undo (tui3: safe git-backed code revert) ────────

    def _auto_checkpoint(self, tool_name: str) -> None:
        """Best-effort pre-edit checkpoint, once per edit-window (silent)."""
        try:
            from . import undo as _undo
            cwd = os.getcwd()
            if not _undo.is_git_repo(cwd):
                return
            cp = _undo.take_checkpoint(cwd, self.session_id, label="auto")
            if cp:
                self._status_set("checkpt", cp["sha"])
        except Exception:
            pass

    def _cmd_checkpoint(self, arg: str) -> str:
        """``/checkpoint [label]`` — record a restorable snapshot of the tree."""
        from . import undo as _undo
        cwd = os.getcwd()
        if not _undo.is_git_repo(cwd):
            return "checkpoint: this directory is not a git work tree"
        cp = _undo.take_checkpoint(cwd, self.session_id, label=(arg or "").strip())
        if not cp:
            return "checkpoint: failed to record (git error?)"
        return (f"checkpoint '{cp['label']}' recorded at {cp['sha']} "
                f"({cp['note']}); restore with /undo {cp['label']}")

    def _cmd_undo(self, arg: str) -> str:
        """``/undo [label]`` — preview (no label) or revert tracked files."""
        from . import undo as _undo
        cwd = os.getcwd()
        lst = _undo.list_checkpoints(self.session_id)
        a = (arg or "").strip()
        if not a:
            if not lst:
                return ("undo: no checkpoints for this session yet — run "
                        "/checkpoint first (one is also recorded automatically "
                        "before the first file edit of a turn)")
            cp = lst[-1]
            avail = ", ".join(c["label"] for c in lst[-6:])
            return (f"undo (preview): checkpoints: {avail}\n"
                    f"latest '{cp['label']}' @ {cp['sha']} — {cp['note']}\n"
                    + _undo.preview(cwd, cp)
                    + f"\nto apply: /undo {cp['label']}")
        cp = _undo.find_checkpoint(self.session_id, a)
        if not cp:
            avail = ", ".join(c["label"] for c in lst[-6:]) or "none"
            return f"undo: no checkpoint named '{a}' (available: {avail})"
        ok, summary = _undo.apply(cwd, cp, dry=False)
        self._note(("✓ " if ok else "✗ ") + summary)
        return summary

    # ── /find (tui3: cross-session full-text search over chat history) ──

    def _cmd_find(self, arg: str) -> str:
        """``/find <query> [session]`` — search messages (all sessions by
        default; append ``session`` to search only the current one)."""
        import nbchat.core.db as _db
        a = (arg or "").strip()
        if not a:
            return "find: give a search term, e.g. /find checkpoint"
        local_only = False
        if a.split()[-1].lower() in ("session", "-s", "--session"):
            local_only = True
            a = a.rsplit(None, 1)[0].strip()
        if not a:
            return "find: give a search term, e.g. /find checkpoint"
        try:
            hits = _db.search_messages(
                a, limit=25,
                session_id=self.session_id if local_only else None)
        except Exception as exc:
            return f"find: search failed ({type(exc).__name__}: {exc})"
        if not hits:
            scope = "this session" if local_only else "any session"
            return f"find: no messages matching '{a}' in {scope}"
        scope = "(this session)" if local_only else "(all sessions)"
        lines = [f"find: {len(hits)} match(es) for '{a}' {scope}"]
        for sid, role, snip in hits:
            short = (sid or "").rsplit(":", 1)[-1][:10]
            cur = "*" if sid == self.session_id else " "
            oneline = " ".join((snip or "").split())
            lines.append(f"{cur} {short}  {role:<9}  {oneline[:100]}")
        tail = "  ·  /load <full sid> to open a match"
        if len(hits) >= 25:
            tail = "  … (capped at 25)" + tail
        lines.append(tail)
        return chr(10).join(lines)

    # ── /diff (tui3: review tracked-file changes, colorized) ────────────

    _DIFF_MAX_LINES = 200
    _GSTATUS_MAX = 50

    def _cmd_diff(self, arg: str) -> str:
        """``/diff [--stat] [label]`` — review tracked-file changes.

        * ``/diff``            working tree vs ``HEAD``
        * ``/diff --stat``     summary only (files + added/removed counts)
        * ``/diff <label>``    working tree vs checkpoint ``<label>``

        The unified diff is rendered as a colorized block (``+`` green,
        ``-`` red) reusing the agent tool-diff renderer.  Read-only.
        """
        from . import undo as _undo
        cwd = os.getcwd()
        if not _undo.is_git_repo(cwd):
            return "diff: this directory is not a git work tree"
        a = (arg or "").strip()
        stat_only = False
        label = ""
        toks = a.split()
        if toks and toks[0].lower() in ("--stat", "-s", "stat"):
            stat_only = True
            toks = toks[1:]
        if toks:
            label = " ".join(toks)
        if label:
            if label.lower() in ("last", "latest"):
                cp = _undo.latest_checkpoint(self.session_id)
            else:
                cp = _undo.find_checkpoint(self.session_id, label)
            if not cp:
                avail = ", ".join(
                    c["label"] for c in _undo.list_checkpoints(self.session_id)[-6:]) or "none"
                return f"diff: no checkpoint named '{label}' (available: {avail})"
            source, title = cp.get("source"), f"working tree vs checkpoint '{cp.get('label')}'"
        else:
            source, title = "HEAD", "working tree vs HEAD"
        git_args = ["diff", "--stat" if stat_only else "--unified=3", source]
        rc, out = _undo._git(cwd, *git_args)
        if rc != 0:
            return f"diff: git diff failed: {out.strip()[:200]}"
        body = out.splitlines()
        if not body:
            self._note(f"diff: {title} — no tracked-file changes")
            return f"diff: {title} — no tracked-file changes"
        files = _undo._git(cwd, "diff", "--name-only", source)[1].splitlines()
        files = [f for f in files if f.strip()]
        truncated = len(body) > self._DIFF_MAX_LINES
        shown = body[:self._DIFF_MAX_LINES]
        if truncated:
            shown.append(f"… ({len(body) - self._DIFF_MAX_LINES} more lines; "
                         f"use /diff --stat for a summary)")
        self.log.add(chatc.ChatMessage(
            role="assistant",
            blocks=[chatc.ChatBlock(kind="tool", name="diff", title=title,
                                    status="done", body=shown,
                                    diff=not stat_only)],
        ))
        nfiles = len(files)
        return (f"diff: {title} — {nfiles} file(s) changed"
                + ("" if stat_only else "  ·  /undo reverts tracked files"))

    # ── /gstatus (tui3: git working-tree overview) ────────────────────
    def _cmd_gstatus(self, arg: str) -> str:
        """``/gstatus`` — a git working-tree overview (branch, staged,
        unstaged, untracked).  Rounds out the /diff + /checkpoint tooling
        with a quick "what has changed" snapshot.  Read-only.
        """
        from . import undo as _undo
        cwd = os.getcwd()
        if not _undo.is_git_repo(cwd):
            return "gstatus: this directory is not a git work tree"
        rc, branch = _undo._git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        branch = branch.strip() if rc == 0 else "?"
        rc, out = _undo._git(cwd, "status", "--porcelain")
        if rc != 0:
            return f"gstatus: git status failed: {out.strip()[:200]}"
        staged, unstaged, untracked = [], [], []
        for line in out.splitlines():
            if len(line) < 3 or not line.strip():
                continue
            x, y, path = line[0], line[1], line[3:].strip()
            if x == "?" and y == "?":
                untracked.append(path)
                continue
            if x not in (" ", "?"):
                staged.append(f"{x} {path}")
            if y not in (" ", "?"):
                unstaged.append(f"{y} {path}")
        def _fmt(items):
            out_ = [f"  {i}" for i in items[:self._GSTATUS_MAX]]
            if len(items) > self._GSTATUS_MAX:
                out_.append(f"  … (+{len(items) - self._GSTATUS_MAX} more)")
            return out_
        total = len(staged) + len(unstaged) + len(untracked)
        if total == 0:
            self._note(f"gstatus: clean working tree ({branch})")
            return f"gstatus: clean working tree ({branch})"
        lines = [f"branch: {branch}"]
        if staged:
            lines.append(f"staged ({len(staged)}):")
            lines.extend(_fmt(staged))
        if unstaged:
            lines.append(f"unstaged ({len(unstaged)}):")
            lines.extend(_fmt(unstaged))
        if untracked:
            lines.append(f"untracked ({len(untracked)}):")
            lines.extend(_fmt(untracked))
        self.log.add(chatc.ChatMessage(
            role="assistant",
            blocks=[chatc.ChatBlock(kind="tool", name="git status",
                                    title=f"git status ({branch})",
                                    status="done", body=lines, diff=False)],
        ))
        return (f"gstatus: {len(staged)} staged, {len(unstaged)} unstaged, "
                f"{len(untracked)} untracked on {branch}")

    # ── /stash (tui3: prompt stash — git-stash for the input buffer) ──
    _STASH_MAX = 50

    def _stash_file(self) -> str:
        p = os.environ.get("NBCHAT_STASH_FILE")
        if p:
            return p
        return os.path.join(os.path.expanduser("~/.nbchat"), "tui3-stash.jsonl")

    def _stash_load(self) -> list:
        p = self._stash_file()
        try:
            if not os.path.exists(p):
                return []
            items = []
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(json.loads(line))
                    except Exception:
                        pass
            return items[-self._STASH_MAX:]
        except Exception:
            return []

    def _stash_save(self, items: list) -> None:
        p = self._stash_file()
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                for it in items[-self._STASH_MAX:]:
                    f.write(json.dumps(it, ensure_ascii=False) + chr(10))
        except Exception:
            pass

    def _cmd_stash(self, arg: str) -> str:
        """``/stash [push|pop [n]|clear]`` — git-stash for the input buffer.

        Push the draft you are composing (persists it, up to 50 entries),
        compose something else, then pop the stashed draft back.  Complements
        the Ctrl+Q steering queue (which queues messages to *send*); the stash
        holds *drafts to compose later* and survives restarts.
        """
        a = (arg or "").strip()
        toks = a.split()
        if not toks:
            items = self._stash_load()
            if not items:
                return "stash: empty (use /stash push to save the current draft)"
            rows = []
            for i, it in enumerate(items, 1):
                txt = (it.get("text") or "").replace(chr(10), " ")
                label = (it.get("label") or "").strip()
                shown = (label + " — " if label else "") + txt[:40]
                if len(txt) > 40:
                    shown += "…"
                rows.append(f"  {i}. {shown}")
            body = [f"stash: {len(items)} draft(s) — most recent last"] + rows
            self.log.add(chatc.ChatMessage(
                role="assistant",
                blocks=[chatc.ChatBlock(kind="tool", name="stash",
                                        title="prompt stash", status="done",
                                        body=body, diff=False)],
            ))
            return f"stash: {len(items)} draft(s); /stash pop [n] to load one"
        cmd = toks[0].lower()
        if cmd in ("push", "p", "+"):
            draft = self.editor.text()
            if not draft.strip():
                return "stash: nothing to stash (the editor is empty)"
            label = " ".join(toks[1:]) if len(toks) > 1 else ""
            items = self._stash_load()
            items.append({"text": draft, "label": label, "ts": time.time()})
            self._stash_save(items)
            self.editor.clear()
            self._note(f"stash: pushed draft (now {len(items)}) — /stash pop to load it back")
            return f"stash: pushed draft (now {len(items)})"
        if cmd in ("pop", "get"):
            items = self._stash_load()
            if not items:
                return "stash: empty (nothing to pop)"
            n = len(items)
            idx = n
            if len(toks) > 1:
                if not toks[1].isdigit():
                    return f"stash: entry must be a number (1..{n})"
                idx = int(toks[1])
                if idx < 1 or idx > n:
                    return f"stash: no entry {idx} (1..{n})"
            entry = items[idx - 1]
            self.editor.set_text(entry.get("text") or "")
            self._note(f"stash: loaded draft #{idx} of {n} into the editor")
            return f"stash: loaded draft #{idx} of {n}"
        if cmd in ("clear", "c", "x", "reset"):
            cnt = len(self._stash_load())
            self._stash_save([])
            self._note(f"stash: cleared {cnt} draft(s)")
            return f"stash: cleared {cnt} draft(s)"
        return f"stash: unknown subcommand '{cmd}' (use push / pop [n] / clear)"

    # ── /plan (tui3: read-only research mode) ───────────────────────────

    _PLAN_NOTE = (
        chr(10) + chr(10)
        + "[PLAN MODE] You are in read-only research mode. Do NOT create, "
        "edit, or delete any files, and do NOT run mutating shell commands "
        "(no writes, no git mutations, no installs). Only READ files, "
        "SEARCH, and report what you find plus a concrete plan. If the user "
        "asks you to change something, present the plan and wait — they will "
        "turn plan mode off when ready."
    )

    _TODO_NOTE = (
        chr(10) + chr(10)
        + "[TASK LIST] For a non-trivial, multi-step task, keep a short task "
        + "list with the todo tool so the user can watch progress: pass the FULL "
        + "list each time as an array of {text, done} objects (done = true when "
        + "a step is finished), under ~8 items. Update it as you go and pass an "
        + "empty array when the work is done."
    )

    def _cmd_plan(self, arg: str = "") -> str:
        """``/plan [on|off]`` — toggle read-only research mode.

        While active, file-mutating tools (create_file / make_change_to_file /
        run_command) are blocked at the tool gate, the mode bar shows
        ``plan``, and a read-only note is appended to the system prompt so the
        model researches instead of editing.  Toggles with no arg; ``on`` /
        ``off`` set explicitly.  Safe: it only ever blocks tools + notes.
        """
        a = (arg or "").strip().lower()
        if a in ("on", "1", "yes", "true", "enable"):
            want = True
        elif a in ("off", "0", "no", "false", "disable"):
            want = False
        else:
            want = not self._plan_mode
        if want == self._plan_mode:
            return f"plan mode is already {'on' if want else 'off'}"
        self._plan_mode = want
        note = self._PLAN_NOTE
        if want:
            if note not in self.system_prompt:
                self.system_prompt += note
            self._note("plan mode ON — read-only (file edits blocked); "
                       "/plan off to exit")
            return "plan mode ON (read-only research)"
        if note in self.system_prompt:
            self.system_prompt = self.system_prompt.replace(note, "")
        self._note("plan mode OFF — file edits re-enabled")
        return "plan mode OFF"

    def _cmd_retry(self, arg: str) -> str:
        """Re-run the last user message (or ``/retry <text>`` to run a new one).

        Handy after a failed/unsatisfying turn or after you tweak the prompt:
        it resends the last thing you asked without retyping it.  Refuses
        while a turn is in flight (interrupting is what interjection/Enter does).
        """
        if self.busy:
            return "retry: wait for the current turn to finish (press Enter to steer)"
        text = arg.strip() if arg.strip() else (self._last_user_text or "").strip()
        if not text:
            return "retry: nothing to retry yet (send a message first)"
        # Record it as the new "last user message" so a chained /retry works,
        # then submit it as a normal turn.
        self._last_user_text = text
        self._start_turn(text)
        return "retry: re-sending your last message"

    # ── /export (tui3: save a session as a markdown file) ───────────────

    def _session_markdown(self, sid: str) -> str:
        """Build a clean, readable markdown document for a session's history.

        Uses the full ``db.load_history`` rows (role + content + tool name) so
        tool calls are labelled.  Pure/read-only — no I/O here.
        """
        from nbchat.core import db as _db
        import datetime
        rows = _db.load_history(sid)
        title = _db.load_session_title(sid) or sid
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        parts = [f"# {title}", "",
                 f"> Exported {now} · session `{sid}` · {len(rows)} messages",
                 "", "---", ""]
        for row in rows:
            role = row[0]
            content = (row[1] if len(row) > 1 else "") or ""
            tool_name = (row[3] if len(row) > 3 else "") or ""
            content = content.strip()
            if not content and not tool_name:
                continue
            if role == "tool":
                hdr = f"**tool** — `{tool_name}`" if tool_name else "**tool**"
                parts.append(hdr)
                if content:
                    fence = "```"
                    while fence in content:
                        fence += "`"
                    parts += ["", fence, content, fence, ""]
                else:
                    parts += ["", ""]
            else:
                parts += [f"**{role}**", "", content, ""]
        return chr(10).join(parts).rstrip() + chr(10)

    def _session_html(self, sid: str) -> str:
        """Build a self-contained HTML document for a session's history
        (read-only over the DB, mirrors _session_markdown)."""
        from nbchat.core import db as _db
        import datetime
        import html as _html
        rows = _db.load_history(sid)
        title = _db.load_session_title(sid) or sid
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        esc = _html.escape
        parts = [
            "<!DOCTYPE html>",
            "<html><head><meta charset=\"utf-8\">",
            f"<title>{esc(title)}</title>",
            "<style>",
            "body{font-family:ui-monospace,Menlo,Consolas,monospace;max-width:900px;margin:2em auto;padding:0 1em;color:#1c1e21;background:#fafafa;line-height:1.45;}",
            "h1{font-size:1.4em;border-bottom:2px solid #333;padding-bottom:.3em;}",
            ".meta{color:#666;font-size:.85em;margin-bottom:1.2em;}",
            ".msg{margin:0 0 1em;padding:.6em .8em;border-radius:6px;border-left:4px solid #999;background:#fff;}",
            ".msg .role{font-weight:700;font-size:.75em;text-transform:uppercase;letter-spacing:.05em;display:block;margin-bottom:.3em;color:#333;}",
            ".user{border-left-color:#2f81f7;}",
            ".assistant{border-left-color:#189a63;}",
            ".tool{border-left-color:#b08800;background:#fffdf5;}",
            ".toolname{color:#b08800;font-weight:700;font-size:.8em;}",
            "pre{white-space:pre-wrap;word-break:break-word;background:#f4f4f4;padding:.5em;border-radius:4px;}",
            "</style></head><body>",
            f"<h1>{esc(title)}</h1>",
            (f"<div class=\"meta\">Exported {esc(now)} "
             "&middot; session <code>{esc(sid)}</code> "
             f"&middot; {len(rows)} messages</div>"),
        ]
        for row in rows:
            role = row[0]
            content = (row[1] if len(row) > 1 else "") or ""
            tool_name = (row[3] if len(row) > 3 else "") or ""
            content = content.strip()
            if not content and not tool_name:
                continue
            if role == "tool":
                body = f"<pre>{esc(content)}</pre>" if content else ""
                nm = f'<span class="toolname">{esc(tool_name)}</span>' if tool_name else ""
                parts.append(f'<div class="msg tool"><span class="role">tool</span>{nm}{body}</div>')
            else:
                css = role if role in ("user", "assistant") else "msg"
                parts.append(f'<div class="msg {css}"><span class="role">{esc(role)}</span>{esc(content)}</div>')
        parts.append("</body></html>")
        return chr(10).join(parts) + chr(10)

    def _cmd_export(self, arg: str = "") -> str:
        """``/export [html] [path]`` — save this session as a file.

        Read-only over the DB; writes one file.  Default format is markdown;
        pass ``html`` as the first token to write a self-contained HTML page
        (``/export html [path]``).  With no path it writes to
        ``~/.nbchat/exports/nbchat-<short-sid>-<ts>.{md,html}`` (never pollutes
        the working tree).  An explicit path (relative or absolute) is honoured.
        """
        from nbchat.core import db as _db
        import datetime
        toks = (arg or "").split(None, 1)
        fmt = "md"
        if toks and toks[0].lower() in ("html", "htm"):
            fmt = "html"
            a = toks[1].strip() if len(toks) > 1 else ""
        else:
            a = (arg or "").strip()
        rows = _db.load_history(self.session_id)
        if not rows:
            return "export: no messages in this session to export"
        if fmt == "html":
            doc = self._session_html(self.session_id)
            ext = "html"
        else:
            doc = self._session_markdown(self.session_id)
            ext = "md"
        if a:
            path = a if os.path.isabs(a) else os.path.join(os.getcwd(), a)
        else:
            short = self.session_id.split(":")[-1][:8]
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M")
            base = os.environ.get("NBCHAT_EXPORT_DIR") or os.path.join(
                os.path.expanduser("~"), ".nbchat", "exports")
            path = os.path.join(base, f"nbchat-{short}-{ts}.{ext}")
        try:
            d = os.path.dirname(os.path.abspath(path))
            os.makedirs(d, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(doc)
        except Exception as exc:
            return f"export: could not write {path}: {type(exc).__name__}: {exc}"
        return f"export: wrote {len(rows)} messages as {ext} -> {path}"

    # ── /team (tui3: multi-agent team runs, output relayed off-thread) ──

    def _team_emit(self, text: str) -> None:
        """Relay a batch of captured team output into the log (UI thread).

        Invoked on the UI thread via a ``"call"`` event (see
        :class:`_TeamCapture`), so touching the log directly is safe."""
        text = text.rstrip("\n")
        if not text.strip():
            return
        self.log.add(chatc.ChatMessage(role="system", text=text))
        self._ui_refresh()

    def _team_status_text(self) -> str:
        st = self._team_state
        if st["status"] == "running" and (st["thread"] is None
                                          or not st["thread"].is_alive()):
            st["status"] = "done"
        if st["status"] == "running":
            elapsed = (time.monotonic() - st["started"]) if st["started"] else 0
            return (f"team: running — goal: {st['goal'][:80]}\n"
                    f"  elapsed {elapsed:.0f}s  (output streams into the log "
                    f"above; /team stop to interrupt)")
        if st["status"] == "idle":
            return "team: no team run (usage: /team <goal>)"
        lines = [f"team: {st['status']} — goal: {st['goal'][:80]}"]
        if st["report"]:
            lines.append(st["report"].strip())
        return "\n".join(lines)

    def _start_team_run(self, goal: str) -> None:
        from nbchat.core.team import TeamAgent, TeamCoordinator, ToolArbiter

        st = self._team_state
        team_agent = TeamAgent(color=False)
        coordinator = TeamCoordinator(team_agent)
        capture = _TeamCapture(self)
        st["coordinator"] = coordinator
        st["capture"] = capture
        st["goal"] = goal
        st["status"] = "running"
        st["report"] = ""
        st["started"] = time.monotonic()
        st["stopped"] = False

        def _run() -> None:
            old = sys.stdout
            sys.stdout = capture
            try:
                with ToolArbiter():
                    result = coordinator.run(goal)
                result = result or {}
                report = result.get("summary", "") or ""
                status = result.get("status", "done") or "done"
            except Exception as exc:
                report = f"team run crashed: {type(exc).__name__}: {exc}"
                status = "failed"
            finally:
                try:
                    capture.flush()
                except Exception:
                    pass
                sys.stdout = old
            # Finalize.  A user stop takes precedence over the run's own
            # terminal status, so a stopped run reports "stopped", not "done".
            st["report"] = report
            if not st["stopped"]:
                st["status"] = status
            st["thread"] = None

        thread = threading.Thread(target=_run, daemon=True)
        st["thread"] = thread
        thread.start()

    def _cmd_team(self, arg: str) -> str:
        """Run a goal as a team of parallel agents (tui3).

        ``/team <goal>`` starts a coordinated multi-agent run in the
        background; its output is relayed into the log (never to the raw
        screen).  ``/team`` shows the current/last status and report;
        ``/team stop`` interrupts a running team."""
        arg = arg.strip()
        st = self._team_state
        if not arg:
            return self._team_status_text()
        if arg == "stop":
            coordinator = st["coordinator"]
            if st["status"] == "running" and coordinator is not None:
                try:
                    coordinator._interrupt_active_workers()
                except Exception as exc:
                    return f"team: could not stop: {exc}"
                st["stopped"] = True
                st["status"] = "stopped"
                return "team: stop requested (workers are being interrupted)"
            return "team: nothing to stop (no run in progress)"
        if st["status"] == "running" and st["thread"] is not None \
                and st["thread"].is_alive():
            return ("team: a run is already in progress; wait for it to "
                    "finish or type /team stop")
        self._start_team_run(arg)
        return (f"team: starting run for: {arg[:100]}\n"
                f"  workers stream into the log; /team for status, "
                f"/team stop to interrupt")

    def _cmd_hotkeys(self, arg: str) -> str:
        """Generated from the single ``KEYMAP`` source of truth."""
        out = ["hotkeys (normal mode):"]
        for k, desc in KEYMAP["normal"]:
            out.append(f"  {k:<11} {desc}")
        out.append("extra:")
        out.append("  " + " " * 9 + "a            always-approve a pending tool")
        out.append("  " + " " * 9 + "/help        list slash commands (+ TUI v2 extras)")
        out.append("hotkeys (browse mode, Ctrl+O):")
        for k, desc in KEYMAP["browse"]:
            out.append(f"  {k:<11} {desc}")
        return "\n".join(out)

    def _cmd_copy(self, arg: str) -> str:
        last = None
        for m in reversed(self.log.messages):
            if m.role == "assistant" and (m.text or "").strip():
                last = m
                break
        if last is None:
            return "nothing to copy yet"
        text = last.text.strip()
        self._copy_to_clipboard(text)
        return f"copied {len(text)} chars to clipboard"

    def _copy_to_clipboard(self, text: str) -> None:
        """Best-effort OSC 52 clipboard write (no-op if unsupported)."""
        try:
            import base64
            payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
            stream = getattr(self.term, "stdout", None)
            if stream is not None:
                stream.write("\x1b]52;c;" + payload + "\x07")
                stream.flush()
        except Exception:
            pass

    def _save_cfg(self) -> None:
        """Persist the current user settings (best-effort, never raises)."""
        vals = {
            "thinking_visible": bool(self._thinking_visible),
            "notify_toasts": bool(self._notify.toasts),
            "notify_bel": bool(self._notify.bel),
            "notify_sound": bool(self._notify.sound),
            "approval_enabled": bool(self._approval_enabled),
            "risky_tools": sorted(self._risky_tools),
            "scroll_tick": int(self._scroll_tick),
        }
        # Preserve the 'auto' theme preference (a setting, not a concrete
        # theme) so auto-detect re-runs on the next start; otherwise persist
        # the active theme name.
        if self._cfg.get("theme") != "auto":
            vals["theme"] = theme.current().name
        self._cfg.update(vals)
        config.save(self._cfg)

    def _cmd_btw(self, arg: str) -> None:
        if not arg:
            self._note("usage: /btw <question>  (kept out of this session)")
            return
        if self.busy:
            self._note("btw: wait for the current turn to finish first")
            return
        self._note(f"btw: {arg}  (asking…)")
        t = threading.Thread(target=self._btw_worker, args=(arg,),
                             name="btw-side", daemon=True)
        t.start()

    def _cmd_approve(self, arg: str) -> str:
        """Manage the tool-approval gate.

        ``/approve`` — status.  ``/approve on|off`` — toggle.
        ``/approve add <tool>`` / ``/approve rm <tool>`` — adjust the
        set of tools that require confirmation.
        """
        import nbchat.tools as _tools
        known = sorted(t.name for t in _tools.TOOLS)
        a = arg.split(None, 1)
        sub = a[0].lower() if a else ""
        rest = a[1].strip() if len(a) > 1 else ""
        if sub in ("on", "enable"):
            self._approval_enabled = True
            self._save_cfg()
            return "tool approval ON — risky tools will prompt"
        if sub in ("off", "disable"):
            self._approval_enabled = False
            self._save_cfg()
            return "tool approval OFF — tools run without prompting"
        if sub == "add" and rest:
            self._risky_tools.add(rest)
            self._save_cfg()
            return f"approval now required for: {sorted(self._risky_tools)}"
        if sub in ("rm", "remove") and rest:
            self._risky_tools.discard(rest)
            self._save_cfg()
            return f"approval no longer required for: {rest}; " \
                   f"risky = {sorted(self._risky_tools)}"
        if sub == "list":
            return (f"risky tools: {sorted(self._risky_tools)}\n"
                    f"all tools: {known}")
        # Default: show status.
        state = "ON" if self._approval_enabled else "OFF"
        return (f"tool approval {state} · prompts before: "
                f"{sorted(self._risky_tools)}")

    def _cmd_notify(self, arg: str) -> str:
        """Manage the in-TUI notification stack.

        ``/notify`` — status.  ``/notify toasts|bel|sound on|off`` —
        toggle a channel.  ``/notify test [kind]`` — fire a test toast.
        """
        a = arg.split()
        sub = a[0].lower() if a else ""
        val = a[1].lower() if len(a) > 1 else ""
        n = self._notify
        if sub in ("toasts", "bel", "sound"):
            if val in ("on", "1", "true"):
                setattr(n, sub, True)
            elif val in ("off", "0", "false"):
                setattr(n, sub, False)
            else:
                setattr(n, sub, not getattr(n, sub))
            self._save_cfg()
            return f"{sub}: {getattr(n, sub)}"
        if sub == "test":
            kind = val if val in ("ok", "warn", "error", "info") else "ok"
            n.push("test", f"toast ({kind})", kind, write=self._term_write)
            self._ui_refresh()
            return f"test {kind} toast fired"
        if sub:
            return "usage: /notify [toasts|bel|sound] [on|off] · /notify test [kind]"
        return (f"notifications — toasts: {n.toasts} · bel: {n.bel} · "
                f"sound: {n.sound}")

    def _apply_auto_theme(self) -> None:
        """Auto light/dark (herdr #7): if the configured theme is 'auto',
        query the terminal's appearance (DECSTERA) and switch to light or
        dark.  Best-effort: on an unsupported terminal the app stays on the
        default (dark).  Called once after raw mode is entered, before the
        render loop reads the input fd.
        """
        if os.environ.get("NBCHAT_NO_AUTO_THEME"):
            return
        if self._cfg.get("theme") != "auto":
            return
        from . import theme
        appearance = None
        try:
            appearance = self.term.probe_appearance()
        except Exception:
            appearance = None
        chosen = "light" if appearance == "light" else (
            "dark" if appearance == "dark" else None)
        if chosen is None:
            theme.set_active("dark")
            return
        theme.set_active(chosen)
        for msg in self.log.messages:
            msg.invalidate()
        self._cfg["theme"] = "auto"  # persist the preference (re-detect)
        try:
            self._save_cfg()
        except Exception:
            pass

    def _cmd_theme(self, arg: str) -> str:
        """Switch the colour theme (tui3 wave 5).

        ``/theme`` — current theme.  ``/theme <name>`` — set (dark, light,
        prime).  The switch is live: the proxy in ``theme.py`` retargets so
        every component re-colours on the next render, and the choice is
        persisted.
        """
        from . import theme
        name = arg.strip().lower()
        real = {t.name for t in theme.all_themes()}
        if not name:
            names = ", ".join(t.name for t in theme.all_themes())
            return (f"theme: {theme.current().name}  "
                    f"(available: {names}, auto)")
        if name == "auto":
            # 'auto' is a preference, not a theme: persist it so every start
            # re-detects the terminal's appearance (DECSTERA).  The actual
            # probe runs at startup (no input-reader contention mid-session).
            self._cfg["theme"] = "auto"
            self._save_cfg()
            cur = theme.current().name
            if os.environ.get("NBCHAT_NO_AUTO_THEME"):
                return "auto theme disabled (NBCHAT_NO_AUTO_THEME)"
            return (f"theme: auto (auto-detects light/dark each start; "
                    f"this session: {cur})")
        if name not in real:
            names = ", ".join(t.name for t in theme.all_themes())
            return f"unknown theme '{name}'  (available: {names}, auto)"
        applied = theme.set_active(name)
        # Force the logged turns to re-render with the new colours.
        for msg in self.log.messages:
            msg.invalidate()
        self._cfg["theme"] = applied.name
        self._save_cfg()
        self._notify.push("theme", applied.name, "ok", write=self._term_write)
        return f"theme: {applied.name}"

    def _goal_first_prompt(self, objective: str) -> str:
        return ("Work toward this goal: " + objective
                + "\nWhen the goal is fully achieved, reply with the exact "
                  "line 'GOAL COMPLETE' plus a one-line summary.")

    def _cmd_goal(self, arg: str) -> str:
        """Manage the running /goal objective.

        ``/goal`` — status.  ``/goal <objective>`` — start a goal.
        ``/goal stop`` — stop auto-continue (current turn finishes).
        ``/goal clear`` — clear the goal entirely.
        ``/goal budget <n>`` — set the auto-continue turn budget.
        """
        a = arg.split(None, 1)
        sub = a[0].lower() if a else ""
        rest = a[1].strip() if len(a) > 1 else ""
        g = self._goal
        if sub == "stop" and g is not None:
            g["stopped"] = True
            return "goal: stopping auto-continue (current turn finishes)"
        if sub in ("stop", "clear"):
            self._goal = None
            return "goal: cleared"
        if sub == "budget":
            if not rest.isdigit() or int(rest) < 1:
                return "usage: /goal budget <n>"
            self._goal_budget = int(rest)
            return f"goal turn budget set to {self._goal_budget}"
        if not arg:
            if g is None:
                return ("no active goal · /goal <objective> to start "
                        f"(default {self._goal_budget}-turn budget)")
            state = "running" if not g.get("stopped") else "stopped"
            return (f"goal ({state}): {g['objective']}\n"
                    f"auto-turns: {g['done']}/{g['budget']}\n"
                    "/goal stop · /goal clear · /goal budget <n>")
        # /goal <objective> — start a new goal.
        objective = arg
        self._goal = {"objective": objective,
                      "remaining": self._goal_budget,
                      "budget": self._goal_budget, "done": 0,
                      "stopped": False}
        if self.busy:
            return (f"goal set: {objective}\n"
                    "(a turn is in flight — auto-continue starts after it "
                    "finishes)")
        self._start_turn(self._goal_first_prompt(objective))
        return f"goal started: {objective} (budget {self._goal_budget})"

    def _cmd_autonomous(self, arg: str) -> str:
        """``/autonomous`` - autonomous run with an approval gate.

        Like ``/goal`` (auto-continue toward an objective), but PAUSES after
        each turn and asks you to continue instead of silently chaining.
        Commands:

        - ``/autonomous <objective>`` - start a gated autonomous run.
        - ``/autonomous <objective> --auto`` - start silently (like ``/goal``).
        - ``/autonomous go`` - run the next continuation turn (gate stays on).
        - ``/autonomous auto`` - switch to silent auto-continue + next turn.
        - ``/autonomous stop`` / ``/autonomous clear`` - stop the run.
        - ``/autonomous`` - status.
        """
        toks = (arg or "").split()
        if len(toks) == 1 and toks[0].lower() in ("go", "auto", "stop", "clear"):
            sub = toks[0].lower()
            g = self._goal
            if sub == "go":
                if g is None or g.get("stopped"):
                    return "autonomous: no active goal to continue"
                nxt = self._goal_next_prompt()
                if nxt is None:
                    return "autonomous: nothing to continue (goal done or budget hit)"
                self._turn_thread = None
                if self._tui._running:
                    self._start_turn(nxt)
                return "autonomous: continuing (gate still on)"
            if sub == "auto":
                self._autonomous_gate = False
                if g is None or g.get("stopped"):
                    return "autonomous: no active goal (auto-continue off)"
                nxt = self._goal_next_prompt()
                if nxt is not None:
                    self._turn_thread = None
                    if self._tui._running:
                        self._start_turn(nxt)
                return "autonomous: auto-continue on (gate off)"
            # stop / clear
            self._autonomous_gate = False
            if g is not None:
                g["stopped"] = True
            self._goal = None
            return "autonomous: stopped and cleared"
        if not toks:
            g = self._goal
            if g is None:
                return ("no active autonomous run - /autonomous <objective> to "
                        "start (gated auto-continue; --auto for silent)")
            gate = "on" if self._autonomous_gate else "off (auto)"
            state = "running" if not g.get("stopped") else "stopped"
            return (f"autonomous ({state}, gate {gate}): {g['objective']}\n"
                    f"turns: {g['done']}/{g['budget']}\n"
                    "/autonomous go - /autonomous auto - /autonomous stop")
        # Start a new run: /autonomous <objective> [--auto]
        silent = toks[-1].lower() in ("--auto", "-a")
        objective = " ".join(toks[:-1]) if silent else arg
        if not objective:
            return "autonomous: no objective given"
        self._goal = {"objective": objective,
                      "remaining": self._goal_budget,
                      "budget": self._goal_budget, "done": 0,
                      "stopped": False}
        self._autonomous_gate = not silent
        mode = "silent" if silent else "gated"
        if self.busy:
            return (f"autonomous set ({mode}): {objective}\n"
                    "(a turn is in flight - it starts after it finishes)")
        self._start_turn(self._goal_first_prompt(objective))
        return f"autonomous started ({mode}): {objective} (budget {self._goal_budget})"

    def _btw_worker(self, arg: str) -> None:
        try:
            reply = self._send_side_question(arg)
        except Exception as exc:
            reply = f"(error: {type(exc).__name__}: {exc})"
        self._note(f"btw reply: {reply}" if reply else "btw reply: (empty)")

    def _send_side_question(self, arg: str) -> str:
        """Answer a side question on an isolated throwaway agent.

        It runs under a ``btw:``-prefixed session id (never listed by
        ``/sessions``), so the question and answer stay out of this
        session's history, context and picker.  All of its print/stream
        output is captured so it cannot corrupt the raw-mode screen.
        """
        import uuid
        from nbchat.tui.agent import TerminalAgent

        side = TerminalAgent(color=False)
        side.session_id = "btw:" + uuid.uuid4().hex[:12]
        side._refine_hook_state = {"running": True}  # never auto-refine
        cap = io.StringIO()
        old = sys.stdout
        sys.stdout = cap
        try:
            reply = side.send(arg)
        finally:
            sys.stdout = old
        return (reply or "").strip()

    def _cmd_compact(self, arg: str) -> str:
        if self.busy:
            return "compact: wait for the current turn to finish"
        rep = self.force_compact(arg)
        if not rep.get("compacted"):
            return f"compact: {rep.get('reason', 'nothing to do')}"
        from nbchat.tui.status import _humanise
        return (
            f"compact: {rep['before_rows']}\u2192{rep['window_rows']} rows "
            f"\u00b7 ~{_humanise(rep['before_tokens'])}"
            f"\u2192~{_humanise(rep['after_tokens'])} tok "
            f"(budget {_humanise(rep['budget'])})"
            + (f" \u00b7 focus: {rep['instructions']}"
               if rep.get("instructions") else "")
        )

    def _cmd_refine(self, arg: str) -> str:
        if arg.lower().startswith("rollback"):
            from nbchat.core import refinement as _rf
            rep = _rf.undo_last_round(self.session_id)
            if rep.get("error"):
                return f"refine rollback: {rep['error']}"
            n = len(rep.get("reverted", []))
            return (f"refine rollback: reverted round "
                    f"{rep.get('round_id', '?')} ({n} change(s))")
        from nbchat.core import refine_hook
        if not refine_hook.schedule_manual_refine(self, arg):
            return ("refine: engine disabled (refine_hook_enabled) "
                    "or a round is already running")
        return "refine: round scheduled — result appears when it finishes"

    def _cmd_lessons(self, arg: str) -> str:
        from nbchat.core import db
        n = 20
        if arg.isdigit():
            n = max(1, int(arg))
        lessons = db.load_lessons(self.session_id, limit=n)
        if not lessons:
            return "lessons: (none recorded yet)"
        lines = [f"lessons ({len(lessons)}):"]
        for les in lessons:
            scope = "g" if les.get("scope") == "global" else "s"
            tail = f"  (r{les['round_id']})" if les.get("round_id") else ""
            lines.append(f"  [{scope}] {les.get('content', '')[:100]}{tail}")
        return "\n".join(lines)

    def _cmd_memory(self, arg: str) -> str:
        from nbchat.core import db
        cm = db.get_core_memory(self.session_id) or {}
        lines = ["memory (L1 core):"]
        if cm:
            for key in ("goal", "constraints", "rationale", "recent_errors"):
                v = cm.get(key)
                if v:
                    lines.append(f"  {key}: {str(v)[:140]}")
        else:
            lines.append("  (empty)")
        try:
            with db._connect() as conn:
                ep = conn.execute(
                    "SELECT COUNT(*) FROM episodic_store WHERE session_id=?",
                    (self.session_id,)).fetchone()[0]
            lines.append(f"memory (L2 episodic): {ep} row(s)")
        except Exception:
            pass
        return "\n".join(lines)
    # ── UI ──────────────────────────────────────────────────────────────

    def _ui_refresh(self) -> None:
        try:
            self.events.put("render")
        except Exception:
            pass

    def _live_rows(self, w: int) -> List[Line]:
        """The in-flight turn: thinking blocks, tool panels, answer text."""
        if not (self._stream_blocks or self._stream_text):
            return []
        rows: List[Line] = []
        for b in self._stream_blocks:
            if b.kind == "tool":
                rows.extend(chatc.ToolCall(
                    b.name, title=b.title, status=b.status,
                    body=b.body, show_diff=b.diff,
                ).render(w))
            elif b.kind == "thinking":
                if not self._thinking_visible:
                    continue
                rows.extend(chatc.ThinkingBlock(
                    b.text, collapsed=False, title=b.title or "thinking",
                ).render(w))
        if self._stream_text:
            rows.extend(chatc.Message(
                "assistant", _strip_markup(self._stream_text)).render(w))
        return rows

    def _toggle_thinking(self) -> None:
        """Ctrl+T: show / hide reasoning blocks (live turn + logged turns)."""
        self._thinking_visible = not self._thinking_visible
        # Rebuild each logged assistant message's blocks from the full copy
        # so the toggle is lossless (no thinking text is dropped).
        for msg in self.log.messages:
            full = getattr(msg, "_full_blocks", None)
            if not full:
                continue
            if self._thinking_visible:
                msg.blocks = list(full)
            else:
                msg.blocks = [b for b in full if b.kind != "thinking"] or None
            msg.invalidate()
        self._note("thinking " + ("shown" if self._thinking_visible else "hidden"))
        self._save_cfg()

    # ── session picker modal ────────────────────────────────────────────

    def _cmd_pin(self, arg: str) -> str:
        """``/pin`` — pin the current session to the top of the picker."""
        import nbchat.core.db as _db
        try:
            _db._meta_set(self.session_id, "pinned", "1")
            return "pinned " + self.session_id + " (top of the session picker)"
        except Exception as e:
            return "pin: " + type(e).__name__ + ": " + str(e)

    def _cmd_unpin(self, arg: str) -> str:
        """``/unpin`` — unpin the current session."""
        import nbchat.core.db as _db
        try:
            _db._meta_set(self.session_id, "pinned", "")
            return "unpinned " + self.session_id
        except Exception as e:
            return "unpin: " + type(e).__name__ + ": " + str(e)

    @staticmethod
    def _parse_onoff(val: str):
        v = (val or "").strip().lower()
        if v in ("on", "1", "true", "yes", "y"):
            return True
        if v in ("off", "0", "false", "no", "n"):
            return False
        return None

    def _settings_view(self) -> str:
        lines = [
            "TUI settings (persisted to ~/.nbchat/tui3.json):",
            f"  theme     = {theme.current().name}    (dark|light|prime)",
            f"  scroll    = {self._scroll_tick}    (lines per page up/down)",
            f"  thinking  = {'on' if self._thinking_visible else 'off'}",
            f"  toasts    = {'on' if self._notify.toasts else 'off'}    (turn-complete / idle toasts)",
            f"  bell      = {'on' if self._notify.bel else 'off'}    (BEL on attention events)",
            f"  sound     = {'on' if self._notify.sound else 'off'}    (needs NBCHAT_SOUND_DIR)",
            f"  approve   = {'on' if self._approval_enabled else 'off'}    (tool-approval gate)",
            f"  risky     = {', '.join(sorted(self._risky_tools))}",
            "",
            "set a value:  /settings <key> <value>   (e.g. /settings theme prime)",
        ]
        return "\n".join(lines)

    def _cmd_settings(self, arg: str) -> str:
        """``/settings [key value...]`` — view or live-tune TUI settings."""
        parts = arg.split()
        if not parts:
            return self._settings_view()
        key = parts[0].lower()
        val = " ".join(parts[1:]).strip()
        if key == "theme":
            valid = [t.name for t in theme.all_themes()] + ["auto"]
            if not val or val.lower() not in valid:
                return (f"usage: /settings theme {'|'.join(valid)} "
                        f"(current: {theme.current().name})")
            v = val.lower()
            if v == "auto":
                # A preference, not a theme: persist it; every start re-detects.
                self._cfg["theme"] = "auto"
                self._save_cfg()
                return "theme -> auto (auto-detects light/dark each start)"
            theme.set_active(v)
            self._save_cfg()
            return "theme -> " + v
        if key == "scroll":
            if not val or not val.isdigit() or int(val) < 1:
                return f"usage: /settings scroll <lines>=1 (current: {self._scroll_tick})"
            self._scroll_tick = max(1, int(val))
            self._save_cfg()
            return f"scroll -> {self._scroll_tick} line(s) per page"
        if key in ("thinking", "toasts", "bell", "sound", "approve"):
            flag = self._parse_onoff(val)
            if flag is None:
                return f"usage: /settings {key} on|off"
            if key == "thinking":
                self._thinking_visible = flag
            elif key == "toasts":
                self._notify.toasts = flag
            elif key == "bell":
                self._notify.bel = flag
            elif key == "sound":
                self._notify.sound = flag
            else:
                self._approval_enabled = flag
            self._save_cfg()
            return f"{key} -> {'on' if flag else 'off'}"
        if key == "risky":
            tools = [t for t in val.split() if t.strip()]
            if not tools:
                return "usage: /settings risky <tool1 tool2 ...>"
            self._risky_tools = set(tools)
            self._save_cfg()
            return "risky tools -> " + ", ".join(sorted(tools))
        return f"unknown setting {key!r} (run /settings for the list)"

    def _todo_pill(self) -> str:
        """A live progress pill for the agent's task list (cached ~0.4 s)."""
        now = time.monotonic()
        cache = getattr(self, "_todo_pill_cache", None)
        if cache is not None and now - cache[0] < 0.4:
            return cache[1]
        text = ""
        try:
            from nbchat.tools.todo import load_todos
            todos = load_todos()
            if todos:
                done = sum(1 for t in todos if t.get("done"))
                text = f"tasks {done}/{len(todos)}"
        except Exception:
            text = ""
        self._todo_pill_cache = (now, text)
        return text

    def _queue_pill(self) -> str:
        """A live pill for the steering queue (Ctrl+Q) when non-empty."""
        if self._queue:
            return f"{len(self._queue)} queued"
        return ""

    def _cmd_todos(self, arg: str) -> str:
        """``/todos`` — show the agent's current task list."""
        from nbchat.tools.todo import load_todos
        todos = load_todos()
        if not todos:
            return "no active task list (the agent sets one via the todo tool)"
        done = sum(1 for t in todos if t.get("done"))
        lines = [f"task list ({done}/{len(todos)} done):"]
        for t in todos:
            mark = "x" if t.get("done") else " "
            lines.append(f"  [{mark}] {t.get('text', '')}")
        return "\n".join(lines)

    def _cmd_project(self, arg: str) -> str:
        """``/project`` - show the project-instruction file auto-loaded at
        start (AGENTS.md / CLAUDE.md in the cwd or git root)."""
        path = getattr(self, "_project_instr_path", "") or _find_project_instructions()
        if not path:
            return ("no project instructions found (looking for AGENTS.md / "
                    "CLAUDE.md in the working dir and git root; "
                    "NBCHAT_NO_PROJECT_INSTRUCTIONS=1 disables auto-load)")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception:
            return "project file not readable: " + path
        lines = text.splitlines()
        head = lines[:10]
        more = len(lines) - len(head)
        out = ["project instructions: " + path + " (" + str(len(text)) + " bytes, "
               + str(len(lines)) + " lines, auto-loaded into the system prompt)"]
        out.append("-" * 40)
        out.extend(head)
        if more > 0:
            out.append("\u2026 +" + str(more) + " more lines (use a tool to read the full file)")
        return "\n".join(out)

    @staticmethod
    def _parse_duration(s: str):
        """Parse a duration like ``5`` / ``30s`` / ``5m`` / ``1h`` to seconds.

        Returns ``None`` when unparseable or < 1 second.
        """
        import re as _re
        m = _re.match(r"^(\d+(?:\.\d+)?)\s*([smh]?)$", (s or "").strip().lower())
        if not m:
            return None
        val = float(m.group(1))
        unit = m.group(2)
        if unit == "m":
            val *= 60.0
        elif unit == "h":
            val *= 3600.0
        if val < 1.0:
            return None
        return val

    def _tick_heartbeat(self) -> None:
        """Fire the recurring instruction when the interval has elapsed and
        the session is idle.  Called from the frame builder on every render
        tick.  Never interrupts a running turn (it defers until idle)."""
        if not self._heartbeat or self._heartbeat_interval <= 0:
            return
        now = time.monotonic()
        if now - self._heartbeat_last < self._heartbeat_interval:
            return
        running = (self._turn_thread is not None
                   and self._turn_thread.is_alive())
        if running:
            return  # defer until idle; the elapsed window is preserved
        self._heartbeat_last = now
        try:
            self._start_turn("[heartbeat] " + self._heartbeat)
        except Exception:
            pass

    def _cmd_heartbeat(self, arg: str) -> str:
        """``/heartbeat`` - manage a recurring instruction.

        ``/heartbeat every <dur> <instruction>`` fires <instruction> as a
        turn every <dur> while the session is idle (dur: 5 / 30s / 5m / 1h).
        ``/heartbeat clear`` stops it.  No arg shows the current heartbeat.
        """
        a = (arg or "").strip()
        if not a:
            if self._heartbeat:
                return ("heartbeat: every %gs -> %r"
                        % (self._heartbeat_interval, self._heartbeat))
            return ("heartbeat: none (use /heartbeat every <dur> <instruction>)")
        low = a.lower()
        if low in ("clear", "off", "stop"):
            self._heartbeat = ""
            self._heartbeat_interval = 0.0
            return "heartbeat: cleared"
        toks = a.split(None, 2)
        if toks and toks[0].lower() == "every" and len(toks) >= 3:
            dur = self._parse_duration(toks[1])
            if dur is None:
                return "heartbeat: bad duration (use e.g. 5 / 30s / 5m / 1h)"
            instr = toks[2].strip()
            if not instr:
                return "heartbeat: empty instruction"
            self._heartbeat = instr
            self._heartbeat_interval = dur
            self._heartbeat_last = time.monotonic()
            return "heartbeat: every %gs -> %r" % (dur, instr)
        return ("heartbeat: usage: /heartbeat every <dur> <instruction>, "
                "/heartbeat clear, or /heartbeat (status)")

    def _open_picker(self) -> None:
        from nbchat.core import db
        self._modal_kind = "session"
        rows = db.list_sessions_with_title("tui:")
        # Per-session message counts (one pass).
        counts: dict = {}
        try:
            with db._connect() as conn:
                for sid, n in conn.execute(
                        "SELECT session_id, COUNT(*) FROM chat_log "
                        "GROUP BY session_id"):
                    counts[sid] = n
        except Exception:
            pass
        # Pinned sessions sort to the top of the picker (a per-session
        # "pinned" flag in session_meta; set with /pin and /unpin).
        pinned = set()
        try:
            with db._connect() as conn:
                for (psid,) in conn.execute(
                        "SELECT session_id FROM session_meta "
                        "WHERE key='pinned' AND value='1'"):
                    pinned.add(psid)
        except Exception:
            pass
        entries = []
        for r in rows:
            sid = r["session_id"]
            title = (r.get("title") or "").strip()
            short = sid.rsplit(":", 1)[-1][:10]
            label = (title or short)
            pin = "\u2605 " if sid in pinned else ""
            tail = f" · {counts.get(sid, 0)} msg"
            tail += f" · {_fmt_ts(r.get('last_ts'))}"
            if sid == self.session_id:
                tail += "  (current)"
            entries.append((sid, pin + label + tail, sid in pinned))
        entries.sort(key=lambda e: e[2], reverse=True)  # pinned first
        self._picker_sessions = [(s, lab) for s, lab, _p in entries]
        self._picker_filter = ""
        self._refresh_picker()
        self._ui_refresh()

    # ── Ctrl+P command palette ─────────────────────────────────────────

    # (text inserted into the editor, human label). The label is what the
    # fuzzy filter matches on, so keep them unique.
    _PALETTE = (
        ("/help",              "list all commands"),
        ("/context",           "model · context bar · compression"),
        ("/compact ",          "manual one-shot compaction"),
        ("/refine ",           "schedule a refinement round"),
        ("/refine rollback",   "revert the last refinement"),
        ("/lessons",           "applied refinement lessons"),
        ("/memory",            "L1 core + L2 episodic memory"),
        ("/btw ",              "throwaway side question"),
        ("/sessions",          "list saved tui sessions"),
        ("/load ",             "load a session by id"),
        ("/new",               "start a fresh session"),
        ("/save",              "save the current session"),
        ("/title ",            "set the session title"),
        ("/name ",             "alias for /title"),
        ("/copy",              "copy last reply to clipboard"),
        ("/hotkeys",           "keybinding reference"),
        ("/quit",              "exit tui2"),
        ("! ",                 "run a shell command  (!cmd)"),
        ("!! ",                "run shell, store output (!!cmd)"),
    )

    def _open_palette(self) -> None:
        self._modal_kind = "palette"
        self._picker_sessions = list(self._PALETTE)
        self._picker_filter = ""
        self._refresh_picker()
        self._ui_refresh()

    # ── Arrow-key history recall (Up/Down) ────────────────────────────

    def _history_recall(self, direction: int) -> None:
        """Walk the input history.  ``direction`` is -1 (Up/older) or
        +1 (Down/newer).  ``_hist_pos`` is ``None`` while free-typing; the
        first Up snapshots the in-progress draft into ``_hist_draft`` and
        steps back one entry.  Down past the newest entry restores the
        draft and returns to free-typing."""
        hist = self._history
        if not hist:
            return
        pos = self._hist_pos
        if pos is None:
            if direction > 0:
                return  # Down while free-typing: nothing to advance to
            self._hist_draft = self.editor.text()
            pos = len(hist)          # one past the newest
        if direction < 0:
            if pos > 0:
                pos -= 1
                self.editor.set_text(hist[pos])
        else:
            if pos < len(hist) - 1:
                pos += 1
                self.editor.set_text(hist[pos])
            else:
                # Past the newest: restore the draft, stop recalling.
                self.editor.set_text(self._hist_draft)
                self._hist_draft = ""
                self._hist_pos = None
                self._ui_refresh()
                return
        self._hist_pos = pos
        self._ui_refresh()

    # ── Ctrl+R reverse search ──────────────────────────────────────────

    def _open_search(self) -> None:
        self._modal_kind = "search"
        # Newest first; key = the text to insert, label = a short preview.
        self._picker_sessions = [
            (h, h if len(h) <= 48 else h[:47] + "…")
            for h in reversed(self._history)
        ]
        self._picker_filter = ""
        self._refresh_picker()
        self._ui_refresh()

    # Per-modal title + footer for the shared picker widget.
    _MODAL_META = {
        "session": ("sessions",
                    "type to filter · ↑↓ move · enter load · esc cancel"),
        "palette": ("commands",
                    "type to filter · ↑↓ move · enter insert · esc cancel"),
        "search": ("history",
                   "type to filter · ↑↓ move · enter insert · esc cancel"),
    }

    def _refresh_picker(self) -> None:
        from .components import SelectList
        from .fuzzy import fuzzy_rank
        title, footer = self._MODAL_META.get(
            self._modal_kind, self._MODAL_META["session"])
        if not self._picker_sessions:
            self._picker_rows = []
            self._picker = SelectList(title=f"{title}  (0)",
                                      items=["(no entries)"], footer=footer)
            return
        needle = self._picker_filter.strip().lower()
        if needle:
            ranked = fuzzy_rank(needle,
                                [lab for _sid, lab in self._picker_sessions])
            keep = {item for item, _m in ranked}
            ordered = [item for item, _m in ranked]
            rows = [(sid, lab) for sid, lab in self._picker_sessions
                    if lab in keep]
            rows.sort(key=lambda _pair: ordered.index(_pair[1]))
        else:
            rows = list(self._picker_sessions)
        labels = [lab for _sid, lab in rows]
        # For the session modal, keep the cursor on the current session.
        sel = 0
        if self._modal_kind == "session":
            for i, (sid, _lab) in enumerate(rows):
                if sid == self.session_id:
                    sel = i
                    break
        self._picker_rows = rows
        self._picker = SelectList(
            title=f"{title}  ({len(rows)}/{len(self._picker_sessions)})",
            items=labels, selected=sel, footer=footer)

    def _close_picker(self) -> None:
        self._picker = None
        self._picker_filter = ""
        self._picker_rows = []
        self._ui_refresh()

    def _picker_key(self, key: Key) -> None:
        if key.name in ("escape", "esc"):
            self._close_picker()
            return
        if key.name == "enter":
            self._picker_select()
            return
        if key.name == "up":
            self._picker.select(self._picker.selected - 1)
            self._ui_refresh()
            return
        if key.name == "down":
            self._picker.select(self._picker.selected + 1)
            self._ui_refresh()
            return
        if key.name == "backspace":
            self._picker_filter = self._picker_filter[:-1]
            self._refresh_picker()
            self._ui_refresh()
            return
        ch = _key_text(key)
        if ch:
            self._picker_filter += ch
            self._refresh_picker()
            self._ui_refresh()

    def _picker_select(self) -> None:
        if not self._picker_rows:
            self._close_picker()
            return
        key, _label = self._picker_rows[self._picker.selected]
        if self._modal_kind == "session":
            sid = key
            self._close_picker()
            if sid == self.session_id:
                return
            self._switch_session(sid)
            self._session_changed()
            self.remember_session(self.session_id)
            self._note(f"loaded session {self.session_id.rsplit(':', 1)[-1]}")
            self._ui_refresh()
            return
        # palette / search: drop the chosen text into the editor for
        # confirmation (the user still presses Enter to actually run it).
        text = key if isinstance(key, str) else str(key)
        self._close_picker()
        self.editor.handle("paste", text)
        self._ui_refresh()

    # ── @-file completion (tui3) ─────────────────────────────────────────

    def _active_at_token(self):
        """Return ``(start, end, query)`` for the active ``@token`` on the
        current editor line, or None.  The token is the run of
        non-whitespace from the last ``@`` (preceded by line-start or
        whitespace) up to the cursor.  An ``@`` inside an email
        (``user@x``) is not a token start because it is not preceded by
        whitespace."""
        ed = self.editor
        line = ed.lines[ed.cursor_line]
        col = ed.cursor_col
        seg = line[:col]
        at = seg.rfind("@")
        if at == -1:
            return None
        query = seg[at + 1:]
        if " " in query or "\t" in query:
            return None
        if at > 0 and not line[at - 1].isspace():
            return None
        return at, col, query

    def _file_list(self):
        """Relative file+dir paths under cwd, walked once per cwd (cached).
        Directories carry a trailing ``/``.  Hidden and VCS/build dirs are
        pruned; the walk is capped so a huge tree never hangs a keystroke."""
        import os as _os
        cwd = _os.getcwd()
        cache = self._file_list_cache
        if cache is not None and cache[0] == cwd:
            return cache[1]
        ignore = {".git", "node_modules", "__pycache__", ".venv", "venv",
                  "dist", "build", ".idea", ".pytest_cache", ".mypy_cache",
                  ".ruff_cache", ".tox", ".eggs"}
        paths: list = []
        cap = 20000
        for root, dirs, files in _os.walk(cwd):
            dirs[:] = [d for d in dirs
                       if d not in ignore and not d.startswith(".")]
            rel = _os.path.relpath(root, cwd)
            base = "" if rel == "." else rel.replace(_os.sep, "/")
            prefix = base + "/" if base else ""
            for d in dirs:
                if len(paths) >= cap:
                    break
                paths.append(prefix + d + "/")
            for f in files:
                if len(paths) >= cap:
                    break
                paths.append(prefix + f)
        self._file_list_cache = (cwd, paths)
        return paths

    def _filecomp_cap(self):
        h = self.term.height
        return max(3, min(6, h - 12))

    # @-completion frecency (survey #8): recently-used @-files rank higher.
    _FILECOMP_RECENCY_MAX = 50

    def _filecomp_recency_file(self) -> str:
        env = os.environ.get("NBCHAT_FILECOMP_RECENCY")
        if env:
            return env
        return os.path.join(os.path.expanduser("~/.nbchat"),
                              "tui3-filecomp-recency.json")

    def _filecomp_recency(self):
        try:
            with open(self._filecomp_recency_file()) as f:
                data = json.load(f)
            if isinstance(data, list):
                return [p for p in data if isinstance(p, str)][:self._FILECOMP_RECENCY_MAX]
        except Exception:
            pass
        return []

    def _filecomp_note_accept(self, path):
        try:
            rec = self._filecomp_recency()
            rec = [p for p in rec if p != path]
            rec.insert(0, path)
            rec = rec[:self._FILECOMP_RECENCY_MAX]
            fpath = self._filecomp_recency_file()
            d = os.path.dirname(fpath)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(rec, f)
        except Exception:
            pass

    def _file_matches(self, query):
        paths = self._file_list()
        cap = self._filecomp_cap()
        q = (query or "").strip()
        if q:
            ranked = fuzzy_rank(q, paths, limit=cap)
            matches = [p for p, _m in ranked][:cap]
        else:
            matches = paths[:cap]
        # Frecency boost (survey #8): recently-used files that are already in
        # the fuzzy top jump to the front.  Best-effort; a hiccup never blocks
        # completion.
        rec = self._filecomp_recency()
        if rec:
            order = {p: i for i, p in enumerate(rec)}
            matches.sort(key=lambda p: (0, order[p]) if p in order else (1, 0))
        return matches[:cap]

    def _maybe_open_filecomp(self):
        tok = self._active_at_token()
        if tok is None:
            if self._filecomp is not None:
                self._filecomp = None
            return
        start, end, query = tok
        matches = self._file_matches(query)
        fc = self._filecomp
        if fc is None:
            self._filecomp = {"query": query, "matches": matches,
                              "idx": 0, "start": start, "end": end}
        else:
            fc["query"] = query
            fc["matches"] = matches
            fc["start"] = start
            fc["end"] = end
            if fc["idx"] >= len(matches):
                fc["idx"] = max(0, len(matches) - 1)

    def _filecomp_key(self, key):
        fc = self._filecomp
        name = key.name
        if name in ("up", "k"):
            fc["idx"] = max(0, fc["idx"] - 1)
            return True
        if name in ("down", "j"):
            fc["idx"] = min(max(0, len(fc["matches"]) - 1), fc["idx"] + 1)
            return True
        if name in ("enter", "tab"):
            self._filecomp_accept()
            return True
        if name in ("escape", "esc"):
            self._filecomp = None
            return True
        return False  # let it reach the editor (grows/shrinks the @token)

    def _filecomp_accept(self):
        fc = self._filecomp
        self._filecomp = None
        if not fc or not fc.get("matches"):
            return
        idx = min(fc["idx"], len(fc["matches"]) - 1)
        chosen = fc["matches"][idx]
        self._filecomp_note_accept(chosen)  # frecency: remember this choice
        ed = self.editor
        tok = self._active_at_token()
        if tok is None:
            ed.replace_range(ed.cursor_col, ed.cursor_col, chosen)
        else:
            start, end, _q = tok
            ed.replace_range(start, end, chosen)

    def _filecomp_render(self, w):
        fc = self._filecomp
        if fc is None or not fc.get("matches"):
            return []
        inner = max(w - 4, 8)
        cap = self._filecomp_cap()
        matches = fc["matches"][:cap]
        title = "@" + fc["query"] if fc.get("query") else "@ files"
        lines = []
        for i, pth in enumerate(matches):
            shown = pth if len(pth) <= inner - 2 else pth[:inner - 2]
            if i == fc["idx"]:
                lines.append(Line(segments=[
                    Segment("\u276f ", DARK.accent),
                    Segment(" " + shown, DARK.accent),
                ]))
            else:
                lines.append(_to_lines("   " + shown, inner)[0])
        return Box(title=title, lines=lines, clip=True).render(w)

    def _scroll_log(self, delta: int) -> None:
        """PgUp/PgDn: page through the conversation log (lines up from the
        bottom).  The offset is clamped to the current content height."""
        w = self.term.width
        log_rows = max(self.term.height - 3 - 1 - 4, 2)
        total = len(self.log._all_rows(w))
        max_off = max(0, total - log_rows)
        self.log.offset = max(0, min(self.log.offset + delta, max_off))
        self._ui_refresh()

    # ── tui3 wave 2: in-log search + visual copy ─────────────────────────
    @staticmethod
    def _row_text(line) -> str:
        return "".join(s.text for s in line.segments)

    def _logsearch_open(self) -> None:
        self._logsearch = {"query": "", "input": True}
        self._search_matches = []
        self._search_idx = 0
        self._ui_refresh()

    def _logsearch_close(self) -> None:
        self._logsearch = None
        self._search_matches = []
        self._search_idx = 0
        self._ui_refresh()

    def _logsearch_char(self, key) -> None:
        if self._logsearch is None or not self._logsearch["input"]:
            return
        if key.name == "backspace":
            self._logsearch["query"] = self._logsearch["query"][:-1]
        elif key.name == "enter":
            self._logsearch_exec()
            return
        elif key.name in ("escape", "esc"):
            self._logsearch_close()
            return
        elif key.name == "space":
            self._logsearch["query"] += " "
        elif key.name == "paste" and getattr(key, "payload", None):
            self._logsearch["query"] += key.payload.replace("\n", " ")
        else:
            txt = _key_text(key)
            if len(txt) == 1:
                self._logsearch["query"] += txt
        self._ui_refresh()

    def _logsearch_exec(self) -> None:
        q = (self._logsearch["query"].strip()
             if self._logsearch else "")
        if not q:
            self._logsearch_close()
            return
        w = self.term.width
        rows = self.log._all_rows(w)
        ql = q.lower()
        idxs = [i for i, r in enumerate(rows)
                if ql in self._row_text(r).lower()]
        # Group consecutive matching lines (same message) into one match.
        groups = []  # [first_idx, last_idx, snippet]
        for i in idxs:
            if groups and i - groups[-1][1] <= 3:
                groups[-1][1] = i
            else:
                groups.append([i, i, self._row_text(rows[i]).strip()[:70]])
        self._search_matches = [(g[0], g[2]) for g in groups]
        self._search_idx = 0
        self._logsearch["input"] = False
        if self._search_matches:
            self._logsearch_jump(0)
        else:
            self._ui_refresh()

    def _logsearch_jump(self, idx: int) -> None:
        if not self._search_matches:
            return
        n = len(self._search_matches)
        idx = idx % n
        self._search_idx = idx
        li = self._search_matches[idx][0]
        w = self.term.width
        total = len(self.log._all_rows(w))
        log_rows = max(self.term.height - 3 - 1 - 4, 2)
        max_off = max(0, total - log_rows)
        target = total - 1 - li  # bring the match to the viewport bottom
        self.log.offset = max(0, min(target, max_off))
        self._ui_refresh()

    def _logsearch_next(self, forward: bool = True) -> None:
        if not self._search_matches:
            self._logsearch_open()  # nothing found yet -> start a query
            return
        self._logsearch_jump(self._search_idx + (1 if forward else -1))

    def _visual_copy(self) -> None:
        """Copy the currently visible log viewport to the clipboard."""
        w = self.term.width
        rows = self.log._all_rows(w)
        total = len(rows)
        vis = getattr(self.log, "rows", 0) or max(self.term.height - 8, 2)
        off = self.log.offset
        lo = max(0, total - vis - off)
        hi = max(lo, min(total, total - off))
        text = "\n".join(self._row_text(r) for r in rows[lo:hi])
        if not text.strip():
            text = "(empty viewport)"
        self._copy_to_clipboard(text)
        self._notify.push(
            "copied", f"{len(text.splitlines())} visible line(s)",
            "ok", write=self._term_write)
        self._ui_refresh()

    def _mouse_select(self, key) -> None:
        """Click / drag-to-copy over the log region (tui3 wave 3b).

        A press records the anchor frame row; a release copies the line
        range [anchor .. release] (a single click copies one line).  The
        text is read from the last rendered frame, so no log-index math is
        needed and it always matches what the user saw.
        """
        row = self._mouse_row(key)
        in_region = row is not None and 2 <= row <= self._last_log_end
        if key.name == "mouse-press":
            if in_region:
                self._sel_anchor = row
            else:
                self._sel_anchor = None
            self._ui_refresh()
            return
        # mouse-release
        if self._sel_anchor is None or not in_region:
            self._sel_anchor = None
            return
        lo, hi = sorted((self._sel_anchor, row))
        self._sel_anchor = None
        if hi < 1 or lo > len(self._last_frame_rows):
            return
        lines = self._last_frame_rows[lo - 1:hi]
        text = "\n".join(self._row_text(r) for r in lines).strip()
        if not text:
            return
        self._copy_to_clipboard(text)
        n = len(lines)
        self._notify.push(
            "copied", "line" if n == 1 else f"{n} lines",
            "ok", write=self._term_write)
        self._ui_refresh()

    @staticmethod
    def _mouse_row(key) -> int:
        """Parse the 1-based row out of a mouse key payload (``col,row``)."""
        payload = getattr(key, "payload", None)
        if not payload or "," not in payload:
            return None
        try:
            return int(payload.split(",", 1)[1])
        except ValueError:
            return None

    def _status_right(self) -> str:
        parts: list = []
        if self._ctx_budget > 0:
            try:
                from nbchat.tui.status import _ctx_bar, _humanise
                bar, pct = _ctx_bar(self._ctx_used, self._ctx_budget)
                parts.append(
                    f"ctx {bar} {pct} "
                    f"({_humanise(self._ctx_used)}/{_humanise(self._ctx_budget)})")
            except Exception:
                pass
        if self._tok_times:
            parts.append(f"{len(self._tok_times):.1f} tok/s")
        g = self._goal
        if g is not None:
            tag = ("goal" if not g.get("stopped") else "goal·done")
            parts.append(f"{tag} {g['done']}/{g['budget']}")
        pill = self._todo_pill()
        if pill:
            parts.append(pill)
        qp = self._queue_pill()
        if qp:
            parts.append(qp)
        return "  ".join(parts)

    def _build_frame(self) -> Frame:
        self._tick_heartbeat()
        w, h = self.term.width, self.term.height
        header_h = 1
        # Bottom area: the 3-row message editor, the tool-approval prompt,
        # or the session-picker modal while it is open (its rendered
        # height, capped to the terminal).
        if self._approval is not None:
            picker_rows = []
            fc_rows = []
            bottom_h = 5
        elif self._picker is not None:
            picker_rows = self._picker.render(w)
            fc_rows = []
            bottom_h = min(len(picker_rows), max(5, h - 6))
        else:
            picker_rows = []
            fc_rows = self._filecomp_render(w)
            bottom_h = 3 + len(fc_rows)
        # In-TUI toasts (transient; rendered just above the input box).
        self._notify.prune()
        toast_lines = self._notify.render_one(w)
        toast_h = len(toast_lines)
        # Fixed chrome: 1 header + toast_h + 1 rule + bottom_h + 1 modebar
        # + 1 status.
        region = max(h - bottom_h - header_h - 3 - toast_h, 2)
        log_rows = max(h - bottom_h - header_h - 5 - toast_h, 2)
        self.log.rows = log_rows

        body = self.log.render(w)
        combined = body + self._live_rows(w)
        if len(combined) > region:
            combined = combined[-region:]  # bottom-anchored: newest visible
        rows: List[Line] = [_header(w, self.session_id, self.model_name)]
        rows.extend(combined)
        rows.extend(blank(w) for _ in range(max(region - len(combined), 0)))
        rows.extend(toast_lines)
        rows.append(_rule(w))

        busy = self.busy
        detail = self._status_detail or self._status_state
        self._loader.message = detail
        self._loader.active = busy
        self._loader.step(int(time.monotonic()))
        status_left = (f"{self._loader.glyph()} {detail}" if busy
                       else (self._status_detail or "ready"))
        status_right = self._status_right()

        if self._approval is not None:
            rows.extend(self._approval_lines(w))
        elif self._picker is not None:
            shown = picker_rows
            if len(shown) > bottom_h:
                shown = shown[:bottom_h - 1] + [shown[-1]]
            rows.extend(shown)
        else:
            rows.extend(fc_rows)  # @-completion box (above the editor)
            rows.extend(Box(
                title="message", lines=self._editor_lines(),
                clip=True,
            ).render(w))
        scroll_tag = f"\u2191{self.log.offset} " if self.log.offset else ""
        rows.append(self._mode_bar(w))
        rows.append(StatusLine(
            left=f"turns: {self._turns}   {scroll_tag}{status_left}",
            right=status_right,
        ).render(w)[0])
        # Remember the rendered rows + where the log/live region ends so a
        # mouse click (1-based row) can be mapped to a copyable line.
        self._last_frame_rows = rows[:h]
        self._last_log_end = 1 + region
        return Frame(lines=rows[:h], width=w, height=h)

    def _approval_lines(self, w: int):
        """Render the pending tool-approval prompt (3 inner rows)."""
        a = self._approval
        tool = (a or {}).get("tool", "?")
        args = (a or {}).get("args", "")
        inner = w - 4
        lines: List[Line] = []
        lines.extend(_to_lines("\u25b8 " + tool, inner))
        lines.extend(_to_lines(args if args else "(no args)", inner))
        lines.extend(_to_lines("y approve · n deny · a always", inner))
        lines = lines[:3]  # keep the prompt to 3 inner rows
        return Box(title="approve tool", lines=lines, clip=True).render(w)

    def _approval_always(self) -> None:
        """Approve now AND stop prompting for this tool this session."""
        pend = self._approval
        if pend is None:
            return
        self._risky_tools.discard(pend["tool"])
        self._approval_answer(True)

    def _term_write(self, data: str) -> None:
        """Best-effort write to the terminal (used for the BEL)."""
        try:
            self.term.stdout.write(data)
            self.term.stdout.flush()
        except Exception:
            pass

    def _mode_bar(self, w: int) -> Line:
        """The contextual mode bar (tui3): active mode + its key hints,
        generated from the single ``KEYMAP`` source of truth."""
        mode = "browse" if self._browse else "normal"
        hints = " · ".join(f"{k} {d}" for k, d in KEYMAP[mode][: _MODEBAR_HINTS])
        label = ("plan" if self._plan_mode
                 else ("browse" if self._browse else "normal"))
        text = f" mode: {label}   {hints}"
        # In-log search overrides the hints with the live query / match pos.
        if self._logsearch is not None:
            q = self._logsearch["query"]
            if self._logsearch["input"]:
                text = f" search: {q}▏  (enter run · esc cancel)"
            elif self._search_matches:
                i = self._search_idx + 1
                text = (f" match {i}/{len(self._search_matches)}: "
                        f"{q}  (n/N cycle · esc clear)")
            else:
                text = f" search: '{q}'  no matches (esc clear)"
        if len(text) > w:
            text = text[: w - 1] + "…"
        return Line([
            Segment("│", DARK.border),
            Segment(text, DARK.status if not self._browse else DARK.warn),
            Segment(" " * max(w - 2 - len(text), 0), DARK.status),
            Segment("│", DARK.border),
        ])

    def _editor_lines(self):
        return self.editor.render(width=self.term.width - 4) \
            if hasattr(self.editor, "render") else _to_lines(
                " ".join(self.editor.lines).strip()
                or self.editor.placeholder, self.term.width - 4)

    # ── entry point ─────────────────────────────────────────────────────

    def _start_control(self):
        """Start the optional external control socket (tui3 wave 6).

        Returns the running :class:`ControlServer`, or ``None`` when
        disabled (``NBCHAT_NO_CTL=1``) or when binding fails.  Read-only
        commands are answered on the socket thread; mutating ones are
        enqueued onto the UI thread via the ``"call"`` event.
        """
        from . import ctl
        if not ctl.enabled():
            return None

        def _status() -> dict:
            from . import theme as _theme
            return {
                "busy": bool(self.busy),
                "session": self.session_id,
                "model": self.model_name,
                "turns": int(self._turns),
                "theme": _theme.current().name,
            }

        def _sessions() -> list:
            from nbchat.core import db
            try:
                return [
                    {"session": r.get("session_id"), "title": r.get("title")}
                    for r in db.list_sessions_with_title("tui:")
                ]
            except Exception:
                return []

        def _result() -> dict:
            # Read-only: the most recent assistant reply for this session
            # (closes the send -> status -> result background loop).
            from nbchat.core import db
            sid = self.session_id
            try:
                for _s, role, content in reversed(db.get_history(sid)):
                    if role == "assistant" and content:
                        return {"session": sid, "text": content}
                return {"session": sid, "text": ""}
            except Exception:
                return {"session": sid, "text": ""}

        def _theme(name: str) -> None:
            out = self._cmd_theme(name)
            if out:
                self._note(out)
            self._ui_refresh()

        def _send(text: str) -> None:
            self._submit(text)

        def _quit() -> None:
            self.events.put("quit")

        server = ctl.ControlServer(
            ctl.socket_path(),
            dispatch=lambda fn: self.events.put("call", fn),
            status_fn=_status,
            sessions_fn=_sessions,
            result_fn=_result,
            theme_fn=_theme,
            send_fn=_send,
            quit_fn=_quit,
        )
        return server if server.start() else None

    # ── Supervisor watchdog (v1 --supervisor parity) ────────────────────

    def _start_supervisor(self) -> None:
        """Create + start the always-on supervisor bound to this agent.

        Best-effort: a failure to start (e.g. missing config) is noted, not
        fatal.  The watchdog reviews the assistant's in-flight work on a
        timer and pushes corrective instructions onto the interjection queue,
        which the conversation loop drains at the top of each tool-turn.
        """
        if not self._supervisor_enabled:
            return
        try:
            from nbchat.core.supervisor import create_supervisor
            self._supervisor = create_supervisor(self)
            self._supervisor.start()
            self._note(
                f"supervisor: ACTIVE (review every "
                f"{self._supervisor._interval}s, cooldown "
                f"{self._supervisor._cooldown}s)"
            )
        except Exception as exc:
            self._supervisor = None
            self._note(f"supervisor: failed to start ({type(exc).__name__}: {exc})")

    def _stop_supervisor(self) -> None:
        if self._supervisor is not None:
            try:
                self._supervisor.stop()
            except Exception:
                pass
            self._supervisor = None

    # ── Alfred voice bridge (v1 --voice parity) ─────────────────────────

    def _start_voice(self) -> None:
        """Create + start the Alfred voice bridge (localhost, SSH-tunnelled).

        Best-effort: a failed bind (port in use, no fastapi) is noted, not
        fatal.  A daemon thread blocks on the bridge's inbound queue and, for
        each transcript, dispatches a tui2-native submit onto the UI thread
        (via the "call" event) — so the voice path reuses the exact same
        turn-launch / interjection machinery as keyboard input, and the raw
        screen is never touched from a background thread.
        """
        if not self._voice_enabled:
            return
        try:
            from nbchat.voice.events import ALFRED_VOICE_PROMPT, VoiceEventBus
            from nbchat.voice.server import VoiceBridge
            import nbchat.core.config as _cfg
        except Exception as exc:
            self._note(f"voice: unavailable ({type(exc).__name__}: {exc})")
            return
        try:
            bus = VoiceEventBus()
            self._voice_bus = bus
            self.system_prompt += ALFRED_VOICE_PROMPT
            port = _cfg.VOICE_PORT
            bridge = VoiceBridge(bus, port=port)
            if not bridge.start():
                self._note(f"voice: bridge FAILED to start on port {port}")
                self._voice_bus = None
                return
            self._voice_bridge = bridge
            threading.Thread(target=self._voice_inbound_loop,
                             daemon=True, name="nbchat-voice-in").start()
            self._note(
                f"voice: ACTIVE on localhost:{port}  (ssh -L "
                f"{port}:127.0.0.1:{port} user@server)"
            )
        except Exception as exc:
            self._note(f"voice: failed to start ({type(exc).__name__}: {exc})")
            self._voice_bridge = None

    def _stop_voice(self) -> None:
        if self._voice_bridge is not None:
            try:
                self._voice_bridge.stop()
            except Exception:
                pass
            self._voice_bridge = None

    def _voice_inbound_loop(self) -> None:
        """Daemon: block on the bridge's inbound queue and auto-submit.

        Runs OFF the UI thread.  When a transcript arrives it fires the
        "received" ack (back to the laptop client) and hands the text to
        ``_voice_submit`` on the UI thread.
        """
        bridge = self._voice_bridge
        if bridge is None:
            return
        while True:
            transcript = bridge.get_inbound(timeout=1.0)
            if transcript is None:
                if self._voice_bridge is None:  # stopped
                    return
                continue
            try:
                self._voice_fire("received")
            except Exception:
                pass
            self.events.put("call",
                            lambda t=transcript: self._voice_submit(t))

    def _voice_submit(self, text: str) -> None:
        """UI-thread submit of a voice transcript (mirrors a typed line)."""
        self._history.append(text)
        if len(self._history) > 200:
            self._history = self._history[-200:]
        self._note(f"♪ [voice] {text}")
        self._start_turn(text)

    def run(self) -> int:
        # Redirect stderr to a log file for the session: the conversation
        # loop\'s logging warnings (mid-stream retries, …) would otherwise
        # hit the lastResort handler and corrupt the raw-mode screen.
        saved_stderr = sys.stderr
        stderr_file = None
        try:
            log_dir = os.path.expanduser("~/.nbchat")
            os.makedirs(log_dir, exist_ok=True)
            stderr_file = open(os.path.join(log_dir, "tui2-stderr.log"),
                               "a", buffering=1)
            sys.stderr = stderr_file
        except Exception:
            stderr_file = None
        # Optional external control socket (tui3 wave 6): lets an external
        # process / nbchat-ctl drive this TUI.  Best-effort; never blocks.
        control = self._start_control()
        self._start_supervisor()
        self._start_voice()
        try:
            with self.term:
                self._apply_auto_theme()
                self._install_approval_gate()
                self._tui.start()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop_voice()
            self._stop_supervisor()
            if control is not None:
                try:
                    control.stop()
                except Exception:
                    pass
            self._save_cfg()
            self._remove_approval_gate()
            sys.stderr = saved_stderr
            if stderr_file is not None:
                try:
                    stderr_file.close()
                except Exception:
                    pass
            try:
                self.events.close()
            except Exception:
                pass
        return 0


def _fmt_ts(ts) -> str:
    """Format a chat_log timestamp (ISO string or epoch) for the picker."""
    if not ts:
        return ""
    try:
        import datetime
        if isinstance(ts, (int, float)):
            dt = datetime.datetime.fromtimestamp(ts)
        else:
            dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        now = datetime.datetime.now(dt.tzinfo) if dt.tzinfo else \
            datetime.datetime.now()
        delta = now - dt
        if delta.total_seconds() < 90:
            return "now"
        if delta.total_seconds() < 3600:
            return f"{int(delta.total_seconds() // 60)}m"
        if delta.total_seconds() < 86400:
            return f"{int(delta.total_seconds() // 3600)}h"
        return dt.strftime("%b %d")
    except Exception:
        return ""


def _key_text(key: Key) -> str:
    """The insertable text for a parsed key, if any.

    Special keys (``enter``, ``up``, ``ctrl+c``…) carry no printable text.
    For a plain character the parser puts it in :attr:`Key.name` with
    ``payload=None``; for multi-byte / unknown sequences the text lives in
    ``payload``.  The space bar is the one printable key the parser names
    explicitly (``"space"``) — it must still insert a space.
    """
    if key.payload:
        return key.payload
    if key.name == "space":
        return " "
    if len(key.name) == 1 and ord(key.name[0]) >= 0x20:
        return key.name
    return ""


def _header(width: int, session_id: str, model_name: str) -> Line:
    short = (session_id or "").rsplit(":", 1)[-1][:12]
    left = f" nbchat  ·  {model_name or '?'}  ·  session {short} "
    pad = max(width - len(left), 0)
    return Line([Segment(left, DARK.muted),
                 Segment(" " * pad, Style())])


def _rule(width: int) -> Line:
    """A thin horizontal rule line."""
    return Line([Segment("─" * width)])


def _arg_hint(args: str) -> str:
    """Compress a JSON args string into a short display hint."""
    try:
        import json
        data = json.loads(args or "{}")
        if isinstance(data, dict) and data:
            return ", ".join(
                f"{k}={str(v)[:24]}" for k, v in list(data.items())[:3])
    except Exception:
        pass
    return (args or "")[:40].replace("\n", " ")


def _result_lines(raw: str, limit: int) -> list:
    """Split a raw tool result into display lines, capped at *limit*."""
    if not raw or not raw.strip():
        return []
    lines = [ln.rstrip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        lines = [raw.strip()[:200]]
    if len(lines) > limit:
        extra = len(lines) - limit
        lines = lines[:limit] + [f"… {extra} more line(s)"]
    return lines


def _strip_markup(text: str) -> str:
    """Remove leaked markup from streamed text before it enters the log."""
    text = _TOOL_TEXT_RE.sub("", text or "")
    return text.strip()


def _to_lines(text: str, width: int):
    return Text(text or " ").render(width)


def run(argv: list | None = None) -> int:
    """``python -m nbchat.tui2`` entry point.

    ``--new`` forces a fresh session; ``--session <id|name>`` resumes a
    specific one; ``--demo`` is handled by :mod:`nbchat.tui2.__main__`.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="nbchat-tui2")
    parser.add_argument("--new", action="store_true",
                        help="force a new session")
    parser.add_argument("--session", metavar="ID",
                        help="resume a specific session id or name")
    parser.add_argument("--bg", action="store_true",
                        help="headless / background mode: stdin EOF does not "
                             "quit; the app stays alive and is driven through "
                             "the control socket (see nbchat-ctl)")
    parser.add_argument("--supervisor", action="store_true",
                        help="start the always-on supervisor watchdog that "
                             "reviews in-flight work and answers /sup queries")
    parser.add_argument("--voice", action="store_true",
                        help="start the Alfred voice bridge (localhost; reach "
                             "it via an SSH tunnel) and auto-submit transcripts")
    args = parser.parse_args(argv)
    # NBCHAT_BG=1 / NBCHAT_SUPERVISOR=1 / NBCHAT_VOICE=1 are env escape hatches.
    bg = args.bg or os.environ.get("NBCHAT_BG") == "1"
    supervisor = args.supervisor or os.environ.get("NBCHAT_SUPERVISOR") == "1"
    voice = args.voice or os.environ.get("NBCHAT_VOICE") == "1"

    term = RawTerminal(sys.stdin, sys.stdout)
    events = EventQueue()
    try:
        app = ChatApp(term, events, resume_last=not args.new,
                      session_id=args.session, bg=bg,
                      supervisor=supervisor, voice=voice)
    except ValueError as exc:
        # Only reachable on a real terminal before raw mode is entered;
        # print to the main screen and exit.
        print(str(exc), file=sys.stderr)
        return 1
    return app.run()


if __name__ == "__main__":
    raise SystemExit(run())
