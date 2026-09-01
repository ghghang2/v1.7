"""IMAP inbox polling for nbchat.

Fetches unseen messages from the Gmail inbox so the TUI email bridge can
inject them into the chat stream as user interjections.

Only uses the standard library (``imaplib`` + ``email``) — no extra deps.
"""
from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class EmailMessage:
    """One decoded inbound email."""
    message_id: str
    from_addr: str
    subject: str
    body: str
    date: datetime | None
    uid: str  # IMAP UID (used to mark as read)
    x_nbchat: str = ""  # value of the X-Nbchat header ("" if absent)
    in_reply_to: str = ""  # value of the In-Reply-To header
    references: str = ""  # value of the References header (space-separated IDs)


def _decode_header(value: str) -> str:
    """Decode a possibly-encoded RFC 2047 header value."""
    parts = email.header.decode_header(value or "")
    out: list[str] = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def _extract_body(msg: email.message.Message) -> str:
    """Return a plain-text representation of the message body.

    Prefers text/plain; falls back to text/html (stripped) when no plain
    part is present.  Handles multipart messages.
    """
    plain: list[str] = []
    html: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype == "text/plain":
                plain.append(part.get_payload(decode=True) and
                             part.get_payload(decode=True).decode(
                                 part.get_content_charset() or "utf-8",
                                 errors="replace") or "")
            elif ctype == "text/html":
                html.append(part.get_payload(decode=True) and
                            part.get_payload(decode=True).decode(
                                part.get_content_charset() or "utf-8",
                                errors="replace") or "")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode(msg.get_content_charset() or "utf-8",
                                  errors="replace")
            if (msg.get_content_type() or "").lower() == "text/html":
                html.append(text)
            else:
                plain.append(text)
    body = "\n".join(p for p in plain if p.strip())
    if not body.strip() and html:
        # Very light HTML → text fallback (strip tags).
        import re
        body = re.sub(r"<[^>]+>", " ", html[0])
        body = re.sub(r"\s+", " ", body).strip()
    return body.strip()


def _parse_date(value: str | None) -> datetime | None:
    """Parse an RFC 2822 ``Date`` header into an aware-UTC datetime.

    ``email.utils.parsedate_to_datetime`` returns a *naive* datetime when
    the header carries no timezone offset and an *aware* one when it does
    (e.g. ``+0530``).  A single inbox poll that mixes the two makes
    ``list.sort`` raise ``TypeError: can't compare offset-naive and
    offset-aware datetimes``.  We normalise every result to aware-UTC so
    callers can sort and compare freely: a missing offset is treated as
    UTC (the overwhelmingly common case), and an explicit offset is
    converted to UTC.  Returns ``None`` when there is no parseable date.
    """
    if not value:
        return None
    dt = email.utils.parsedate_to_datetime(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fetch_unseen(box: str = "INBOX", limit: int = 20) -> list[EmailMessage]:
    """Connect to Gmail and return up to *limit* UNSEEN messages.

    The mailbox is opened read-only and **not** marked read here — the
    caller decides when to mark messages read (after successful injection
    into the chat) so a crash doesn't lose emails.
    """
    import os
    pw = os.getenv("GHG_APP_PASSWORD")
    if not pw:
        raise RuntimeError("GHG_APP_PASSWORD env variable not set")

    host = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        host.login("ghghang2@gmail.com", pw)
        status, _ = host.select(box, readonly=True)
        if status != "OK":
            raise RuntimeError(f"IMAP SELECT failed for {box!r}: {status}")

        # Use UID search so we get stable UIDs (not sequence numbers).
        # host.search() returns sequence numbers that shift when messages
        # are added/removed; host.uid("search", ...) returns immutable UIDs
        # that persist across connections.  mark_read() uses the UID STORE
        # command, so the values passed to it must be real UIDs.
        status, data = host.uid("SEARCH", None, "UNSEEN")
        if status != "OK":
            raise RuntimeError(f"IMAP SEARCH failed: {status}")
        ids = data[0].split()
        if not ids:
            return []

        results: list[EmailMessage] = []
        # Fetch the newest *limit* messages.
        for uid in ids[-limit:]:
            status, fetched = host.uid("FETCH", uid, "(RFC822)")
            if status != "OK" or not fetched:
                continue
            raw = fetched[0][1]
            msg = email.message_from_bytes(raw)
            results.append(EmailMessage(
                message_id=msg.get("Message-ID", f"<{uid}@gmail.com>"),
                from_addr=_decode_header(msg.get("From", "")),
                subject=_decode_header(msg.get("Subject", "(no subject)")),
                body=_extract_body(msg),
                date=_parse_date(msg.get("Date")),
                uid=uid.decode() if isinstance(uid, bytes) else str(uid),
                x_nbchat=(msg.get("X-Nbchat") or "").strip(),
                in_reply_to=(msg.get("In-Reply-To") or "").strip(),
                references=(msg.get("References") or "").strip(),
            ))
        # Keep chronological order (oldest first) for natural injection.
        results.sort(key=lambda e: e.date or datetime.min.replace(tzinfo=timezone.utc))
        return results
    finally:
        try:
            host.logout()
        except Exception:
            pass

def mark_read_batch(uids: list[str], box: str = "INBOX") -> None:
    """Mark multiple messages (by UID) as SEEN in a single IMAP session.

    Far more efficient than calling :func:`mark_read` in a loop: one
    TCP+TLS+login+select round-trip instead of one per UID.
    """
    if not uids:
        return
    import os
    pw = os.getenv("GHG_APP_PASSWORD")
    if not pw:
        raise RuntimeError("GHG_APP_PASSWORD env variable not set")
    host = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        host.login("ghghang2@gmail.com", pw)
        host.select(box)
        # IMAP UID STORE accepts a UID set, but imaplib serialises every
        # argument as a quoted literal, so a single "1 2" string becomes the
        # quoted literal "1 2" and Gmail rejects it with
        # ``BAD [Could not parse command]``.  Passing each UID as its own
        # argument lets imaplib emit them as a proper space-separated set.
        for uid in uids:
            host.uid("STORE", uid, "+FLAGS", "\\Seen")
    finally:
        try:
            host.logout()
        except Exception:
            pass

def peek_unseen(box: str = "INBOX", limit: int = 20) -> list[EmailMessage]:
    """Fast header-only fetch of UNSEEN messages.

    Returns ``EmailMessage`` objects with ``body=""`` — the caller should
    use :func:`fetch_body` to retrieve the full body for messages that
    pass the filter.  This is dramatically faster than :func:`fetch_unseen`
    because it downloads only the ``From``, ``Subject``, ``Date`` and
    ``Message-ID`` headers (~200 bytes each) instead of the full RFC822
    payload (often 10-50 KB each).
    """
    import os
    pw = os.getenv("GHG_APP_PASSWORD")
    if not pw:
        raise RuntimeError("GHG_APP_PASSWORD env variable not set")

    host = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        host.login("ghghang2@gmail.com", pw)
        status, _ = host.select(box, readonly=True)
        if status != "OK":
            raise RuntimeError(f"IMAP SELECT failed for {box!r}: {status}")

        status, data = host.uid("SEARCH", None, "UNSEEN")
        if status != "OK":
            raise RuntimeError(f"IMAP SEARCH failed: {status}")
        ids = data[0].split()
        if not ids:
            return []

        results: list[EmailMessage] = []
        for uid in ids[-limit:]:
            status, fetched = host.uid(
                "FETCH", uid,
                "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID X-NBCHAT IN-REPLY-TO REFERENCES)])"
            )
            if status != "OK" or not fetched:
                continue
            raw = fetched[0][1]
            msg = email.message_from_bytes(raw)
            results.append(EmailMessage(
                message_id=msg.get("Message-ID", f"<{uid}@gmail.com>"),
                from_addr=_decode_header(msg.get("From", "")),
                subject=_decode_header(msg.get("Subject", "(no subject)")),
                body="",  # not fetched yet — use fetch_body()
                date=_parse_date(msg.get("Date")),
                uid=uid.decode() if isinstance(uid, bytes) else str(uid),
                x_nbchat=(msg.get("X-Nbchat") or "").strip(),
                in_reply_to=(msg.get("In-Reply-To") or "").strip(),
                references=(msg.get("References") or "").strip(),
            ))
        results.sort(key=lambda e: e.date or datetime.min.replace(tzinfo=timezone.utc))
        return results
    finally:
        try:
            host.logout()
        except Exception:
            pass


def fetch_body(uid: str, box: str = "INBOX") -> str:
    """Fetch the full body of a single message by IMAP UID.

    Called only for messages that have already passed the bridge's
    filter, so the cost is paid at most once per matching email.
    """
    import os
    pw = os.getenv("GHG_APP_PASSWORD")
    if not pw:
        raise RuntimeError("GHG_APP_PASSWORD env variable not set")

    host = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        host.login("ghghang2@gmail.com", pw)
        host.select(box, readonly=True)
        status, fetched = host.uid("FETCH", uid, "(RFC822)")
        if status != "OK" or not fetched:
            return ""
        raw = fetched[0][1]
        msg = email.message_from_bytes(raw)
        return _extract_body(msg)
    finally:
        try:
            host.logout()
        except Exception:
            pass


def mark_read(uid: str, box: str = "INBOX") -> None:
    """Mark a single message (by UID) as SEEN.

    Done after the email has been successfully injected into the chat, so a
    crash before this point does not silently discard the message.
    """
    import os
    pw = os.getenv("GHG_APP_PASSWORD")
    if not pw:
        raise RuntimeError("GHG_APP_PASSWORD env variable not set")
    host = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        host.login("ghghang2@gmail.com", pw)
        host.select(box)
        host.uid("STORE", uid, "+FLAGS", "\\Seen")
    finally:
        try:
            host.logout()
        except Exception:
            pass
