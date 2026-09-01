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
