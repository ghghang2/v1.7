"""app.tools.send_email
=========================

This module implements a small, **stateless** email-sending tool that can be
invoked by the OpenAI function-calling interface.  It delegates all SMTP
handling to :mod:`nbchat.core.email_smtp` (shared with the TUI email
bridge) so that every outbound message carries the same ``X-Nbchat:
outbound`` marker and the same thread-safe header handling.

Threading
---------
When the user's request arrives via the TUI email bridge, the injected
message includes a ``Message-ID: <...>`` line.  Supplying that value as
``message_id`` makes this tool emit ``In-Reply-To`` / ``References``
headers so the email client (e.g. Gmail) groups the sent message into the
original thread instead of opening a new one.  For non-email requests
leave ``message_id`` empty - the message is a standalone send.

The public API of this module follows the same pattern as the
``get_weather`` tool: a callable named :data:`func` that returns a JSON
string.  On success the JSON contains a ``result`` key; on failure it
contains an ``error`` key.  The tool is automatically discovered by
``app.tools.__init__``.
"""

from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# Recipient.  The tool is stateless: the agent passes the recipient address
# explicitly (taken from the email header block of an incoming email, or
# from the user's request).  All credentials are resolved by the shared
# SMTP helper.
# ---------------------------------------------------------------------------
DEFAULT_TO = "ghghang2@gmail.com"


def _send_email(subject: str, body: str, to: str = "", message_id: str = "") -> str:
    """Send a plain-text email via Gmail SMTP.

    Parameters
    ----------
    subject: Subject line.  Prefix with ``Re: `` when answering a request
        that arrived by email so the thread reads naturally.
    body: Plain-text body.  Do NOT include ``<voice>`` blocks - any that
        slip in are stripped before sending; the voice channel is for
        speech, not for email.
    to: Recipient address.  Defaults to the account owner.
    message_id: The ``Message-ID`` (in ``<...>`` form) of the email this
        message replies to.  When set, the outgoing mail carries
        ``In-Reply-To`` / ``References`` headers so it lands in the same
        thread as the original.  Empty for standalone messages.

    Returns
    -------
    str
        JSON string containing either ``result`` or ``error``.
    """
    try:
        from nbchat.core import email_smtp
        email_smtp.send(
            to=to or DEFAULT_TO,
            subject=subject,
            body=body,
            in_reply_to=message_id,
            references=message_id,
        )
        return json.dumps({"result": "Email sent successfully"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# Public attributes for auto-discovery -------------------------------------------------

func = _send_email
name = "send_email"
description = (
    "Send a plain-text email via Gmail.  Provide `to` (recipient), "
    "`subject` and `body`.  If the message answers a request that arrived "
    "by email, also provide `message_id` (the Message-ID of the incoming "
    "email, in <...> form) so the reply is grouped into the same thread. "
    "For standalone messages leave `message_id` empty."
)
