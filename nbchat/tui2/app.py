"""The TUI v2 chat application (Phase 2).

:class:`ChatApp` is a :class:`nbchat.tui.agent.TerminalAgent` subclass whose
terminal-output hooks are re-routed from ``sys.stdout`` into the structured
render tree (:class:`~nbchat.tui2.chat.ChatLog`) per the §11.4.2 decision:

- the print-based status bar and ``_status_*`` hooks are overridden so they
  feed the tui2 status line instead of printing (no double-fire in raw mode);
- ``_run_turn``'s ``printer`` seam is used for the user turn;
- the existing REPL (``nbchat.tui``) is byte-for-byte unchanged — nothing is
  subclassed or patched, only a new entry point is added.

The agentic turn runs on a daemon worker thread (serialized by the
agent's own ``_send_lock``) so the UI event loop stays responsive while
streaming.  ``Enter`` submits (single-line semantics); ``Esc`` interrupts
an in-flight turn; ``Ctrl+D`` on an empty editor quits.
"""

from __future__ import annotations

import re
import sys
import threading
import time

from . import chat as chatc
from .components import Box, Container, Loader, StatusLine, Text, blank
from .editor import LineEditor
from .frame import Frame, Line, Segment
from .theme import DARK
from .keys import Key
from .keys import KeyReader
from .raw import EventQueue, RawTerminal, TUIApp
from nbchat.tui.agent import TerminalAgent

# <tool_call> blocks leak through the stream when the model emits them as
# text instead of structured tool calls; keep the log clean.
_TOOL_TEXT_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL)


class ChatApp(TerminalAgent):
    """A full conversation app: agent + chat log + input line.

    Subclasses :class:`~nbchat.tui.agent.TerminalAgent` directly so the
    print-based output hooks (``_status_set``, ``_print_user``,
    ``_on_stream_*``, ``_on_tool_display``, ``_on_agent_message``) resolve
    to the overrides below instead of writing to ``sys.stdout``.
    """

    def __init__(self, term: RawTerminal, events: EventQueue) -> None:
        super().__init__(color=False)
        self.term = term
        self.events = events
        # TerminalAgent.__init__ mints self.session_id; the tui2 chrome
        # and any future /sessions wiring read it directly.

        self.editor = LineEditor(
            placeholder="Type a message…  (enter sends · esc interrupts · ctrl+d quits)",
            multiline=False,
        )
        self.log = chatc.ChatLog()

        # Status fed by the overridden agent hooks.
        self._status_state = "ready"
        self._status_detail = ""
        self._context = ""
        self._turns = 0
        self._queued = 0

        # Live streaming turn (the only message re-rendered per frame).
        self._stream_blocks: list[chatc.ChatBlock] = []
        self._stream_text = ""

        # Turn worker bookkeeping.
        self._turn_thread: threading.Thread | None = None
        self._turn_dirty = True

        self._tui = TUIApp(term, events)
        self._tui.set_frame_provider(self._build_frame)
        self._tui.set_key_reader(KeyReader())
        self._tui.on_input = self._on_input

        # Disable the print-based status bar before any hook fires.
        try:
            from nbchat.tui import status as st
            if st._enabled():
                st._enabled = lambda: False
        except Exception:
            pass

    # ── agent hook overrides (print → render tree) ──────────────────────

    def _status(self):  # override: no print status bar in tui2
        return None

    def _status_set(self, state: str, detail: str = "") -> None:
        self._status_state = state
        self._status_detail = detail
        self._ui_refresh()

    def _print_user(self, text: str) -> None:
        self.log.add(chatc.ChatMessage(role="user", text=text))
        self._ui_refresh()

    def _on_stream_reasoning(self, reasoning: str) -> None:
        delta = reasoning[len(getattr(self, "_reasoning_printed", "")):]
        self._reasoning_printed = reasoning
        if delta and not self._stream_text:
            self._stream_blocks.insert(0, chatc.ChatBlock(
                kind="thinking", title="thinking", text=reasoning))
            self._ui_refresh()

    def _on_stream_token(self, content: str) -> None:
        delta = content[len(getattr(self, "_content_printed", "")):]
        self._content_printed = content
        self._content_started = True
        if delta:
            self._stream_text += delta
            self._ui_refresh()

    def _on_stream_complete(self, content: str, tool_calls=None) -> None:
        if content:
            self._last_response = content
        held = self._voice_parser.flush_unclosed() if hasattr(
            self, "_voice_parser") else ""
        if held:
            self._stream_text += held
        self._reasoning_printed = ""
        self._content_printed = ""
        self._content_started = False
        if hasattr(self, "_voice_parser"):
            from nbchat.tui.agent import VoiceTagParser
            self._voice_parser = VoiceTagParser()

    def _on_tool_display(self, raw_result: str, tool_name: str,
                         tool_args: str) -> None:
        hint = _arg_hint(tool_args)
        preview = raw_result[:300].replace("\n", " ⏎ ")
        self._stream_blocks.append(chatc.ChatBlock(
            kind="tool", name=tool_name, title=f"{tool_name}({hint})",
            status="done", body=[preview] if preview.strip() else [],
        ))
        self._ui_refresh()

    def _on_agent_message(self, text: str) -> None:
        self._status_set("error", text[:80])
        self._stream_blocks.append(chatc.ChatBlock(
            kind="tool", name="error", title="agent message",
            status="error", body=text.splitlines()[:10],
        ))
        if not getattr(self, "_last_response", ""):
            self._last_response = text
        self._ui_refresh()

    # ── turn worker ─────────────────────────────────────────────────────

    def _start_turn(self, text: str) -> None:
        if self._turn_thread is not None and self._turn_thread.is_alive():
            self._queued += 1
            self._ui_refresh()
            return
        self._turn_thread = threading.Thread(
            target=self._turn_worker, args=(text,), daemon=True)
        self._turn_thread.start()

    def _turn_worker(self, text: str) -> None:
        try:
            self.send(text)
            queued = getattr(self, "_queued", 0)
            while queued:
                self._queued = getattr(self, "_queued", 0)
                break  # queued messages are handled by the REPL; tui2 v1
                # simply reports them.  (Interjection queueing lands with
                # the / command pass.)
        except KeyboardInterrupt:
            pass
        except Exception as exc:  # pragma: no cover - surface in the UI
            self._stream_blocks.append(chatc.ChatBlock(
                kind="tool", name="error", title=str(type(exc).__name__),
                status="error", body=[str(exc)],
            ))
        finally:
            self._finalize_turn()

    def _finalize_turn(self) -> None:
        msg = chatc.ChatMessage(
            role="assistant",
            text=_strip_markup(self._stream_text),
            blocks=self._stream_blocks or None,
        )
        if msg.text or msg.blocks:
            self.log.add(msg)
        self._stream_blocks = []
        self._stream_text = ""
        self._turns += 1
        self._status_set("ready", "")
        self._turn_dirty = True
        self._ui_refresh()

    def _interrupt(self) -> None:
        if self.busy:
            self.interrupt()
            self._status_set("interrupted", "")
        else:
            self.editor.clear()
        self._ui_refresh()

    # ── key handling ────────────────────────────────────────────────────

    def _on_input(self, key: Key) -> None:
        if key.name == "ctrl+d" and not self.editor.text():
            self._tui.stop()
            return
        if key.name in ("escape", "esc"):
            self._interrupt()
            return
        if key.name == "paste" and key.payload:
            self.editor.handle("paste", key.payload)
            return
        self.editor.handle(key.name, _key_text(key))
        if self.editor.submitted:
            self.editor.submitted = False
            text = self.editor.text().strip()
            self.editor.clear()
            if text:
                self._start_turn(text)
        self._ui_refresh()

    # ── UI ──────────────────────────────────────────────────────────────

    def _ui_refresh(self) -> None:
        self._turn_dirty = True
        try:
            self.events.put("render")
        except Exception:
            pass

    def _build_frame(self) -> Frame:
        w, h = self.term.width, self.term.height
        # The message Box renders 3 rows (top border + input line +
        # bottom border).  Account for all of them or the status line
        # below gets clipped off the frame.
        editor_h = 3
        # chat log gets everything but the rule, editor box and status line
        log_rows = max(h - editor_h - 4, 2)
        self.log.rows = log_rows
        body = self.log.render(w)

        busy = self.busy
        detail = self._status_detail or self._status_state
        loader = Loader(detail if busy else "ready",
                        active=busy).step(int(time.time()))
        status_left = detail or "idle"
        status_right = self._context
        if self._queued:
            status_right = f"queued: {self._queued}" + (
                f"  |  {status_right}" if status_right else "")
        rows = Container(list(body)).render(w)[:h - editor_h - 2]
        # Pad the body region to a fixed size so the layout is stable.
        pad = h - editor_h - 2 - len(rows)
        rows = rows + [blank(w) for _ in range(max(pad, 0))]
        rows.append(_rule(w))
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
        try:
            with self.term:
                self._tui.start()
        except KeyboardInterrupt:
            pass
        finally:
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
    ``payload``.  This resolves either case to the string the editor should
    insert (empty when the key has none).
    """
    if key.payload:
        return key.payload
    if len(key.name) == 1 and ord(key.name[0]) >= 0x20:
        return key.name
    return ""


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


def _strip_markup(text: str) -> str:
    """Remove leaked markup from streamed text before it enters the log."""
    text = _TOOL_TEXT_RE.sub("", text or "")
    return text.strip()


def _to_lines(text: str, width: int):
    return Text(text or " ").render(width)


def run() -> int:
    """``python -m nbchat.tui2.app`` entry point."""
    term = RawTerminal(sys.stdin, sys.stdout)
    events = EventQueue()
    app = ChatApp(term, events)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(run())
