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
import os
import re
import sys
import threading
import time
from typing import List

from . import chat as chatc
from .components import Box, Container, Loader, StatusLine, Text, _SPINNER_FRAMES, blank
from .editor import LineEditor
from .frame import Frame, Line, Segment, Style
from .theme import DARK
from .keys import Key
from .keys import KeyReader
from .raw import EventQueue, RawTerminal, TUIApp
from .notify import NotifyStack
from nbchat.tui.agent import TerminalAgent

# <tool_call> blocks leak through the stream when the model emits them as
# text instead of structured tool calls; keep the log clean.
_TOOL_TEXT_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL)

# Cap on visible tool-result lines inside a panel (v1 shows ~the same).
_TOOL_RESULT_LINES = 8

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
        ("pgup/pgdn", "scroll the log up / down"),
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
                 session_id: str | None = None) -> None:
        super().__init__(color=False)
        self.term = term
        self.events = events

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
        self._tok_times: list = []
        self._turns = 0
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

        self._goal = None
        self._goal_budget = 20  # default auto-continue turn budget
        # Phrases the model can emit to mark the goal achieved.
        self._goal_done_markers = ("goal complete", "goal: complete",
                                   "goal achieved", "[goal done]",
                                   "goal accomplished")

        # Turn worker bookkeeping.
        self._turn_thread: threading.Thread | None = None

        self._tui = TUIApp(term, events)
        self._tui.set_frame_provider(self._build_frame)
        self._tui.set_key_reader(KeyReader())
        self._tui.on_input = self._on_input
        # ~1 Hz heartbeat so the busy spinner advances between events.
        self._tui.clock_interval = 1.0

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
        self._ui_refresh()

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
            # turn).  The model can mark the goal achieved with a completion
            # phrase; otherwise chain the next turn until the budget runs out.
            if self._goal_declared_done(msg.text):
                self._goal["stopped"] = True
                self._note("goal: model reported it complete — stopping "
                           "auto-continue")
            else:
                nxt = self._goal_next_prompt()
                if nxt is not None:
                    self._turn_thread = None
                    if self._tui._running:
                        self._start_turn(nxt)
                        chained = True
        # Turn-complete toast — only when the turn actually ended (no
        # redirect / goal continue).  Quiet by default: the focused chat's
        # own turn fires no BEL/sound (herdr suppresses the active pane);
        # the card is a subtle "done" marker.
        if not chained:
            self._notify.push("turn complete", "", "ok",
                              write=self._term_write)
            self._ui_refresh()

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
        self.editor.handle(key.name, _key_text(key))
        if self.editor.submitted:
            self.editor.submitted = False
            text = self.editor.text().strip()
            self.editor.clear()
            if text:
                self._submit(text)
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
                    "/goal", "/notify")

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
            "  /compact    force a context summarisation now",
            "  /copy       copy last reply to the clipboard",
            "  /btw <q>    side question, kept out of this session",
            "  /approve    tool-approval gate (on/off/add/rm/list)",
            "  /goal <x>   auto-continue until done or budget (stop)",
            "  /notify     toasts / BEL / sound (test)",
            "  /lessons    /memory  /refine   continual-harness",
            "  !<cmd>      run a shell command ( !! stores output )",
            "  Ctrl+T      show / hide thinking blocks",
            "  Ctrl+L      session picker (bare /load = picker)",
            "  Ctrl+P      command palette    Ctrl+R reverse search",
            "  Ctrl+O      browse mode (j/k scroll the log)",
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
        return "\n".join(lines)

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
            return "tool approval ON — risky tools will prompt"
        if sub in ("off", "disable"):
            self._approval_enabled = False
            return "tool approval OFF — tools run without prompting"
        if sub == "add" and rest:
            self._risky_tools.add(rest)
            return f"approval now required for: {sorted(self._risky_tools)}"
        if sub in ("rm", "remove") and rest:
            self._risky_tools.discard(rest)
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

    # ── session picker modal ────────────────────────────────────────────

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
        self._picker_sessions = []
        for r in rows:
            sid = r["session_id"]
            title = (r.get("title") or "").strip()
            short = sid.rsplit(":", 1)[-1][:10]
            label = (title or short)
            tail = f" · {counts.get(sid, 0)} msg"
            tail += f" · {_fmt_ts(r.get('last_ts'))}"
            if sid == self.session_id:
                tail += "  (current)"
            self._picker_sessions.append((sid, label + tail))
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
        return "  ".join(parts)

    def _build_frame(self) -> Frame:
        w, h = self.term.width, self.term.height
        header_h = 1
        # Bottom area: the 3-row message editor, the tool-approval prompt,
        # or the session-picker modal while it is open (its rendered
        # height, capped to the terminal).
        if self._approval is not None:
            picker_rows = []
            bottom_h = 5
        elif self._picker is not None:
            picker_rows = self._picker.render(w)
            bottom_h = min(len(picker_rows), max(5, h - 6))
        else:
            picker_rows = []
            bottom_h = 3
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
        label = ("browse" if self._browse else "normal")
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
        try:
            with self.term:
                self._install_approval_gate()
                self._tui.start()
        except KeyboardInterrupt:
            pass
        finally:
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
    args = parser.parse_args(argv)

    term = RawTerminal(sys.stdin, sys.stdout)
    events = EventQueue()
    try:
        app = ChatApp(term, events, resume_last=not args.new,
                      session_id=args.session)
    except ValueError as exc:
        # Only reachable on a real terminal before raw mode is entered;
        # print to the main screen and exit.
        print(str(exc), file=sys.stderr)
        return 1
    return app.run()


if __name__ == "__main__":
    raise SystemExit(run())
