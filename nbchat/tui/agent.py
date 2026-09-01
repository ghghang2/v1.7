"""TerminalAgent — headless agent with a plain-text terminal frontend.

This is the terminal counterpart of :class:`nbchat.channels.whatsapp_agent.
WhatsAppAgent`: it mixes in the *full* agent stack — ``ContextMixin`` +
``ConversationMixin`` (L1/L2 memory, token-budget windowing, hard-trim,
compression, monitoring and the agentic tool-calling loop with streaming) —
but replaces the ipywidgets output hooks with plain ``stdout`` writes.

No Jupyter, no ipywidgets, no browser.  Runs in a basic terminal.
"""
from __future__ import annotations

import json
import sys
import threading
import uuid
from typing import List, Tuple

from nbchat.core import config
from nbchat.core import db
from nbchat.core import compressor as comp
from nbchat.core.supervisor import InterjectionQueue
from nbchat.ui.context_manager import ContextMixin, ImportanceTracker
from nbchat.ui.conversation import ConversationMixin
from nbchat.tui.colors import Palette
from nbchat.voice.events import ALFRED, VoiceTagParser
from nbchat.voice.events import VoiceEventBus  # noqa: F401  (type hint)

# Session ids are namespaced so terminal chats are easy to spot in the shared
# chat_history.db alongside Jupyter / WhatsApp sessions.
_SESSION_PREFIX = "tui:"

# Pseudo session used to persist small TUI state (e.g. "last used session")
# without polluting the real session list (no chat rows are ever written here).
_STATE_SESSION = ":tui:state"


def short_arg(value) -> str:
    """One-line, length-bounded rendering of a single tool argument."""
    if isinstance(value, str):
        text = value.replace("\n", " ")
    else:
        text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


class TerminalAgent(ContextMixin, ConversationMixin):
    """Interactive agent whose conversation loop renders to the terminal."""

    MAX_TOOL_TURNS = config.MAX_TOOL_TURNS

    def __init__(self, *, color: bool = True) -> None:
        db.init_db()
        self.palette = Palette(color)
        self.system_prompt = config.DEFAULT_SYSTEM_PROMPT
        self.model_name = config.MODEL_NAME

        self.session_id = self._new_session_id()
        self.history: List[Tuple[str, str, str, str, str, int]] = []
        self.task_log: List[str] = []
        self._turn_summary_cache: dict = {}
        self._summary_futures: dict = {}
        self._importance_tracker = ImportanceTracker(
            persist_fraction=config.PERSIST_FRACTION
        )
        self._stop_event = threading.Event()
        self._history_lock = threading.Lock()
        self._tool_running = False
        # True while a conversation turn (terminal or injected email) is
        # running the agentic loop.  The supervisor watchdog uses this to
        # decide whether the assistant is mid-turn.
        self._turn_active = False
        # Supervisor interjection queue — the supervisor pushes corrective
        # instructions here; the conversation loop drains them at safe points
        # (top of each tool-turn) and injects them as user messages.
        self._interjection_queue = InterjectionQueue()
        # Serializes conversation turns so a terminal send and an injected
        # email turn never run the LLM loop concurrently (shared history).
        self._send_lock = threading.Lock()

        # Streaming / capture state (reset between LLM calls).
        self._reasoning_printed = ""
        self._content_printed = ""
        self._content_started = False
        self._last_response: str = ""

        # Voice channel (Alfred).  The bus is attached by the TUI when
        # --voice is active; when None, all voice hooks are no-ops.
        self._voice_bus: VoiceEventBus | None = None
        self._voice_parser = VoiceTagParser()
        # Set when the conversation loop surfaces a terminal error
        # (loop crash / max tool turns); used to pick the honest
        # complete vs failed voice ack.
        self._turn_failed = False

        comp.init_session(self.session_id)

    # ── Session management ─────────────────────────────────────────────────

    @staticmethod
    def _new_session_id() -> str:
        return _SESSION_PREFIX + uuid.uuid4().hex[:12]

    def _switch_session(self, session_id: str) -> None:
        """Load history for *session_id* into this agent instance."""
        if session_id == self.session_id:
            return
        self.session_id = session_id
        self.history = list(db.load_history(session_id))
        self.task_log = db.load_task_log(session_id)
        self._turn_summary_cache = db.load_turn_summaries(session_id)
        comp.init_session(session_id)

    def new_session(self) -> str:
        """Start a fresh session; returns its id."""
        self._flush_monitor()
        sid = self._new_session_id()
        self.session_id = sid
        self._reset_state()
        comp.init_session(sid)
        return sid

    def list_sessions(self) -> List[str]:
        return [s for s in db.get_session_ids() if s.startswith(_SESSION_PREFIX)]

    @staticmethod
    def last_session() -> str:
        return db._meta_get(_STATE_SESSION, "last_session")

    @staticmethod
    def remember_session(session_id: str) -> None:
        db._meta_set(_STATE_SESSION, "last_session", session_id)

    def _reset_state(self) -> None:
        with self._history_lock:
            self.history = []
        self.task_log = []
        self._turn_summary_cache = {}
        self._summary_futures = {}
        try:
            db.clear_core_memory(self.session_id)
            db.delete_episodic_for_session(self.session_id)
        except Exception:
            pass

    def _flush_monitor(self) -> None:
        try:
            from nbchat.core import monitoring as mon

            mon.flush_session_monitor(self.session_id, db)
        except Exception:
            pass

    # ── Conversation entry point ───────────────────────────────────────────

    def send(self, text: str) -> str:
        """Append a user message and run the (blocking) agentic turn.

        Returns the agent's final reply text.  Safe to call from the main
        thread: ``KeyboardInterrupt`` (Ctrl+C) propagates so the caller can
        interrupt streaming.
        """
        with self._send_lock:
            return self._run_turn(text, self._print_user)

    def send_async(self, text: str) -> threading.Thread:
        """Run a user turn on a daemon thread and return the thread handle.

        The turn runs the exact same blocking path as :meth:`send` (holding
        ``_send_lock`` the whole time), so it is fully serialized with the
        email bridge and any other turn.  The caller is expected to keep
        reading input while the returned thread is alive and to
        :meth:`interrupt` + join it when the user wants to redirect.
        """
        def _runner() -> None:
            with self._send_lock:
                self._voice_fire("started")
                self._turn_failed = False
                try:
                    self._run_turn(text, self._print_user)
                except Exception:
                    # The conversation loop already surfaces errors through
                    # _on_agent_message; never let a turn thread die silently.
                    import logging
                    logging.getLogger("nbchat.tui").exception(
                        "async turn failed")
                    self._turn_failed = True
                finally:
                    # Verified terminal state of the turn:
                    #   interrupted  — the user set the stop event
                    #   failed       — a terminal error was surfaced
                    #   complete     — everything else
                    if self._stop_event.is_set():
                        self._voice_fire("interrupted")
                    elif self._turn_failed:
                        self._voice_fire("failed")
                    else:
                        self._voice_fire("complete")

        thread = threading.Thread(
            target=_runner, name="nbchat-tui-turn", daemon=True
        )
        thread.start()
        return thread

    # -- Voice channel (Alfred) -------------------------------------------

    def _voice_fire(self, kind: str) -> None:
        """Fire a verified event-ack to the voice bus (no-op if off).

        Only called from verified state transitions (see
        :mod:`nbchat.voice.events`); the line is the deterministic
        Alfred template for *kind*.
        """
        bus = self._voice_bus
        if bus is None:
            return
        text = ALFRED.get(kind)
        if not text:
            return
        self._print_voice(text)
        bus.publish(kind, text)

    def _print_voice(self, text: str) -> None:
        p = self.palette
        sys.stdout.write(p.dim(f"  \u266a Alfred: {text}\n"))
        sys.stdout.flush()

    def send_from_email(self, sender: str, subject: str, body: str) -> str:
        """Inject an inbound email into the chat as a user turn.

        The email is composed into a clearly-labelled user message so the
        model treats it as a normal user interjection, streamed through the
        same agentic loop as a terminal message.  Returns the agent's reply
        text (the caller may choose to send it back by email).
        """
        text = (
            f"[Email message from {sender}]\n"
            f"Subject: {subject}\n\n"
            f"{body}"
        )
        with self._send_lock:
            return self._run_turn(text, lambda t: self._print_mail(sender, subject, t))

    def _run_turn(self, text: str, printer) -> str:
        """Core turn: append + persist + print + run the agentic loop."""
        self._last_response = ""
        self._stop_event.clear()

        self.history.append(("user", text, "", "", "", 0))
        db.log_message(self.session_id, "user", text)
        printer(text)

        self._turn_active = True
        try:
            self._process_conversation_turn()
        finally:
            self._turn_active = False
        return self._last_response

    @property
    def busy(self) -> bool:
        """True while a conversation turn is running the agentic loop."""
        return self._turn_active

    def interrupt(self) -> None:
        """Ask the in-flight turn to stop at the next safe point.

        Sets the shared stop event that the conversation loop and the
        streaming loop already honor (checked at the top of each tool-turn
        and between streamed chunks).  The turn thread then winds down and
        exits; the caller should join it before starting the next turn so
        history is consistent.  Safe to call from any thread; a no-op when
        no turn is running.
        """
        self._stop_event.set()

    def interject(self, text: str) -> None:
        """Queue a supervisor interjection for the next safe point.

        The text is placed on the interjection queue.  The conversation
        loop drains it at the top of the next tool-turn and injects it
        as a user message.  Safe to call from any thread.
        """
        self._interjection_queue.push(text)

    def drain_interjections(self) -> list[str]:
        """Drain all pending supervisor interjections.

        Called by the conversation loop at the top of each tool-turn.
        """
        return self._interjection_queue.drain()

    # ── Terminal output hooks (ConversationMixin interface) ────────────────

    def _print_user(self, text: str) -> None:
        p = self.palette
        sys.stdout.write(p.green("You: ") + "\n")
        for line in text.splitlines() or [""]:
            sys.stdout.write("  " + line + "\n")
        sys.stdout.write("\n")
        sys.stdout.flush()

    def _print_mail(self, sender: str, subject: str, text: str) -> None:
        p = self.palette
        sys.stdout.write(p.magenta(f"✉ Email {p.bold(sender)}")
                         + p.gray(f" — {subject}") + "\n")
        for line in text.splitlines():
            if line.startswith("[Email message from") or line.startswith("Subject:"):
                continue
            sys.stdout.write("  " + line + "\n")
        sys.stdout.write("\n")
        sys.stdout.flush()

    def _on_stream_reasoning(self, reasoning: str) -> None:
        p = self.palette
        delta = reasoning[len(self._reasoning_printed):]
        if delta:
            if not self._reasoning_printed:
                sys.stdout.write(p.dim("[thinking] "))
            sys.stdout.write(p.dim(delta))
            sys.stdout.flush()
        self._reasoning_printed = reasoning

    def _on_stream_token(self, content: str) -> None:
        p = self.palette
        if not self._content_started:
            if self._reasoning_printed:
                sys.stdout.write("\n")
            sys.stdout.write(p.cyan("» "))
            self._content_started = True
        delta = content[len(self._content_printed):]
        if delta:
            display, blocks = self._voice_parser.process(delta)
            if display:
                sys.stdout.write(display)
                sys.stdout.flush()
            for block in blocks:
                self._print_voice(block)
                if self._voice_bus is not None:
                    self._voice_bus.publish("tag", block)
        self._content_printed = content

    def _on_stream_complete(self, content: str, tool_calls: list | None) -> None:
        if self._content_started or self._reasoning_printed:
            sys.stdout.write("\n")
            sys.stdout.flush()
        if content:
            self._last_response = content
        # Reset streaming state for the next LLM call in the loop.
        self._reasoning_printed = ""
        self._content_printed = ""
        self._content_started = False
        # Fresh voice parser for the next LLM call (clean tag buffer).
        self._voice_parser = VoiceTagParser()

    def _on_tool_display(self, raw_result: str, tool_name: str, tool_args: str) -> None:
        p = self.palette
        hint = _arg_hint(tool_args)
        preview = raw_result[:300].replace("\n", " ⏎ ")
        sys.stdout.write(p.blue(f"  [tool] {p.bold(tool_name)}({hint})\n"))
        if preview.strip():
            ellipsis = "…" if len(raw_result) > 300 else ""
            sys.stdout.write(p.gray(f"         {preview}{ellipsis}\n"))
        sys.stdout.flush()

    def _on_agent_message(self, text: str) -> None:
        sys.stdout.write(self.palette.red(f"  ! {text}\n"))
        sys.stdout.flush()
        if not self._last_response:
            self._last_response = text

    # _append / _refresh_monitoring_panel — inherited no-ops (no widget UI).


def _arg_hint(args_json: str) -> str:
    """Compact one-line rendering of a tool-args JSON string."""
    try:
        args = json.loads(args_json)
    except Exception:
        return args_json[:120]
    if not isinstance(args, dict):
        return short_arg(args)
    return ", ".join(f"{k}={short_arg(v)}" for k, v in args.items())
