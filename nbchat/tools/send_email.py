"""app.tools.send_email
=========================

This module implements a very small email‑notification tool that can be
invoked by the OpenAI function‑calling interface.  The tool uses the
``smtplib`` standard library to send a plain‑text email via Gmail's SMTP
server.  All credentials and the recipient address are hard‑coded; only
``subject`` and ``body`` are supplied by the caller.

The public API of this module follows the same pattern as the
``get_weather`` tool: a callable named :data:`func` that returns a JSON
string.  On success the JSON contains a ``result`` key; on failure it
contains an ``error`` key.  The tool is automatically discovered by
``app.tools.__init__``.
"""

from __future__ import annotations

import json
import smtplib
from email import policy
from email.message import EmailMessage
import os

# ---------------------------------------------------------------------------
# Hard‑coded configuration – replace these with your own values.
# ---------------------------------------------------------------------------
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
LOGIN = "ghghang2@gmail.com"          # your Gmail address
PASSWORD = os.getenv("GHG_APP_PASSWORD")          # Gmail app password
TO_ADDR = LOGIN       # recipient email address


def _send_email(subject: str, body: str) -> str:
    """Send an email via Gmail.

    Parameters
    ----------
    subject: str
        Subject line of the email.
    body: str
        Plain‑text body of the email.

    Returns
    -------
    str
        JSON string containing either ``result`` or ``error``.

    Every message is stamped with an ``X-Nbchat: outbound`` header so the
    email bridge never re-injects the agent's own outbound mail.
    """
    try:
        # max_line_length=9999 prevents line-folding of long headers
        # (In-Reply-To, References) which Gmail's threading engine can't parse.
        msg = EmailMessage(policy=policy.SMTP.clone(max_line_length=9999))
        msg["From"] = LOGIN
        msg["To"] = TO_ADDR
        msg["Subject"] = subject
        msg["X-Nbchat"] = "outbound"
        msg.set_content(body)

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(LOGIN, PASSWORD)
            server.send_message(msg)

        return json.dumps({"result": "Email sent successfully"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# Public attributes for auto‑discovery -------------------------------------------------

func = _send_email
name = "send_email"
description = "Send a simple plain‑text email via Gmail using hard‑coded credentials."
