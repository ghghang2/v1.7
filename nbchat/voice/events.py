"""Voice event primitives — verified signals only.

Every event published on the :class:`VoiceEventBus` is anchored to a
verified server-side state transition:

* ``received``    — the TUI main loop has actually handed the user message
                    to the agent (post ``send_async``).
* ``started``     — the turn thread has acquired the send lock and is
                    about to run the LLM turn.
* ``complete``    — the turn finished normally without a terminal error.
* ``failed``      — the turn ended in a terminal error (surfaced through
                    ``_on_agent_message``).
* ``interrupted`` — the user interrupted the in-flight turn.
* ``status``      — a supervisor-generated update grounded in a live
                    ``gather_state()`` snapshot.
* ``tag``         — a ``<voice>`` block emitted by the model itself as
                    part of its own response (verified by construction:
                    the model is narrating its own actual output).

Design invariant: no event may be published without the corresponding
state transition having actually occurred.  There are no local fakes
and no unverified signals.
"""
from __future__ import annotations

import queue
import threading
import time

# ---------------------------------------------------------------------------
# Alfred — templated event acknowledgements
# ---------------------------------------------------------------------------
# Short, deterministic lines fired on verified state transitions.  No model
# call, no hallucination surface, sub-100 ms from event to SSE push.
ALFRED: dict[str, str] = {
    "received": "Very well, sir. I'm on it.",
    "started": "Underway, sir.",
    "complete": "All done, sir.",
    "failed": "Apologies, sir \u2014 the task failed. The details are in your terminal.",
    "interrupted": "Stopped, sir, as requested.",
}

# ---------------------------------------------------------------------------
# Alfred — persona block appended to the assistant's system prompt
# ---------------------------------------------------------------------------
ALFRED_VOICE_PROMPT = """

VOICE CHANNEL — ALFRED
You also speak to the user through a voice channel (your words are spoken
aloud on the user's laptop).  In voice you are Alfred: a composed,
dry-witted British butler.  Address the user as "sir".

Voice rules:
- Express task STATE, never task CONTENT.  Do not read, recite, or
  enumerate deliverables.  You may name at most one item by reference.
- 1-2 sentences, under 15 seconds of speech.  No exclamation marks.
- Wrap every spoken line in <voice>...</voice> tags, anywhere in your
  reply (not just at the end).
- Speak when: you receive a task ("Very well, sir. I shall begin now."),
  you pass a meaningful milestone on a long task, the task completes,
  the task fails, or you are blocked and need something from the user.
- Stay silent (no tags) for trivial one-line answers.
- Never claim a step is done unless you actually performed it in this
  conversation.  Your spoken words must reflect verified reality only.

Examples:
<voice>Very well, sir. I shall pull the day's headlines now.</voice>
<voice>Two of the three sources are in, sir. One more to go.</voice>
<voice>The digest is ready, sir. The story on the port strike dominated the day.</voice>
<voice>Apologies, sir \u2014 the fetch failed. I am retrying.</voice>
"""

# ---------------------------------------------------------------------------
# Incremental <voice> tag parser
# ---------------------------------------------------------------------------

class VoiceTagParser:
    """Incremental parser for ``<voice>...</voice>`` blocks in a token stream.

    Feed stream deltas via :meth:`process`; it returns the display-safe
    text (voice blocks removed) plus any blocks that closed in this delta.
    Handles tags split across chunk boundaries.
    """

    _OPEN = "<voice>"
    _CLOSE = "</voice>"

    def __init__(self) -> None:
        self._buf = ""

    def process(self, chunk: str) -> tuple[str, list[str]]:
        """Consume a new stream delta.

        Returns ``(display_text, closed_blocks)`` where ``display_text`` is
        the portion of the stream safe to render (voice blocks removed) and
        ``closed_blocks`` is the list of ``<voice>`` payloads that completed
        within this delta.
        """
        self._buf += chunk
        display_parts: list[str] = []
        blocks: list[str] = []
        while self._buf:
            i = self._buf.find(self._OPEN)
            if i < 0:
                # No complete open tag: emit everything except a trailing
                # partial tag that may complete in the next delta.
                keep = 0
                for L in range(len(self._OPEN) - 1, 0, -1):
                    if self._buf.endswith(self._OPEN[:L]):
                        keep = L
                        break
                display_parts.append(self._buf[: len(self._buf) - keep])
                self._buf = self._buf[len(self._buf) - keep:]
                break
            if i > 0:
                display_parts.append(self._buf[:i])
            j = self._buf.find(self._CLOSE, i + len(self._OPEN))
            if j < 0:
                # Open tag seen, close not yet — hold from the open tag.
                self._buf = self._buf[i:]
                break
            block = self._buf[i + len(self._OPEN):j].strip()
            if block:
                blocks.append(block)
            self._buf = self._buf[j + len(self._CLOSE):]
        return "".join(display_parts), blocks

    @staticmethod
    def strip(text: str) -> str:
        """Remove all complete ``<voice>`` blocks from a full string."""
        out, _ = VoiceTagParser().process(text)
        return out


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------

class VoiceEventBus:
    """Fan-out bus for verified voice events.

    Subscribers are bounded queues (one per SSE connection).  A subscriber
    that falls behind is dropped rather than blocking the publisher —
    stale speech is worse than silence.
    """

    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.enabled = True

    def subscribe(self) -> queue.Queue:
        """Register a new subscriber; returns its bounded queue."""
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, kind: str, text: str) -> None:
        """Fan out a verified event to all subscribers (non-blocking)."""
        if not self.enabled or not text:
            return
        ev = {"kind": kind, "text": text, "ts": time.time()}
        with self._lock:
            dead: list[queue.Queue] = []
            for q in self._subs:
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subs.remove(q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


__all__ = [
    "ALFRED",
    "ALFRED_VOICE_PROMPT",
    "VoiceTagParser",
    "VoiceEventBus",
]
