"""Tests for app.email_ingestion — email polling (mock IMAP)."""

import email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch, call

import pytest

from app.email_ingestion import (
    _build_gmail_link,
    _clean_html,
    _get_message_body,
    _get_message_id,
    _open_imap_connection,
    _process_message,
)
from app.models import Capture


class TestBuildGmailLink:
    """Test Gmail link building."""

    def test_build_link_basic(self):
        link = _build_gmail_link("<abc@example.com>")
        assert "mail.google.com" in link
        assert "rfc822msgid" in link

    def test_build_link_escapes_angle_brackets(self):
        link = _build_gmail_link("<test@test.com>")
        assert "%3C" in link
        assert "%3E" in link
        assert "<" not in link.split("rfc822msgid")[1]


class TestCleanHtml:
    """Test HTML cleaning."""

    def test_basic_html(self):
        html = "<html><body><p>Hello world</p></body></html>"
        result = _clean_html(html)
        assert "Hello world" in result

    def test_strips_scripts(self):
        html = "<html><body><script>alert('xss')</script><p>Content</p></body></html>"
        result = _clean_html(html)
        assert "alert" not in result
        assert "Content" in result

    def test_strips_styles(self):
        html = "<html><head><style>.x{color:red}</style></head><body><p>Text</p></body></html>"
        result = _clean_html(html)
        assert "color" not in result
        assert "Text" in result

    def test_preserves_newlines(self):
        html = "<p>Line 1</p><p>Line 2</p>"
        result = _clean_html(html)
        assert "Line 1" in result
        assert "Line 2" in result

    def test_removes_empty_lines(self):
        html = "<p>Text</p><br><br><br><p>More</p>"
        result = _clean_html(html)
        lines = result.strip().split("\n")
        # Should not have excessive blank lines
        assert all(line.strip() for line in lines)


class TestGetMessageBody:
    """Test email body extraction."""

    def test_plain_text_email(self):
        msg = MIMEText("Hello from plain text", "plain")
        body = _get_message_body(msg)
        assert "Hello from plain text" in body

    def test_html_email(self):
        msg = MIMEText("<p>Hello from HTML</p>", "html")
        body = _get_message_body(msg)
        assert "Hello from HTML" in body

    def test_multipart_prefers_plain(self):
        """For multipart, plain text should be preferred over HTML."""
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText("Plain version", "plain"))
        msg.attach(MIMEText("<p>HTML version</p>", "html"))
        body = _get_message_body(msg)
        assert "Plain version" in body

    def test_multipart_falls_back_to_html(self):
        """If only HTML is available, use it."""
        msg = MIMEMultipart()
        msg.attach(MIMEText("<p>Only HTML</p>", "html"))
        body = _get_message_body(msg)
        assert "Only HTML" in body

    def test_empty_email(self):
        msg = MIMEText("", "plain")
        body = _get_message_body(msg)
        assert body == ""


class TestGetMessageId:
    """Test Message-ID extraction."""

    def test_extracts_message_id(self):
        msg = MIMEText("Test", "plain")
        msg["Message-Id"] = "<test@example.com>"
        mid = _get_message_id(msg)
        assert mid == "<test@example.com>"

    def test_missing_message_id(self):
        msg = MIMEText("Test", "plain")
        mid = _get_message_id(msg)
        assert mid is None


class TestOpenImapConnection:
    """Test IMAP connection setup."""

    def test_no_credentials_returns_none(self, monkeypatch):
        monkeypatch.delenv("IMAP_USERNAME", raising=False)
        monkeypatch.delenv("IMAP_PASSWORD", raising=False)
        result = _open_imap_connection()
        assert result is None

    def test_missing_password_returns_none(self, monkeypatch):
        monkeypatch.setenv("IMAP_USERNAME", "user@test.com")
        monkeypatch.delenv("IMAP_PASSWORD", raising=False)
        result = _open_imap_connection()
        assert result is None

    def test_connection_failure_returns_none(self, monkeypatch):
        monkeypatch.setenv("IMAP_USERNAME", "user@test.com")
        monkeypatch.setenv("IMAP_PASSWORD", "password123")
        monkeypatch.setenv("IMAP_HOST", "imap.test.com")

        with patch("app.email_ingestion.imaplib.IMAP4_SSL", side_effect=Exception("Connection refused")):
            result = _open_imap_connection()
            assert result is None


class TestProcessMessage:
    """Test email message processing."""

    def test_creates_capture_from_email(self, db_session):
        msg = MIMEText("Buy groceries and pick up laundry", "plain")
        msg["Message-Id"] = "<unique@example.com>"

        _process_message(db_session, b"1", msg.as_bytes())

        captures = db_session.query(Capture).all()
        assert len(captures) == 1
        assert captures[0].source == "email"
        assert captures[0].source_id == "<unique@example.com>"
        assert "Buy groceries" in captures[0].raw_text

    def test_deduplication_by_message_id(self, db_session):
        """Same message_id should not create duplicate captures."""
        # Create existing capture
        existing = Capture(
            raw_text="Already processed",
            source="email",
            source_id="<dup@example.com>",
        )
        db_session.add(existing)
        db_session.commit()

        msg = MIMEText("Same email again", "plain")
        msg["Message-Id"] = "<dup@example.com>"

        _process_message(db_session, b"1", msg.as_bytes())

        captures = db_session.query(Capture).filter_by(source="email").all()
        assert len(captures) == 1  # Still only one

    def test_capture_without_message_id(self, db_session):
        """Email without Message-Id should use UID as source_id."""
        msg = MIMEText("No message ID", "plain")

        _process_message(db_session, b"42", msg.as_bytes())

        captures = db_session.query(Capture).all()
        assert len(captures) == 1
        assert captures[0].source_id == "42"

    def test_gmail_link_set_when_message_id_present(self, db_session):
        msg = MIMEText("Link test", "plain")
        msg["Message-Id"] = "<linktest@example.com>"

        _process_message(db_session, b"1", msg.as_bytes())

        c = db_session.query(Capture).first()
        assert c.email_link is not None
        assert "mail.google.com" in c.email_link
