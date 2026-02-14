import imaplib
import logging
import os
import threading
import time
from email import message_from_bytes
from email.header import decode_header, make_header
from typing import Optional
from bs4 import BeautifulSoup

from .config import config
from .db import SessionLocal
from .db_utils import transactional_session
from .models import Capture

logger = logging.getLogger(__name__)


"""
Email ingestion via IMAP.

This module implements a very small, explicit poller that:
- connects to Gmail via IMAP
- searches for messages matching a fixed query
- creates Capture records for each email (idempotently)

No AI, clarification, or RTM integration is performed here.
"""


# Import polling interval from config
POLL_INTERVAL_SECONDS = config.EMAIL_POLL_INTERVAL


def _get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name, default)
    return value




def _open_imap_connection() -> Optional[imaplib.IMAP4_SSL]:
    host = _get_env("IMAP_HOST", "imap.gmail.com")
    user = _get_env("IMAP_USERNAME")
    password = _get_env("IMAP_PASSWORD")

    if not user or not password:
        # Without credentials we simply skip ingestion; this keeps the
        # system runnable without email configured.
        has_user = bool(os.environ.get("IMAP_USERNAME"))
        has_pass = bool(os.environ.get("IMAP_PASSWORD"))
        logger.info(
            "IMAP credentials not configured, skipping email ingestion "
            "(IMAP_USERNAME=%s, IMAP_PASSWORD=%s)",
            "set" if has_user else "missing",
            "set" if has_pass else "missing",
        )
        return None

    port_raw = _get_env("IMAP_PORT", "993")
    try:
        port = int(port_raw)
    except ValueError:
        port = 993

    # Get IMAP timeout from config
    timeout = config.IMAP_TIMEOUT

    try:
        logger.info(
            f"Connecting to IMAP {host}:{port} as {user}",
            extra={
                "component": "email",
                "external_service": "gmail_imap",
                "operation": "connect",
            },
        )
        client = imaplib.IMAP4_SSL(host, port, timeout=timeout)
        client.login(user, password)
        logger.info(
            "IMAP connection established",
            extra={
                "component": "email",
                "external_service": "gmail_imap",
                "operation": "connect",
            },
        )
        return client
    except Exception as e:
        logger.error(
            f"Failed to connect to IMAP: {e}",
            extra={
                "component": "email",
                "external_service": "gmail_imap",
                "operation": "connect",
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        return None


def _clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # remove scripts and styles
    for tag in soup(["script", "style", "head", "meta"]):
        tag.decompose()

    text = soup.get_text(separator="\n")

    # Normalize whitespace
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    return "\n".join(lines)


def _get_message_body(msg) -> str:
    """
    Extract a clean text body from email.
    Preference:
    1. text/plain
    2. cleaned text/html
    """

    text_part = None
    html_part = None

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()

            if content_type == "text/plain" and text_part is None:
                payload = part.get_payload(decode=True) or b""
                text_part = payload.decode(
                    part.get_content_charset() or "utf-8",
                    errors="replace",
                )

            elif content_type == "text/html" and html_part is None:
                payload = part.get_payload(decode=True) or b""
                html_part = payload.decode(
                    part.get_content_charset() or "utf-8",
                    errors="replace",
                )
    else:
        payload = msg.get_payload(decode=True) or b""
        content_type = msg.get_content_type()

        if content_type == "text/plain":
            text_part = payload.decode(
                msg.get_content_charset() or "utf-8",
                errors="replace",
            )
        elif content_type == "text/html":
            html_part = payload.decode(
                msg.get_content_charset() or "utf-8",
                errors="replace",
            )

    # Prefer plain text
    if text_part and text_part.strip():
        return text_part.strip()

    # Fallback to cleaned HTML
    if html_part:
        return _clean_html(html_part)

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
    """
    Process an email message and create a capture.

    Includes deduplication check to prevent duplicates from email processing failures.
    Even though gtdinput/gtdprocessed labels are normally mutually exclusive,
    this check protects against edge cases where label move fails.
    """
    msg = message_from_bytes(raw_email)
    message_id = _get_message_id(msg)
    if not message_id:
        source_id = uid.decode()
    else:
        source_id = message_id

    # Deduplication: check if we already have a capture from this email
    existing = (
        db_session.query(Capture)
        .filter(
            Capture.source == "email",
            Capture.source_id == source_id
        )
        .first()
    )

    if existing:
        logger.info(
            f"Email already processed, skipping (source_id={source_id})",
            extra={
                "component": "email",
                "operation": "process_message",
                "error_type": "duplicate",
            },
        )
        return

    body = _get_message_body(msg)
    source_link = _build_gmail_link(message_id) if message_id else None

    capture = Capture(
        raw_text=body,
        source="email",
        source_id=source_id,
        source_link=source_link,
        email_id=message_id,
        email_link=source_link,
    )
    db_session.add(capture)
    with transactional_session(db_session):
        pass  # Context manager handles commit

    logger.info(
        f"Created capture from email (source_id={source_id})",
        extra={
            "component": "email",
            "operation": "process_message",
        },
    )


def _poll_once() -> None:
    client = _open_imap_connection()
    if client is None:
        return

    try:
        # Select the gtdinput mailbox (Gmail label as IMAP folder)
        logger.info("Selecting gtdinput mailbox")
        typ, data = client.select("gtdinput")
        if typ != "OK":
            logger.warning(f"Failed to select gtdinput mailbox: {typ} {data}")
            return

        # Search for all messages in gtdinput mailbox
        # Idempotency is handled by message-id check in _process_message
        logger.info("Searching for all messages in gtdinput")
        typ, data = client.search(None, "ALL")
        if typ != "OK":
            logger.warning(f"Search failed: {typ}")
            return

        uids = data[0].split()
        logger.info(f"Found {len(uids)} emails in gtdinput")
        if not uids:
            return

        db_session = SessionLocal()
        try:
            # Fetch all email data first to avoid UID invalidation
            emails_to_process = []
            for uid in uids:
                typ, msg_data = client.fetch(uid, "(RFC822)")
                if typ != "OK" or not msg_data or msg_data[0] is None:
                    logger.warning(f"Failed to fetch email UID {uid.decode()}")
                    continue
                raw_email = msg_data[0][1]
                emails_to_process.append((uid, raw_email))

            # Process all emails and store in DB
            for uid, raw_email in emails_to_process:
                _process_message(db_session, uid, raw_email)

            # Move all emails from gtdinput to gtdprocessed
            processed = 0
            for uid, raw_email in emails_to_process:
                try:
                    # Add gtdprocessed label
                    typ, copy_data = client.copy(uid, "gtdprocessed")
                    if typ != "OK":
                        logger.warning(f"Failed to copy to gtdprocessed: {typ}")
                        continue

                    # Remove gtdinput label by deleting from this mailbox
                    typ, store_data = client.store(uid, "+FLAGS", "(\\Deleted)")
                    if typ != "OK":
                        logger.warning(f"Failed to mark for deletion: {typ}")
                        continue

                    logger.info(f"Moved email {uid.decode()} from gtdinput to gtdprocessed")
                    processed += 1
                except Exception as e:
                    logger.warning(f"Error moving email {uid.decode()}: {e}")

            # Expunge once after all emails are processed
            if processed > 0:
                client.expunge()

            logger.info(f"Processed {processed} new emails from gtdinput")
        finally:
            db_session.close()
    except Exception as e:
        logger.error(f"Error during email polling: {e}", exc_info=True)
    finally:
        try:
            client.logout()
        except Exception as e:
            logger.warning(f"Error closing IMAP connection: {e}")


def poll_once() -> None:
    """
    Execute one immediate IMAP polling cycle.

    Intended for manual trigger endpoints/UI actions.
    """
    _poll_once()


def run_imap_poller() -> None:
    """
    Simple polling loop that runs in a background thread.

    Failures are logged but do not crash the main application.
    """
    logger.info("Starting IMAP poller thread")
    while True:
        try:
            _poll_once()
        except Exception as e:
            logger.error(f"Unexpected error in IMAP poller loop: {e}", exc_info=True)
        time.sleep(POLL_INTERVAL_SECONDS)


def start_background_poller() -> None:
    """
    Start the poller in a daemon thread. Intended to be called from
    FastAPI startup.
    """
    logger.info("Starting background IMAP poller")
    thread = threading.Thread(target=run_imap_poller, name="imap-poller", daemon=True)
    thread.start()
    logger.info("IMAP poller thread started")
