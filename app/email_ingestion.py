import imaplib
import logging
import os
import threading
import time
from email import message_from_bytes
from email.header import decode_header, make_header
from typing import Optional

from .db import SessionLocal
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


POLL_INTERVAL_SECONDS = 60  # 1 hour for production


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

    try:
        logger.info(f"Connecting to IMAP {host}:{port} as {user}")
        client = imaplib.IMAP4_SSL(host, port)
        client.login(user, password)
        logger.info("IMAP connection established")
        return client
    except Exception as e:
        logger.error(f"Failed to connect to IMAP: {e}")
        return None


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
    """
    Process an email message and create a capture.

    No idempotency check needed - gtdinput/gtdprocessed labels are mutually exclusive.
    """
    msg = message_from_bytes(raw_email)
    message_id = _get_message_id(msg)
    if not message_id:
        source_id = uid.decode()
    else:
        source_id = message_id

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
            processed = 0
            for uid in uids:
                typ, msg_data = client.fetch(uid, "(RFC822)")
                if typ != "OK" or not msg_data or msg_data[0] is None:
                    logger.warning(f"Failed to fetch email UID {uid.decode()}")
                    continue
                raw_email = msg_data[0][1]

                # Process the message and store in DB
                _process_message(db_session, uid, raw_email)

                # Move from gtdinput to gtdprocessed (mutually exclusive labels)
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

            # Expunge to finalize deletion (remove from gtdinput)
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

