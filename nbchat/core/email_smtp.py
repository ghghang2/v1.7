"""Reusable SMTP email-sending for nbchat.

Used by both the ``send_email`` tool and the TUI email bridge.
Credentials: ``ghghang2@gmail.com`` + ``GHG_APP_PASSWORD`` env var.
"""
from __future__ import annotations

import os
import smtplib
from email import policy
from email.message import EmailMessage

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
LOGIN = "ghghang2@gmail.com"

# Process-local registry of Message-IDs of emails that were answered via
# the send_email tool (i.e. any outgoing mail whose in_reply_to was set
# here).  The TUI email bridge consults this after the agentic turn
# completes: if the tool already replied to the inbound message, the
# auto-reply is skipped so the user receives exactly one reply.
_OUTBOUND_REPLIES: set = set()


def _password() -> str:
    pw = os.getenv("GHG_APP_PASSWORD")
    if not pw:
        raise RuntimeError("GHG_APP_PASSWORD env variable not set")
    return pw.strip()


def send(to: str, subject: str, body: str, *, in_reply_to: str = "", references: str = "") -> str:
    """Send a plain-text email via Gmail SMTP.

    Parameters
    ----------
    to: Recipient address.
    subject: Subject line.
    body: Plain-text body.
    in_reply_to: Optional Message-ID of the message being replied to.
        When set, the outgoing mail carries an ``In-Reply-To`` header so
        Gmail groups it into the same thread as the original.
    references: Optional space-separated list of Message-IDs forming the
        thread chain.  Appended to the outgoing ``References`` header.

    Returns
    -------
    str
        A human-readable confirmation on success.

    Every message is stamped with an ``X-Nbchat: outbound`` header so the
    email bridge can distinguish system-generated mail from user commands.

    Raises
    ------
    Exception
        On any SMTP / authentication failure.
    """
    # Track tool-mediated replies so the email bridge can suppress its
    # own auto-reply for the same inbound message (one reply, not two).
    if in_reply_to:
        _OUTBOUND_REPLIES.add(in_reply_to)
        _OUTBOUND_REPLIES.discard("")

    # A reply must read as a reply.  If In-Reply-To is set but the
    # subject carries no "Re:" prefix, Gmail's conversation UI can treat
    # the message as a new top-level conversation (subject identical to
    # the thread opener), splitting the thread even though the headers
    # are correct.  Normalize once here so every reply path (tool,
    # bridge, supervisor) is covered regardless of what the caller passes.
    if in_reply_to and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    # Voice blocks are spoken on the voice channel only — strip them so a
    # reply that also contains <voice>...</voice> lines never ships them
    # as email text.
    body = _strip_voice_blocks(body)
    # Use a generous max_line_length so In-Reply-To / References headers
    # are NOT line-folded.  Gmail's threading engine fails to extract
    # Message-IDs from folded continuation lines, which causes replies
    # to appear as new threads instead of joining the original.
    msg = EmailMessage(policy=policy.SMTP.clone(max_line_length=9999))
    msg["From"] = LOGIN
    msg["To"] = to
    msg["Subject"] = subject
    msg["X-Nbchat"] = "outbound"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(LOGIN, _password())
        server.send_message(msg)

    return f"Email sent to {to}: {subject}"


def _strip_voice_blocks(text: str) -> str:
    """Remove ``<voice>...</voice>`` blocks (and their surrounding blank
    lines) from *text* before it is sent as email."""
    if not text or "<voice>" not in text:
        return text
    out = ""
    rest = text
    while True:
        i = rest.find("<voice>")
        if i < 0:
            out += rest
            break
        j = rest.find("</voice>", i + len("<voice>"))
        if j < 0:
            # Unterminated tag — a mistyped close such as ``</voice`` can
            # never match.  Keep the payload; never drop the text that
            # follows a malformed tag (that is how a malformed tag turned a
            # whole reply into an empty email / blank terminal output).
            payload = rest[i + len("<voice>"):]
            if payload.rstrip().endswith("</voice"):
                payload = payload[: -len("</voice")]
            out += rest[:i]
            out += payload
            break
        out += rest[:i]
        rest = rest[j + len("</voice>"):]
    # Collapse the 3+ blank lines left behind into a single blank line.
    return _collapse_blank_lines(out)


def _collapse_blank_lines(text: str) -> str:
    import re
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def was_replied_via_tool(message_id: str) -> bool:
    """True if an outbound mail with in_reply_to==message_id was already
    sent this process (by the send_email tool or any reply path)."""
    return bool(message_id) and message_id in _OUTBOUND_REPLIES


def clear_outbound_registry() -> None:
    """Reset the registry (used by tests and process restarts)."""
    _OUTBOUND_REPLIES.clear()


__all__ = [
    "send",
    "was_replied_via_tool",
    "clear_outbound_registry",
    "SMTP_SERVER",
    "SMTP_PORT",
    "LOGIN",
]
