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
from nbchat.tui.agent import TerminalAgent

# <tool_call> blocks leak through the stream when the model emits them as
# text instead of structured tool calls; keep the log clean.
_TOOL_TEXT_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL)

# Cap on visible tool-result lines inside a panel (v1 shows ~the same).
_TOOL_RESULT_LINES = 8


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

    def _finalize_turn(self) -> None:
        self._close_thinking()
        msg = chatc.ChatMessage(
            role="assistant",
            text=_strip_markup(self._stream_text),
            blocks=self._stream_blocks or None,
        )
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
        if redirect:
            self._turn_thread = None
            if self._tui._running:
                self._start_turn(redirect)

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
        if text.startswith("/"):
            self._run_command(text)
            return
        self._start_turn(text)

    def _run_command(self, line: str) -> None:
        """Route a slash command through the shared v1 command handler.

        ``handle_command`` prints its output; capturing ``sys.stdout`` keeps
        the raw-mode screen intact and renders the output as a dim "system"
        note in the log instead.
        """
        from nbchat.tui.app import handle_command

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

        if self.session_id != prev_sid:
            self._session_changed()
        if out:
            self.log.add(chatc.ChatMessage(role="system", text=out))
        self._ui_refresh()
        if should_quit:
            self._tui.stop()

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
                rows.extend(chatc.ThinkingBlock(
                    b.text, collapsed=False, title=b.title or "thinking",
                ).render(w))
        if self._stream_text:
            rows.extend(chatc.Message(
                "assistant", _strip_markup(self._stream_text)).render(w))
        return rows

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
        return "  ".join(parts)

    def _build_frame(self) -> Frame:
        w, h = self.term.width, self.term.height
        # Fixed chrome: 1 header + 1 rule + 3 message-box rows + 1 status.
        editor_h = 3
        header_h = 1
        region = max(h - editor_h - header_h - 2, 2)
        log_rows = max(h - editor_h - header_h - 4, 2)
        self.log.rows = log_rows

        body = self.log.render(w)
        combined = body + self._live_rows(w)
        if len(combined) > region:
            combined = combined[-region:]  # bottom-anchored: newest visible
        rows: List[Line] = [_header(w, self.session_id, self.model_name)]
        rows.extend(combined)
        rows.extend(blank(w) for _ in range(max(region - len(combined), 0)))
        rows.append(_rule(w))

        busy = self.busy
        detail = self._status_detail or self._status_state
        self._loader.message = detail
        self._loader.active = busy
        self._loader.step(int(time.monotonic()))
        status_left = (f"{self._loader.glyph()} {detail}" if busy
                       else (self._status_detail or "ready"))
        status_right = self._status_right()

        rows.extend(Box(
            title="message", lines=self._editor_lines(),
            clip=True,
        ).render(w))
        rows.append(StatusLine(
            left=f"turns: {self._turns}   {status_left}",
            right=status_right,
        ).render(w)[0])
        return Frame(lines=rows[:h], width=w, height=h)

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
                self._tui.start()
        except KeyboardInterrupt:
            pass
        finally:
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
