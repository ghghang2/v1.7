"""Email bridge for the nbchat terminal UI.

Extends the TUI chat input box to the email layer: a daemon thread polls the
Gmail inbox (IMAP) and, for each **matching** message, injects it into the
agent's chat stream as a user interjection — exactly as if the user typed it
into the terminal.  Optionally it sends the agent's reply back by email.

Filtering
---------
Only emails that satisfy BOTH conditions are injected:

1. Sent **from the user's own address** (``ghghang2@gmail.com``).
2. Subject contains the string ``nbchat`` (case-insensitive).

All other inbox traffic (colleagues, newsletters, auto-replies, etc.) is
silently marked read and ignored.

On top of that, only mail **sent since this chat session started** is ever
injected.  Older unread mail is left completely untouched (not responded to,
not marked read) so the user is never forced to read stale mail just to use
the email bridge.

Design
------
* ``agent.send_from_email(...)`` is called under the agent's ``_send_lock``
  (acquired inside ``_run_turn``), so an email turn and a terminal turn can
  never run the LLM loop concurrently — the inbox is an extension of the same
  input box, not a parallel conversation.
* Emails are marked read **only after** they have been injected, so a crash
  before that point does not silently discard a message.
* All IMAP/SMTP calls are isolated in ``nbchat.core.email_inbox`` /
  ``email_smtp``; errors are logged and the loop continues (one bad poll
  must not kill the bridge).

Run with:  ``python -m nbchat.tui --email``
"""
from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timedelta, timezone

from nbchat.core import config
from nbchat.core import email_inbox, email_smtp

_log = logging.getLogger("nbchat.tui.email")

# Mark our own replies so we never re-process (and reply to) ourselves.
OUTBOUND_MARKER = "nbchat-tui"

# Subject keywords that mark an email as HIGH priority (preempts low-priority
# work in flight).  Matched case-insensitively against the subject line.
HIGH_PRIORITY_KEYWORDS = ("supervisor", "urgent", "high priority")

# Priority values (lower number = higher priority, dequeued first).
PRIO_HIGH = 0
PRIO_LOW = 1


class EmailBridge:
    """Background thread that pipes the Gmail inbox into the chat."""

    def __init__(self, agent, *, auto_reply: bool | None = None,
                 poll_interval: int | None = None,
                 my_addr: str = email_smtp.LOGIN,
                 session_start: datetime | None = None,
                 supervisor=None) -> None:
        self._agent = agent
        self._auto_reply = (
            config.EMAIL_AUTO_REPLY if auto_reply is None else auto_reply
        )
        self._poll_interval = (
            config.EMAIL_POLL_INTERVAL if poll_interval is None else poll_interval
        )
        self._my_addr = my_addr
        self._supervisor = supervisor
        self._stop = threading.Event()
        self._seen: set[str] = set()   # in-memory Message-ID dedupe (belt & suspenders)
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        # Priority queue of (priority, seq, EmailMessage) tuples waiting to
        # be processed by the worker thread.  Detection (IMAP) and processing
        # (LLM) are decoupled so the detector never blocks on a slow turn.
        #
        # Supervisor emails get priority 0, normal emails priority 1, and a
        # monotonically increasing sequence number breaks ties within the
        # same priority (FIFO).  This lets a supervisor email jump ahead of
        # a queued normal email so it is answered in real-time even while
        # the assistant is mid-stream on a long turn.
        self._queue: "queue.PriorityQueue" = queue.PriorityQueue()
        self._seq = 0
        self._seq_lock = threading.Lock()
        # Tracks what the worker is currently processing (for preemption).
        self._processing_msg: "email_inbox.EmailMessage | None" = None
        self._processing_prio: int = PRIO_LOW
        # Timestamp of this chat session's start.  The bridge only injects
        # mail sent at/after this moment, so pre-existing unread mail
        # (days/weeks old) is never answered.  When no explicit
        # session_start is given we default to "now" **minus a 60 s
        # lookback grace**, which handles two real-world cases:
        #   1. The user sends the email and *then* starts the TUI.
        #   2. Minor clock skew between the user's machine and Gmail.
        # A pinned session_start (e.g. from tests) is used verbatim.
        if session_start is not None:
            self._session_start = session_start
        else:
            self._session_start = (
                datetime.now(timezone.utc) - timedelta(seconds=60)
            )

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="nbchat-email-detect", daemon=True
        )
        self._worker = threading.Thread(
            target=self._worker_loop, name="nbchat-email-worker", daemon=True
        )
        self._thread.start()
        self._worker.start()
        _log.info("email bridge started (detect every %ss, auto_reply=%s)",
                  self._poll_interval, self._auto_reply)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._worker:
            self._worker.join(timeout=timeout)
            self._worker = None
        _log.info("email bridge stopped")

    @property
    def running(self) -> bool:
        return bool(
            self._thread and self._thread.is_alive()
            and self._worker and self._worker.is_alive()
        )

    # ── Poll loop ──────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._detect_and_enqueue()
            except Exception as exc:  # one bad poll must not kill the bridge
                _log.warning("email poll failed: %s: %s",
                             type(exc).__name__, exc)
            # Event.wait lets us stop promptly and is interruptible.
            self._stop.wait(self._poll_interval)

    def _detect_and_enqueue(self) -> None:
        """Fast header-only poll: peek unseen mail, filter, fetch bodies
        for matches, and enqueue.

        This runs in the detector thread and must be fast.  It does NOT
        call ``send_from_email`` (that is the worker's job).  Only emails
        from our own address with 'nbchat' in the subject are enqueued;
        all others are marked read and silently skipped.

        Ordering and efficiency notes
        ----------------------------
        * Messages are processed **newest-first** so a fresh command email
          (always the most recent) is enqueued on the first iteration,
          without waiting behind any bookkeeping for older mail.
        * The freshness check runs **before** the match filter and before
          any IMAP write.  Stale mail (older than the session-start grace
          window) is skipped with zero connections — this is what makes a
          large UNSEEN backlog (thousands of old messages) invisible to the
          bridge instead of costing one IMAP session per message.
        * All ``mark_read`` writes for skipped *fresh* mail are batched into
          a single IMAP session at the end of the poll, rather than one
          connect/login/STORE round-trip per message.
        """
        msgs = email_inbox.peek_unseen(limit=20)
        # Newest-first: a fresh command email is always the most recent, so
        # handling it first guarantees it is enqueued before any of the
        # (rare) fresh non-command mail that might precede it.
        msgs.sort(
            key=lambda e: e.date or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        mark_uids: list[str] = []
        for msg in msgs:
            if self._stop.is_set():
                break
            # Freshness first: stale mail is left completely untouched
            # (not marked read, not responded to), with no IMAP write.
            if not self._is_fresh(msg):
                continue
            # Only process deliberate user commands: from our own address
            # with 'nbchat' in the subject.  Everything else is skipped.
            if not self._should_process(msg):
                mark_uids.append(msg.uid)
                continue
            # Dedup check (belt & suspenders for in-flight messages).
            if msg.message_id in self._seen:
                mark_uids.append(msg.uid)
                continue
            # Mark as seen immediately to prevent double-enqueue.
            self._seen.add(msg.message_id)
            # Fetch the full body now (only for matching, fresh emails).
            try:
                msg.body = email_inbox.fetch_body(msg.uid)
            except Exception as exc:
                _log.warning("failed to fetch body for %s: %s", msg.uid, exc)
                continue
            self._enqueue(msg)
            _log.info("enqueued email: %r from %s", msg.subject, msg.from_addr)
            # Send immediate acknowledgment so the user knows the email
            # was received and queued.
            self._send_ack(msg)
        # Mark all skipped fresh mail read in ONE IMAP session (was: one
        # connection per message).  Stale mail was never added to this list.
        if mark_uids:
            try:
                email_inbox.mark_read_batch(mark_uids)
            except Exception as exc:
                _log.warning(
                    "batch mark_read failed for %d msg(s): %s: %s",
                    len(mark_uids), type(exc).__name__, exc,
                )

    def _worker_loop(self) -> None:
        """Processing loop: dequeue and process emails one at a time.

        Runs in the worker thread.  Each email is processed sequentially
        via ``agent.send_from_email`` (which holds ``_send_lock`` for the
        entire LLM turn).  The detector thread keeps running independently,
        so new emails are always picked up within one poll interval even
        while the worker is busy.

        Emails are dequeued in priority order: high-priority emails
        (priority 0) are processed before low-priority emails (priority 1).
        Ties within a priority are resolved FIFO by the sequence number.

        Preemption: if a high-priority email arrives while a low-priority
        email is being processed, the detector calls ``agent.interrupt()``.
        The worker detects the interrupted turn and re-queues the email so
        it can be retried after the high-priority work completes.
        """
        while not self._stop.is_set():
            try:
                _prio, _seq, msg = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            # Notify the user that processing has started.
            self._send_processing(msg)
            # Track what we are processing (for preemption checks).
            self._processing_msg = msg
            self._processing_prio = _prio
            try:
                self._process_email(msg)
            except Exception as exc:
                _log.warning("email processing failed: %s: %s",
                             type(exc).__name__, exc)
            finally:
                self._processing_msg = None
                self._processing_prio = PRIO_LOW

    def _enqueue(self, msg: "email_inbox.EmailMessage") -> None:
        """Place *msg* on the priority queue with the right priority.

        High-priority emails (subject contains any of
        ``HIGH_PRIORITY_KEYWORDS``) get priority 0 so they jump ahead of
        queued low-priority emails (priority 1).  A monotonically increasing
        sequence number breaks ties FIFO and keeps the tuple comparable.

        Preemption: if this is a high-priority email and the worker is
        currently processing a low-priority one, the in-flight turn is
        interrupted so the high-priority email can be handled immediately.
        """
        subj = (msg.subject or "").lower()
        prio = PRIO_HIGH if any(kw in subj for kw in HIGH_PRIORITY_KEYWORDS) else PRIO_LOW
        with self._seq_lock:
            seq = self._seq
            self._seq += 1
        self._queue.put((prio, seq, msg))
        # Preempt a low-priority turn in flight if this is high priority.
        if prio == PRIO_HIGH and self._processing_prio == PRIO_LOW:
            _log.info("preempting low-priority email %r for high-priority %r",
                      self._processing_msg.subject if self._processing_msg else "?",
                      msg.subject)
            try:
                self._agent.interrupt()
            except Exception as exc:
                _log.warning("preempt interrupt failed: %s: %s",
                             type(exc).__name__, exc)

    def _process_email(self, msg) -> None:
        """Process a single email: inject into chat, mark read, auto-reply.

        If the turn was interrupted (by a high-priority preemption), the
        email is re-queued at its original priority so it can be retried
        after the high-priority work completes.
        """
        # Route supervisor questions: subject contains "supervisor".
        if self._supervisor is not None and "supervisor" in msg.subject.lower():
            self._handle_supervisor_email(msg)
            return

        # Inject into the chat stream (blocks until the turn completes).
        reply = self._agent.send_from_email(
            msg.from_addr, msg.subject, msg.body
        )

        # Mark read (only after successful inject).
        try:
            email_inbox.mark_read(msg.uid)
        except Exception as exc:
            _log.warning("failed to mark read %s: %s", msg.uid, exc)

        # If the turn was interrupted (preemption), re-queue this email.
        if self._agent._stop_event.is_set():
            _log.info("turn interrupted; re-queuing %r", msg.subject)
            with self._seq_lock:
                seq = self._seq
                self._seq += 1
            self._queue.put((PRIO_LOW, seq, msg))
            return

        # Optionally reply to the sender by email (in the same thread).
        if self._auto_reply and reply:
            try:
                in_reply_to, references = self._thread_headers(msg)
                email_smtp.send(
                    to=self._parse_addr(msg.from_addr) or msg.from_addr,
                    subject=self._reply_subject(msg.subject),
                    body=reply,
                    in_reply_to=in_reply_to,
                    references=references,
                )
                _log.info("auto-replied to %s", msg.from_addr)
            except Exception as exc:
                _log.warning("auto-reply failed: %s: %s",
                             type(exc).__name__, exc)

    # ── Helpers ──────────────────────────────────────────────────────

    def _handle_supervisor_email(self, msg) -> None:
        """Answer a supervisor question by email.

        The email body is treated as the question.  The supervisor gathers
        the live state snapshot and returns an answer, which is emailed back
        to the sender (if auto-reply is on) and logged to the terminal.
        """
        question = msg.body.strip() or msg.subject
        _log.info("supervisor email question from %s: %s",
                  msg.from_addr, question[:80])

        answer = self._supervisor.ask(question)

        # Log to terminal so the user sees it in the TUI.
        p = getattr(self._agent, "palette", None)
        if p is not None:
            import sys
            sys.stdout.write(p.magenta(f"  [supervisor] {question[:60]}\n"))
            for line in answer.splitlines() or [""]:
                sys.stdout.write("  " + line + "\n")
            sys.stdout.write("\n")
            sys.stdout.flush()

        # Record + mark read.
        self._seen.add(msg.message_id)
        try:
            email_inbox.mark_read(msg.uid)
        except Exception as exc:
            _log.warning("failed to mark read %s: %s", msg.uid, exc)

        # Optionally reply by email (in the same thread).
        if self._auto_reply:
            try:
                in_reply_to, references = self._thread_headers(msg)
                email_smtp.send(
                    to=self._parse_addr(msg.from_addr) or msg.from_addr,
                    subject=self._reply_subject(msg.subject),
                    body=answer,
                    in_reply_to=in_reply_to,
                    references=references,
                )
                _log.info("supervisor auto-replied to %s", msg.from_addr)
            except Exception as exc:
                _log.warning("supervisor auto-reply failed: %s: %s",
                             type(exc).__name__, exc)

    # ── Acknowledgment & notification helpers ──────────────────────────────────────────────

    def _send_ack(self, msg) -> None:
        """Send an immediate acknowledgment for a newly enqueued email.

        This gives the user instant feedback that their email was received
        and queued, without waiting for the LLM turn to complete.
        """
        subj = (msg.subject or "").lower()
        prio_label = "high" if any(kw in subj for kw in HIGH_PRIORITY_KEYWORDS) else "low"
        body = (
            f"Received: {msg.subject}\n"
            f"Priority: {prio_label}\n"
            f"You are in the queue."
        )
        try:
            in_reply_to, references = self._thread_headers(msg)
            email_smtp.send(
                to=self._parse_addr(msg.from_addr) or msg.from_addr,
                subject=msg.subject,
                body=body,
                in_reply_to=in_reply_to,
                references=references,
            )
            _log.info("ack sent for %r", msg.subject)
        except Exception as exc:
            _log.warning("ack send failed for %r: %s: %s",
                         msg.subject, type(exc).__name__, exc)

    def _send_processing(self, msg) -> None:
        """Send a notification that the email is now being processed."""
        body = f"Working on: {msg.subject}\nThis may take a moment."
        try:
            in_reply_to, references = self._thread_headers(msg)
            email_smtp.send(
                to=self._parse_addr(msg.from_addr) or msg.from_addr,
                subject=msg.subject,
                body=body,
                in_reply_to=in_reply_to,
                references=references,
            )
            _log.info("processing notification sent for %r", msg.subject)
        except Exception as exc:
            _log.warning("processing notification failed for %r: %s: %s",
                         msg.subject, type(exc).__name__, exc)

    def _thread_headers(self, msg) -> tuple[str, str]:
        """Build In-Reply-To and References headers for a reply to *msg*.

        Returns (in_reply_to, references) strings.  If the incoming message
        has no Message-ID, both are empty strings (no threading).
        """
        in_reply_to = msg.message_id or ""
        # References: the incoming References chain + the incoming Message-ID.
        refs_parts = []
        if getattr(msg, "references", ""):
            refs_parts.extend(msg.references.split())
        if msg.message_id:
            refs_parts.append(msg.message_id)
        references = " ".join(refs_parts)
        return in_reply_to, references

    @staticmethod
    def _reply_subject(original: str) -> str:
        """Build a reply subject: strip leading 'Re: ' then prefix 'Re: '.

        Avoids 'Re: Re: Re: ...' stacking when the user replies multiple
        times within the same thread.
        """
        subj = original
        while subj.lower().startswith("re:"):
            subj = subj[3:].lstrip()
        return f"Re: {subj}"

    def _is_outbound(self, msg) -> bool:
        """True if this is one of our own auto-replies (avoid self-loops).

        The sole signal is the ``X-Nbchat`` header.  Every message sent
        through :func:`nbchat.core.email_smtp.send` or the ``send_email``
        tool carries ``X-Nbchat: outbound``.  A user who replies to a
        system email in Gmail gets a new message that retains the
        subject text (including any ``(nbchat-tui)`` marker) but does
        NOT inherit the custom header, so the header is the only
        reliable way to distinguish system mail from user mail within
        the same thread.
        """
        return bool(getattr(msg, "x_nbchat", ""))

    def _should_process(self, msg) -> bool:
        """True if this email should be injected into the chat.

        Only emails that satisfy **all** conditions are processed:

        1. NOT one of our own auto-replies (subject has no ``(nbchat-tui)``).
        2. Sent from our own address (``ghghang2@gmail.com``).
        3. Subject contains the string ``nbchat`` (case-insensitive).
        4. Sent at or after this chat session started (see ``_is_fresh``).

        This ensures the bridge acts as a deliberate command channel —
        the user sends themselves an email with 'nbchat' in the subject,
        and it gets injected as a user turn.  All other inbox traffic
        (colleagues, newsletters, etc.) is silently ignored.
        """
        # Skip our own auto-replies (prevent self-loops).
        if self._is_outbound(msg):
            return False
        # Must be from our own address.
        from_addr = self._parse_addr(msg.from_addr)
        if not from_addr or from_addr.lower() != self._my_addr.lower():
            return False
        # Must have 'nbchat' in the subject.
        # 'nbchat' routes to the assistant; 'supervisor' routes to the
        # supervisor (when one is attached).  Either keyword is a deliberate
        # command from the user's own address.
        subj = msg.subject.lower()
        if "nbchat" not in subj and "supervisor" not in subj:
            return False
        return True

    def _is_fresh(self, msg) -> bool:
        """True if *msg* was sent at or after this chat session started.

        This is the guard that stops the bridge from answering **older
        unread** mail that merely happens to match the filter.  A message
        with no parseable ``Date`` is treated as fresh (we cannot prove it
        is stale), so a legitimate command is never silently dropped; in
        practice Gmail always populates the ``Date`` header.

        Both sides of the comparison are normalised to aware-UTC to
        avoid ``TypeError: can't compare offset-naive and offset-aware
        datetimes`` regardless of what the IMAP server or the caller
        hands us.
        """
        if msg.date is None:
            return True
        msg_date = msg.date
        if msg_date.tzinfo is None:
            msg_date = msg_date.replace(tzinfo=timezone.utc)
        else:
            msg_date = msg_date.astimezone(timezone.utc)
        # Normalise the session start too (defensive: a naive session_start
        # from any code path would otherwise crash the comparison).
        start = self._session_start
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return msg_date >= start

    @staticmethod
    def _parse_addr(from_header: str) -> str | None:
        """Extract a bare email address from an RFC 5322 From header."""
        import email.utils
        _name, addr = email.utils.parseaddr(from_header)
        return addr if addr and "@" in addr else None


def start_for(agent, **kw) -> EmailBridge:
    """Convenience: construct + start a bridge for *agent*."""
    bridge = EmailBridge(agent, **kw)
    bridge.start()
    return bridge
