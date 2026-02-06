import imaplib
import os
import threading
import time
from email import message_from_bytes
from email.header import decode_header, make_header
from typing import Optional

from .db import SessionLocal
from .models import Capture


"""
Email ingestion via IMAP.

This module implements a very small, explicit poller that:
- connects to Gmail via IMAP
- searches for messages matching a fixed query
- creates Capture records for each email (idempotently)

No AI, clarification, or RTM integration is performed here.
"""


POLL_INTERVAL_SECONDS = 60


def _get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name, default)
    return value


def _build_gmail_search_query() -> str:
    """
    Build the Gmail search query string.

    from:jiaarnio@gmail.com AND label:gtd-input AND -label:gtd-processed
    """
    return 'from:jiaarnio@gmail.com label:gtd-input -label:gtd-processed'


def _open_imap_connection() -> Optional[imaplib.IMAP4_SSL]:
    host = _get_env("IMAP_HOST", "imap.gmail.com")
    user = _get_env("IMAP_USERNAME")
    password = _get_env("IMAP_PASSWORD")

    if not user or not password:
        # Without credentials we simply skip ingestion; this keeps the
        # system runnable without email configured.
        return None

    port_raw = _get_env("IMAP_PORT", "993")
    try:
        port = int(port_raw)
    except ValueError:
        port = 993

    client = imaplib.IMAP4_SSL(host, port)
    client.login(user, password)

    # Select INBOX in read-write mode so labels can be updated later if
    # desired. For now we do not modify labels on failure.
    client.select("INBOX")
    return client


def _get_message_body(msg) -> str:
    """
    Extract a reasonable text body from an email message.

    Preference order: text/plain part, else fallback to raw payload.
    """
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        # Fallback to first part
        first = msg.get_payload(0)
        if first:
            payload = first.get_payload(decode=True) or b""
            return payload.decode(first.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def _get_message_id(msg) -> Optional[str]:
    mid = msg.get("Message-Id") or msg.get("Message-ID")
    if not mid:
        return None
    # Normalize whitespace
    return str(make_header(decode_header(mid))).strip()


def _build_gmail_link(message_id: str) -> str:
    """
    Build a Gmail web URL that searches by rfc822msgid.
    """
    # Escape angle brackets for use in the search URL.
    escaped = message_id.replace("<", "%3C").replace(">", "%3E")
    return f"https://mail.google.com/mail/u/0/#search/rfc822msgid%3A{escaped}"


def _process_message(db_session, uid: bytes, raw_email: bytes) -> None:
    msg = message_from_bytes(raw_email)
    message_id = _get_message_id(msg)
    if not message_id:
        # Without a stable id we still capture, but we lose strong
        # idempotency guarantees. Use the UID as a fallback source_id.
        source_id = uid.decode()
    else:
        source_id = message_id

    # Idempotency: check if we have already captured this email.
    existing = (
        db_session.query(Capture)
        .filter(Capture.source == "email", Capture.source_id == source_id)
        .first()
    )
    if existing:
        return

    body = _get_message_body(msg)
    source_link = _build_gmail_link(message_id) if message_id else None

    capture = Capture(
        raw_text=body,
        source="email",
        source_id=source_id,
        source_link=source_link,
    )
    db_session.add(capture)
    db_session.commit()


def _poll_once() -> None:
    client = _open_imap_connection()
    if client is None:
        return

    try:
        # Use Gmail's X-GM-RAW search to match the specified query.
        query = _build_gmail_search_query()
        typ, data = client.uid("SEARCH", "X-GM-RAW", query)
        if typ != "OK":
            return

        uids = data[0].split()
        if not uids:
            return

        db_session = SessionLocal()
        try:
            for uid in uids:
                typ, msg_data = client.uid("FETCH", uid, "(RFC822)")
                if typ != "OK" or not msg_data or msg_data[0] is None:
                    continue
                raw_email = msg_data[0][1]
                _process_message(db_session, uid, raw_email)
        finally:
            db_session.close()
    finally:
        try:
            client.logout()
        except Exception:
            pass


def run_imap_poller() -> None:
    """
    Simple polling loop that runs in a background thread.

    Failures are swallowed to avoid crashing the main application; they
    will be visible through logs when logging is added.
    """
    while True:
        try:
            _poll_once()
        except Exception:
            # Intentionally minimal: we do not crash the loop.
            pass
        time.sleep(POLL_INTERVAL_SECONDS)


def start_background_poller() -> None:
    """
    Start the poller in a daemon thread. Intended to be called from
    FastAPI startup.
    """
    thread = threading.Thread(target=run_imap_poller, name="imap-poller", daemon=True)
    thread.start()

