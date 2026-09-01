"""Headless agent for the WhatsApp channel.

Inherits the full ContextMixin + ConversationMixin stack — L1/L2 memory,
compression, context windowing, monitoring, tool execution — without any
ipywidgets dependency.

Session isolation
-----------------
Each WhatsApp sender JID gets its own session_id (prefixed "wa:") in the
shared chat_history.db.  All existing DB functions work unchanged.

Usage
-----
    agent = WhatsAppAgent()
    reply = agent.handle("+15551234567@s.whatsapp.net", "Hello!")
"""
from __future__ import annotations

import logging
import threading
from typing import List, Tuple

from nbchat.core.utils import lazy_import
from nbchat.ui.context_manager import ImportanceTracker
from nbchat.ui.context_manager import ContextMixin
from nbchat.ui.conversation import ConversationMixin

_log = logging.getLogger("nbchat.whatsapp")

# Namespace prefix keeps WhatsApp sessions distinct from Jupyter sessions
# in the shared chat_history.db without any schema change.
_SESSION_PREFIX = "wa:"


class WhatsAppAgent(ContextMixin, ConversationMixin):
    """Headless agent that serves WhatsApp messages.

    One instance is created at server startup and handles all senders.
    Session state is switched per request via _switch_session().

    Thread safety: FastAPI dispatches each request on its own thread.
    _switch_session + _process_conversation_turn are called sequentially
    within the same request, so no two requests share an instance
    concurrently provided the HTTP server is configured with a single worker
    (default for uvicorn).  For multi-worker deployments, instantiate one
    WhatsAppAgent per worker process.
    """

    config = lazy_import("nbchat.core.config")
    MAX_TOOL_TURNS = config.MAX_TOOL_TURNS
    # NOTE: WINDOW_TURNS was removed from config (config.py __all__ and
    # repo_config.yaml) but was still referenced here at import time, which
    # crashed the whole module with AttributeError. Removed.

    def __init__(self):
        db = lazy_import("nbchat.core.db")
        db.init_db()

        config = lazy_import("nbchat.core.config")
        self.system_prompt = config.DEFAULT_SYSTEM_PROMPT
        self.model_name = config.MODEL_NAME

        # These are set per-request by _switch_session().
        self.session_id: str = ""
        self.history: List[Tuple[str, str, str, str, str, int]] = []
        self.task_log: List[str] = []
        self._turn_summary_cache: dict = {}
        self._summary_futures: dict = {}
        # ContextMixin._window() / _hard_trim() require an ImportanceTracker
        # and a stop event + history lock (mirrors ChatUI's thread primitives).
        self._importance_tracker = ImportanceTracker(
            persist_fraction=config.PERSIST_FRACTION
        )
        self._stop_event = threading.Event()
        self._history_lock = threading.Lock()

        self._stop_streaming = False
        self._tool_running = False

        # Capture final response text from the streaming hook.
        self._last_response: str = ""

    # ── Public interface ──────────────────────────────────────────────────

    def handle(self, sender_jid: str, text: str) -> str:
        """Process one inbound WhatsApp message and return the reply text.

        Parameters
        ----------
        sender_jid:
            WhatsApp JID of the sender, e.g. "+15551234567@s.whatsapp.net".
        text:
            Plain text content of the inbound message.

        Returns
        -------
        str
            The agent's reply.  Empty string if the agent produced no output
            (should not happen in normal operation).
        """
        self._switch_session(sender_jid)
        self._last_response = ""
        self._stop_streaming = False

        db = lazy_import("nbchat.core.db")
        self.history.append(("user", text, "", "", "", 0))
        db.log_message(self.session_id, "user", text)

        # _process_conversation_turn runs synchronously here.  The HTTP
        # handler thread plays the role that ChatUI's background thread plays
        # in the Jupyter UI.
        self._process_conversation_turn()

        return self._last_response

    # ── Session management ────────────────────────────────────────────────

    def _switch_session(self, sender_jid: str) -> None:
        """Load history for this sender, creating a new session if first contact."""
        new_id = f"{_SESSION_PREFIX}{sender_jid}"
        if new_id == self.session_id:
            return  # same sender, history already loaded

        comp = lazy_import("nbchat.core.compressor")
        db = lazy_import("nbchat.core.db")

        self.session_id = new_id
        self.history = list(db.load_history(self.session_id))
        self.task_log = db.load_task_log(self.session_id)
        self._turn_summary_cache = db.load_turn_summaries(self.session_id)
        comp.init_session(self.session_id)

        _log.debug(f"switched to session {self.session_id} ({len(self.history)} rows)")

    # ── Output hook overrides (ConversationMixin interface) ───────────────

    def _on_stream_token(self, content: str) -> None:
        """Capture accumulated assistant text as streaming progresses."""
        self._last_response = content

    def _on_stream_complete(self, content: str, tool_calls: list | None) -> None:
        """Ensure final content is captured (handles tool-call-only turns)."""
        if content:
            self._last_response = content

    def _on_agent_message(self, text: str) -> None:
        """Log warnings / errors that would appear in the Jupyter UI."""
        _log.warning(f"agent notice [{self.session_id}]: {text}")
        if not self._last_response:
            self._last_response = text

    # _on_stream_reasoning, _on_tool_display — inherited no-ops, correct for WA
    # _append, _refresh_monitoring_panel — inherited no-ops, correct for WA