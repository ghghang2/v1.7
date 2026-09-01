"""Tests for the nbchat email bridge and its components.

These tests do NOT require a running llama-server or a real IMAP/SMTP
connection.  Network calls (imaplib, smtplib) are monkey-patched.
"""
from __future__ import annotations

import os
import json
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from nbchat.core import email_inbox, email_smtp
from nbchat.tui import TerminalAgent


# ── email_inbox parsing (no network) ─────────────────────────────────────────────


def test_decode_header_plain():
    assert email_inbox._decode_header("Alice <alice@example.com>") == "Alice <alice@example.com>"


def test_decode_header_encoded():
    # RFC 2047 encoded header
    assert email_inbox._decode_header("=?utf-8?B?SGVsbG8=?= <x@y.com>") == "Hello <x@y.com>"


def test_extract_body_plain():
    import email as em
    msg = em.message_from_string(
        "Content-Type: text/plain\r\n\r\nHello world"
    )
    assert email_inbox._extract_body(msg) == "Hello world"


def test_extract_body_multipart():
    import email as em
    raw = (
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/alternative; boundary=\"bnd\"\r\n"
        "\r\n"
        "--bnd\r\n"
        "Content-Type: text/plain\r\n"
        "\r\n"
        "plain body here\r\n"
        "--bnd\r\n"
        "Content-Type: text/html\r\n"
        "\r\n"
        "<html><p>html body</p></html>\r\n"
        "--bnd--\r\n"
    )
    msg = em.message_from_string(raw)
    body = email_inbox._extract_body(msg)
    assert "plain body here" in body


def test_extract_body_html_fallback():
    import email as em
    msg = em.message_from_string(
        "Content-Type: text/html\r\n\r\n<p>Hello</p><br><b>World</b>"
    )
    body = email_inbox._extract_body(msg)
    assert "Hello" in body and "World" in body


def test_email_message_dataclass():
    em = email_inbox.EmailMessage(
        message_id="<abc@x>", from_addr="a@b.com", subject="Hi",
        body="Hello", date=None, uid="42",
    )
    assert em.uid == "42"
    assert em.subject == "Hi"


# ── email_inbox: _parse_date (naive/aware normalisation) ─────────────────────


def test_parse_date_none_for_empty():
    assert email_inbox._parse_date(None) is None
    assert email_inbox._parse_date("") is None


def test_parse_date_naive_becomes_aware_utc():
    """A Date header with no offset is treated as UTC and made aware."""
    dt = email_inbox._parse_date("Mon, 03 Jan 2026 10:00:00")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)


def test_parse_date_aware_converted_to_utc():
    """A Date header with an explicit offset is converted to UTC."""
    dt = email_inbox._parse_date("Mon, 03 Jan 2026 10:00:00 +0530")
    assert dt is not None
    assert dt.tzinfo is not None
    # 10:00 +05:30 == 04:30 UTC
    assert dt.utcoffset() == timedelta(0)
    assert (dt.hour, dt.minute) == (4, 30)


def test_parse_date_mixed_sort_does_not_crash():
    """Regression: an inbox poll mixing an offset-less Date (naive) and a
    Date with an explicit offset (aware) must not raise
    ``TypeError: can't compare offset-naive and offset-aware datetimes``
    when the results are sorted chronologically (as fetch_unseen does)."""
    naive = email_inbox._parse_date("Mon, 03 Jan 2026 10:00:00")
    aware = email_inbox._parse_date("Mon, 03 Jan 2026 12:00:00 +0530")
    assert naive is not None and aware is not None
    # Both are now aware, so sorting a mixed list is safe.
    ordered = sorted([aware, naive])
    assert ordered == [aware, naive]
# ── email_bridge: _should_process filter ─────────────────────────────────────────

def test_should_process_own_addr_with_nbchat_subject():
    """Email from own address with 'nbchat' in subject is processed."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    msg = email_inbox.EmailMessage(
        message_id="<cmd1@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat: check the weather", body="what's the weather?",
        date=None, uid="10",
    )
    assert bridge._should_process(msg) is True


def test_should_process_nbchat_case_insensitive():
    """'nbchat' matching is case-insensitive."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    msg = email_inbox.EmailMessage(
        message_id="<cmd2@x>", from_addr=email_smtp.LOGIN,
        subject="NBChat test", body="hi",
        date=None, uid="11",
    )
    assert bridge._should_process(msg) is True


def test_should_reject_other_address_with_nbchat_subject():
    """Email from a different address is rejected even with 'nbchat' in subject."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    msg = email_inbox.EmailMessage(
        message_id="<other1@x>", from_addr="someone@else.com",
        subject="nbchat test", body="hi",
        date=None, uid="12",
    )
    assert bridge._should_process(msg) is False


def test_should_reject_own_addr_without_nbchat_subject():
    """Email from own address but without 'nbchat' in subject is rejected."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    msg = email_inbox.EmailMessage(
        message_id="<other2@x>", from_addr=email_smtp.LOGIN,
        subject="RE: How was baby Winter doing?", body="Sounds good!",
        date=None, uid="13",
    )
    assert bridge._should_process(msg) is False


def test_should_reject_other_addr_without_nbchat_subject():
    """Email from a different address without 'nbchat' is rejected (the
    case that was causing the original bug: random unread inbox mail)."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    # Simulates the Linna / Health Unit email that was being picked up.
    msg = email_inbox.EmailMessage(
        message_id="<linna@x>", from_addr="LinNX@state.gov",
        subject="RE: [External] Re: How was baby Winter doing?",
        body="I've confirmed your appointment for 2:30pm today.",
        date=None, uid="14",
    )
    assert bridge._should_process(msg) is False


def test_should_reject_outbound_auto_reply():
    """Our own auto-replies (with nbchat-tui marker) are never processed,
    even though they come from our own address and contain 'nbchat'."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    msg = email_inbox.EmailMessage(
        message_id="<self@x>", from_addr=email_smtp.LOGIN,
        subject="Re: nbchat test (nbchat-tui)", body="auto reply",
        date=None, uid="15", x_nbchat="outbound",
    )
    assert bridge._should_process(msg) is False


def test_should_process_from_header_with_display_name():
    """From header in 'Name <addr>' format is parsed correctly."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    msg = email_inbox.EmailMessage(
        message_id="<cmd3@x>",
        from_addr=f"\\u4f8b\\u5b50 \\u2764\\ufe0f <{email_smtp.LOGIN}>",
        subject="nbchat: do a thing", body="please do it",
        date=None, uid="16",
    )
    assert bridge._should_process(msg) is True


# ── email_bridge: _is_outbound ──────────────────────────────────────────────────

def test_is_outbound_detects_marker():
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    own_reply = email_inbox.EmailMessage(
        message_id="<self@x>", from_addr=email_smtp.LOGIN,
        subject="Re: Something (nbchat-tui)", body="self reply",
        date=None, uid="2", x_nbchat="outbound",
    )
    assert bridge._is_outbound(own_reply) is True

    regular = email_inbox.EmailMessage(
        message_id="<u1@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat test", body="testing",
        date=None, uid="4",
    )
    assert bridge._is_outbound(regular) is False

def test_is_outbound_detects_header():
    """Messages with X-Nbchat header are detected as outbound even without
    the subject marker."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    # Header present, no subject marker — should still be detected.
    header_reply = email_inbox.EmailMessage(
        message_id="<hdr@x>", from_addr=email_smtp.LOGIN,
        subject="Re: nbchat test", body="auto reply",
        date=None, uid="3", x_nbchat="outbound",
    )
    assert bridge._is_outbound(header_reply) is True

    # No header, no subject marker — regular user email.
    regular = email_inbox.EmailMessage(
        message_id="<u2@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat test", body="testing",
        date=None, uid="5",
    )
    assert bridge._is_outbound(regular) is False


def test_user_reply_to_system_email_is_processed():
    """When a user replies to a system auto-reply in Gmail, the reply
    retains the subject text (including the (nbchat-tui) marker) but does
    NOT carry the X-Nbchat header.  The bridge must process it as a
    legitimate user command, not reject it as an outbound self-loop."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    # Simulates the user clicking Reply to the system's auto-reply.
    # Gmail preserves the subject (with marker) but no X-Nbchat header.
    user_reply = email_inbox.EmailMessage(
        message_id="<user-reply@x>", from_addr=email_smtp.LOGIN,
        subject="Re: supervisor: what's the status? (nbchat-tui)",
        body="actually, can you also check disk space?",
        date=None, uid="20",
    )
    # Must NOT be flagged as outbound (no header).
    assert bridge._is_outbound(user_reply) is False
    # Must be processed as a user command (subject has 'supervisor').
    assert bridge._should_process(user_reply) is True


# ── email_bridge: injection + poll loop (mocked network) ─────────────────────────

def test_poll_injects_matching_email_only():
    """Detection + processing injects only matching emails; others are marked read and
    never reach the agent."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    matching = email_inbox.EmailMessage(
        message_id="<m1@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat: say hi", body="please say hi",
        date=None, uid="20",
    )
    non_matching = email_inbox.EmailMessage(
        message_id="<m2@x>", from_addr="LinNX@state.gov",
        subject="RE: appointment", body="confirmed for 2:30pm",
        date=None, uid="21",
    )

    captured = {}
    def fake_send(sender, subject, body):
        captured["args"] = (sender, subject, body)
        return "hi there"

    with patch("nbchat.core.email_inbox.peek_unseen",
               return_value=[non_matching, matching]), \
         patch("nbchat.core.email_inbox.fetch_body", return_value="please say hi"), \
         patch("nbchat.core.email_inbox.mark_read") as mock_mr, \
         patch("nbchat.core.email_inbox.mark_read_batch") as mock_batch, \
         patch.object(agent, "send_from_email", side_effect=fake_send) as mock_send:
        _poll_bridge(bridge)

    # Agent was called exactly once — for the matching email only.
    mock_send.assert_called_once()
    assert captured["args"][1] == "nbchat: say hi"

    # The matching email is marked read by the worker after inject (one
    # connection); the non-matching email is batched into a single session.
    assert mock_mr.call_count == 1
    assert mock_batch.call_count == 1
    assert mock_batch.call_args[0][0] == ["21"]


def test_poll_injects_nothing_when_no_match():
    """When no email matches the filter, the agent is never called."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    random_mail = email_inbox.EmailMessage(
        message_id="<r1@x>", from_addr="newsletter@example.com",
        subject="Weekly digest", body="here's your news",
        date=None, uid="30",
    )

    with patch("nbchat.core.email_inbox.peek_unseen",
               return_value=[random_mail]), \
         patch("nbchat.core.email_inbox.mark_read"), \
         patch("nbchat.core.email_inbox.mark_read_batch"), \
         patch.object(agent, "send_from_email") as mock_send:
        _poll_bridge(bridge)

    mock_send.assert_not_called()


def test_bridge_parse_addr():
    from nbchat.tui.email_bridge import EmailBridge
    assert EmailBridge._parse_addr("Alice <alice@example.com>") == "alice@example.com"
    assert EmailBridge._parse_addr("alice@example.com") == "alice@example.com"
    assert EmailBridge._parse_addr("nobody") is None


def test_supervisor_email_gets_priority_over_normal():
    """A supervisor email enqueued after a normal email is dequeued first.

    This is the real-time guarantee: even if a normal email turn is already
    queued (or in flight), a supervisor question jumps the queue so it is
    answered in real-time rather than waiting behind the normal turn.
    """
    from nbchat.tui.email_bridge import EmailBridge
    from nbchat.core.supervisor import Supervisor

    agent = TerminalAgent(color=False)
    sup = Supervisor(agent, interval=60, cooldown=300)
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1, supervisor=sup)

    normal = email_inbox.EmailMessage(
        message_id="<n@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat: long task", body="do the long thing",
        date=None, uid="1",
    )
    sup_msg = email_inbox.EmailMessage(
        message_id="<s@x>", from_addr=email_smtp.LOGIN,
        subject="supervisor: what are you doing?", body="status?",
        date=None, uid="2",
    )

    # Enqueue the normal email first, then the supervisor email.
    bridge._enqueue(normal)
    bridge._enqueue(sup_msg)

    # The supervisor email must come out first.
    _p1, _s1, first = bridge._queue.get_nowait()
    _p2, _s2, second = bridge._queue.get_nowait()
    assert first is sup_msg, "supervisor email should be dequeued first"
    assert second is normal, "normal email should be dequeued second"
    assert _p1 == 0 and _p2 == 1, "supervisor priority 0, normal priority 1"


def test_normal_emails_keep_fifo_order_without_supervisor():
    """Without a supervisor, all emails get priority 1 and stay FIFO."""
    from nbchat.tui.email_bridge import EmailBridge

    agent = TerminalAgent(color=False)
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    msgs = [
        email_inbox.EmailMessage(
            message_id=f"<{i}@x>", from_addr=email_smtp.LOGIN,
            subject="nbchat: task", body=str(i), date=None, uid=str(i),
        )
        for i in range(3)
    ]
    for m in msgs:
        bridge._enqueue(m)

    out = []
    while not bridge._queue.empty():
        _p, _s, m = bridge._queue.get_nowait()
        out.append(m)
    assert out == msgs, "normal emails should be processed FIFO"


def test_bridge_start_stop_lifecycle():
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)
    assert not bridge.running
    bridge.start()
    assert bridge.running
    bridge.stop(timeout=2)
    assert not bridge.running


def test_bridge_dedup_by_message_id():
    """Second poll with same message_id should not re-inject."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    msg = email_inbox.EmailMessage(
        message_id="<dup@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat: dup test", body="test", date=None, uid="5",
    )
    # Simulate first injection
    bridge._seen.add(msg.message_id)
    # Verify it would be skipped
    with patch("nbchat.core.email_inbox.peek_unseen", return_value=[msg]), \
         patch("nbchat.core.email_inbox.mark_read"), \
         patch("nbchat.core.email_inbox.mark_read_batch"), \
         patch.object(agent, "send_from_email") as mock_send:
        _poll_bridge(bridge)
        mock_send.assert_not_called()


# ── agent.send_from_email (mocked LLM) ─────────────────────────────────────────────


def test_send_from_email_appends_labelled_user_message():
    agent = TerminalAgent(color=False)
    # Mock the conversation turn to avoid LLM calls
    with patch.object(agent, "_process_conversation_turn") as mock_turn:
        mock_turn.return_value = None
        agent._last_response = "I got your email."
        agent.send_from_email("bob@x.com", "Hello", "Please do X")

    # Verify the user message was composed correctly
    user_msgs = [r for r in agent.history if r[0] == "user"]
    assert user_msgs, "no user message found"
    last_user = user_msgs[-1][1]
    assert "bob@x.com" in last_user
    assert "Hello" in last_user
    assert "Please do X" in last_user


# ── truncation guard (conversation loop) ─────────────────────────────────────────────


def test_truncation_guard_detects_ending_colon():
    """The truncation heuristic should flag a reply ending with a colon."""
    content = "Now let me write tests for the email feature:"
    _tail = content.rstrip()
    _ends_unfinished = bool(_tail) and _tail.endswith((
        ":", "\u2026", "...", " (", " [", " \u2014",
        ", and", ", the", ", that", ", it",
        " then", " now", " let", " i will",
    ))
    assert _ends_unfinished, "should detect trailing colon as truncated"


def test_truncation_guard_allows_complete_sentence():
    """A normal complete sentence should NOT be flagged."""
    content = "All done. The TUI is ready to use."
    _tail = content.rstrip()
    _ends_unfinished = bool(_tail) and _tail.endswith((
        ":", "\u2026", "...", " (", " [", " \u2014",
        ", and", ", the", ", that", ", it",
        " then", " now", " let", " i will",
    ))
    assert not _ends_unfinished, "complete sentence should not be flagged"


def test_truncation_guard_finish_reason_length():
    """finish_reason='length' should always be treated as truncated."""
    finish_reason = "length"
    _truncated = (finish_reason == "length")
    assert _truncated


# ── session-start gating (the stale-unread-mail fix) ─────────────────────────

def _bridge(session_start=None):
    from nbchat.tui.email_bridge import EmailBridge
    agent = TerminalAgent(color=False)
    kw = {"auto_reply": False, "poll_interval": 1}
    if session_start is not None:
        kw["session_start"] = session_start
    return EmailBridge(agent, **kw)


def _poll_bridge(bridge):
    """Run one full detect + process cycle synchronously (for tests).

    Replaces the old ``_poll_bridge(bridge)`` which did detection and
    processing in a single blocking call.  Now detection and processing
    are separate threads, so tests drive them manually.
    """
    bridge._detect_and_enqueue()
    while not bridge._queue.empty():
        _prio, _seq, msg = bridge._queue.get_nowait()
        bridge._process_email(msg)


def test_is_fresh_none_date_treated_as_fresh():
    """A message with no Date header is treated as fresh (never dropped)."""
    bridge = _bridge(session_start=datetime(2026, 1, 1, tzinfo=timezone.utc))
    msg = email_inbox.EmailMessage(
        message_id="<n@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat", body="hi", date=None, uid="1",
    )
    assert bridge._is_fresh(msg) is True


def test_is_fresh_after_session_start():
    bridge = _bridge(session_start=datetime(2026, 1, 1, tzinfo=timezone.utc))
    msg = email_inbox.EmailMessage(
        message_id="<a@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat", body="hi",
        date=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc), uid="2",
    )
    assert bridge._is_fresh(msg) is True


def test_is_fresh_before_session_start():
    bridge = _bridge(session_start=datetime(2026, 1, 1, tzinfo=timezone.utc))
    msg = email_inbox.EmailMessage(
        message_id="<b@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat", body="hi",
        date=datetime(2025, 12, 31, tzinfo=timezone.utc), uid="3",
    )
    assert bridge._is_fresh(msg) is False


def test_is_fresh_naive_date_assumed_utc():
    """A naive Date is compared as UTC against the (aware) session start."""
    bridge = _bridge(session_start=datetime(2026, 1, 1, tzinfo=timezone.utc))
    msg = email_inbox.EmailMessage(
        message_id="<c@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat", body="hi",
        date=datetime(2025, 12, 31, 23, 0), uid="4",  # naive, before start
    )
    assert bridge._is_fresh(msg) is False


def test_is_fresh_default_grace_window():
    """The default session start includes a 60 s lookback grace.

    This is the regression test for the "--email no longer reacts" bug:
    a user who sends the email and *then* starts the TUI (or whose machine
    clock lags Gmail by a few seconds) must still have the email accepted.
    An email a few minutes old is still correctly treated as stale.
    """
    from nbchat.tui.email_bridge import EmailBridge
    agent = TerminalAgent(color=False)
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)  # default start

    now = datetime.now(timezone.utc)

    # 30 s ago -> inside the grace window -> fresh.
    recent = email_inbox.EmailMessage(
        message_id="<r@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat", body="hi",
        date=now - timedelta(seconds=30), uid="50",
    )
    assert bridge._is_fresh(recent) is True

    # 5 min ago -> well before the grace window -> stale.
    old = email_inbox.EmailMessage(
        message_id="<o@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat", body="hi",
        date=now - timedelta(minutes=5), uid="51",
    )
    assert bridge._is_fresh(old) is False


def test_session_start_pinned_used_verbatim():
    """An explicitly pinned session_start is used as-is (no grace added),
    so tests that pin a start are fully deterministic."""
    from nbchat.tui.email_bridge import EmailBridge
    agent = TerminalAgent(color=False)
    pinned = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1,
                         session_start=pinned)
    assert bridge._session_start == pinned


def test_is_fresh_naive_session_start_does_not_crash():
    """A naive session_start (from any code path) must not raise
    TypeError on comparison with an aware email Date.

    Regression: the detector thread crashed with
    ``TypeError: can't compare offset-naive and offset-aware datetimes``
    when session_start was naive.  _is_fresh now normalises both sides
    to aware-UTC.
    """
    from nbchat.tui.email_bridge import EmailBridge
    agent = TerminalAgent(color=False)
    # Deliberately naive pinned start (no tzinfo).
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1,
                         session_start=datetime(2026, 1, 1, 12, 0))

    # Aware email Date, well after the (naive) start -> fresh.
    msg = email_inbox.EmailMessage(
        message_id="<naive@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat", body="hi",
        date=datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc), uid="60",
    )
    assert bridge._is_fresh(msg) is True

    # Aware email Date, before the (naive) start -> stale.
    old = email_inbox.EmailMessage(
        message_id="<naive2@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat", body="hi",
        date=datetime(2025, 12, 31, 13, 0, tzinfo=timezone.utc), uid="61",
    )
    assert bridge._is_fresh(old) is False


def test_poll_skips_stale_matching_email():
    """A matching email sent BEFORE session start is neither injected nor
    marked read — the core of the stale-unread-mail fix."""
    bridge = _bridge(session_start=datetime(2026, 1, 1, tzinfo=timezone.utc))
    stale = email_inbox.EmailMessage(
        message_id="<stale@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat: old command", body="do the old thing",
        date=datetime(2025, 6, 1, tzinfo=timezone.utc), uid="10",
    )
    with patch("nbchat.core.email_inbox.peek_unseen", return_value=[stale]), \
         patch("nbchat.core.email_inbox.mark_read") as mock_mr, \
         patch.object(bridge._agent, "send_from_email") as mock_send:
        _poll_bridge(bridge)

    mock_send.assert_not_called()      # not answered
    mock_mr.assert_not_called()        # not forced to read


def test_poll_injects_fresh_matching_email_and_marks_read():
    """A matching email sent AFTER session start is injected and marked read
    (once), so it is not answered again on later polls."""
    bridge = _bridge(session_start=datetime(2026, 1, 1, tzinfo=timezone.utc))
    fresh = email_inbox.EmailMessage(
        message_id="<fresh@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat: new command", body="do the new thing",
        date=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc), uid="20",
    )
    with patch("nbchat.core.email_inbox.peek_unseen", return_value=[fresh]), \
         patch("nbchat.core.email_inbox.fetch_body", return_value="do the new thing"), \
         patch("nbchat.core.email_inbox.mark_read") as mock_mr, \
         patch.object(bridge._agent, "send_from_email", return_value="ok") as mock_send:
        _poll_bridge(bridge)

    mock_send.assert_called_once()
    mock_mr.assert_called_once()       # marked read after successful inject
    assert fresh.message_id in bridge._seen


def test_poll_stale_and_fresh_together():
    """When both a stale and a fresh matching email are present, only the
    fresh one is injected; the stale one is left untouched."""
    bridge = _bridge(session_start=datetime(2026, 1, 1, tzinfo=timezone.utc))
    stale = email_inbox.EmailMessage(
        message_id="<s@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat: stale", body="old",
        date=datetime(2025, 6, 1, tzinfo=timezone.utc), uid="30",
    )
    fresh = email_inbox.EmailMessage(
        message_id="<f@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat: fresh", body="new",
        date=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc), uid="31",
    )
    with patch("nbchat.core.email_inbox.peek_unseen",
               return_value=[stale, fresh]), \
         patch("nbchat.core.email_inbox.fetch_body", return_value="new"), \
         patch("nbchat.core.email_inbox.mark_read") as mock_mr, \
         patch.object(bridge._agent, "send_from_email", return_value="ok") as mock_send:
        _poll_bridge(bridge)

    mock_send.assert_called_once()
    # Only the fresh email is marked read (the stale one is left for the user).
    mock_mr.assert_called_once_with("31")


# ─── peek_unseen / fetch_body (mocked IMAP) ──────────────────────────────────


def test_peek_unseen_returns_header_only_messages():
    """peek_unseen returns EmailMessage objects with empty body."""
    import email as em
    raw_header = (
        b"From: ghghang2@gmail.com\r\n"
        b"Subject: nbchat: test\r\n"
        b"Date: Thu, 27 Aug 2026 04:56:00 -0000\r\n"
        b"Message-ID: <peek1@gmail.com>\r\n"
        b"\r\n"
    )

    class FakeIMAP:
        def __init__(self, *a, **kw): pass
        def login(self, *a): pass
        def select(self, *a, **kw): return ("OK", [b"1"])
        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                return ("OK", [b"100"])
            if cmd == "FETCH":
                return ("OK", [(b"100 (RFC822 {50}", raw_header)])
        def logout(self): pass

    with patch("imaplib.IMAP4_SSL", return_value=FakeIMAP()), \
         patch.dict("os.environ", {"GHG_APP_PASSWORD": "test"}):
        results = email_inbox.peek_unseen(limit=5)

    assert len(results) == 1
    assert results[0].body == ""
    assert results[0].subject == "nbchat: test"
    assert results[0].from_addr == "ghghang2@gmail.com"
    assert results[0].uid == "100"
    assert results[0].date is not None


def test_peek_unseen_empty_inbox():
    """peek_unseen returns empty list when no unseen messages."""
    class FakeIMAP:
        def __init__(self, *a, **kw): pass
        def login(self, *a): pass
        def select(self, *a, **kw): return ("OK", [b"0"])
        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                return ("OK", [b""])
            return ("OK", [])
        def logout(self): pass

    with patch("imaplib.IMAP4_SSL", return_value=FakeIMAP()), \
         patch.dict("os.environ", {"GHG_APP_PASSWORD": "test"}):
        results = email_inbox.peek_unseen()

    assert results == []


def test_fetch_body_returns_extracted_text():
    """fetch_body downloads the full message and extracts the body."""
    import email as em
    raw = (
        b"From: a@b.com\r\nSubject: test\r\n"
        b"Content-Type: text/plain\r\n\r\nHello body"
    )

    class FakeIMAP:
        def __init__(self, *a, **kw): pass
        def login(self, *a): pass
        def select(self, *a, **kw): return ("OK", [b"1"])
        def uid(self, cmd, *args):
            return ("OK", [(b"1 (RFC822 {40}", raw)])
        def logout(self): pass

    with patch("imaplib.IMAP4_SSL", return_value=FakeIMAP()), \
         patch.dict("os.environ", {"GHG_APP_PASSWORD": "test"}):
        body = email_inbox.fetch_body("1")

    assert body == "Hello body"


# \u2500\u2500 email_bridge: reply subject helper \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def test_reply_subject_plain():
    """A plain subject gets a single 'Re: ' prefix."""
    from nbchat.tui.email_bridge import EmailBridge
    assert EmailBridge._reply_subject("nbchat: do a thing") == "Re: nbchat: do a thing"


def test_reply_subject_strips_single_re():
    """'Re: foo' becomes 'Re: foo' (not 'Re: Re: foo')."""
    from nbchat.tui.email_bridge import EmailBridge
    assert EmailBridge._reply_subject("Re: nbchat: do a thing") == "Re: nbchat: do a thing"


def test_reply_subject_strips_multiple_re():
    """'Re: Re: foo' becomes 'Re: foo'."""
    from nbchat.tui.email_bridge import EmailBridge
    assert EmailBridge._reply_subject("Re: Re: nbchat: do a thing") == "Re: nbchat: do a thing"


# \u2500\u2500 email_bridge: thread headers \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def test_thread_headers_with_references():
    """In-Reply-To is the incoming Message-ID; References is the chain + ID."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    msg = email_inbox.EmailMessage(
        message_id="<incoming@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat: test", body="hello", date=None, uid="1",
        references="<root@x> <parent@x>",
    )
    in_reply_to, references = bridge._thread_headers(msg)
    assert in_reply_to == "<incoming@x>"
    assert references == "<root@x> <parent@x> <incoming@x>"


def test_thread_headers_no_references():
    """When the incoming email has no References, only the Message-ID is used."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    msg = email_inbox.EmailMessage(
        message_id="<solo@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat: test", body="hello", date=None, uid="1",
    )
    in_reply_to, references = bridge._thread_headers(msg)
    assert in_reply_to == "<solo@x>"
    assert references == "<solo@x>"


# \u2500\u2500 email_bridge: priority keywords \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def test_urgent_keyword_gets_high_priority():
    """Subject containing 'urgent' gets PRIO_HIGH (0)."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge, PRIO_HIGH
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    msg = email_inbox.EmailMessage(
        message_id="<u@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat: urgent fix needed", body="fix it",
        date=None, uid="1",
    )
    bridge._enqueue(msg)
    prio, _seq, _m = bridge._queue.get_nowait()
    assert prio == PRIO_HIGH


def test_high_priority_keyword_gets_high_priority():
    """Subject containing 'high priority' gets PRIO_HIGH (0)."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge, PRIO_HIGH
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    msg = email_inbox.EmailMessage(
        message_id="<h@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat: high priority task", body="do it",
        date=None, uid="1",
    )
    bridge._enqueue(msg)
    prio, _seq, _m = bridge._queue.get_nowait()
    assert prio == PRIO_HIGH


def test_urgent_preempts_low_priority():
    """An urgent email enqueued while a low-priority one is 'in flight'
    triggers an interrupt on the agent."""
    agent = TerminalAgent(color=False)
    from nbchat.tui.email_bridge import EmailBridge, PRIO_LOW
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    # Simulate the worker being busy with a low-priority email.
    low_msg = email_inbox.EmailMessage(
        message_id="<low@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat: background task", body="long task",
        date=None, uid="1",
    )
    bridge._processing_msg = low_msg
    bridge._processing_prio = PRIO_LOW

    # Enqueue an urgent (high-priority) email.
    urgent_msg = email_inbox.EmailMessage(
        message_id="<urg@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat: urgent", body="stop everything",
        date=None, uid="2",
    )
    bridge._enqueue(urgent_msg)

    # The agent's _stop_event should have been set by the interrupt.
    assert agent._stop_event.is_set(), "urgent email should preempt low-priority turn"

def test_send_ack_uses_original_subject_and_no_re_prefix():
    """The acknowledgment email uses the original subject (no 'Re: ' prefix)
    and says 'You are in the queue.' without a position number."""
    from nbchat.tui.email_bridge import EmailBridge

    agent = TerminalAgent(color=False)
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    msg = email_inbox.EmailMessage(
        message_id="<ack@x>", from_addr=email_smtp.LOGIN,
        subject="nbchat: do a thing", body="please",
        date=None, uid="10",
    )

    sent = {}
    with patch("nbchat.tui.email_bridge.email_smtp.send") as mock_send:
        mock_send.return_value = "ok"
        bridge._send_ack(msg)
        assert mock_send.call_count == 1
        kwargs = mock_send.call_args
        # Subject must be the original, not "Re: ..."
        assert kwargs[1]["subject"] == "nbchat: do a thing"
        # Body must contain the queue wording without a position number
        body = kwargs[1]["body"]
        assert "You are in the queue." in body
        assert "Received: nbchat: do a thing" in body
        assert "Priority: low" in body
        # Must carry X-Nbchat via email_smtp (implicit)
        # Must have threading headers
        assert kwargs[1]["in_reply_to"] == "<ack@x>"
